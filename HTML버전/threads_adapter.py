"""Strict, anonymous extraction of one video from an official Threads embed.

No browser, cookies, login, third-party service, generic extractor, or redirects.
The returned info dict can be passed to yt-dlp --load-info-json. Re-extract before
use: CDN URLs expire. Duration/dimensions require downstream ffprobe validation.
"""
from __future__ import annotations
import http.client
import ipaddress
import re
import socket
import ssl
import time
from html.parser import HTMLParser
from urllib.parse import urlsplit

MAX_HTML_BYTES = 2 * 1024 * 1024
HOSTS = {'threads.com', 'www.threads.com', 'threads.net', 'www.threads.net'}
POST_PATH = re.compile(r'/@([A-Za-z0-9._]{1,30})/post/([A-Za-z0-9_-]{5,64})/?$')
ASCII_HOST = re.compile(r'[a-z0-9]+(?:[a-z0-9.-]*[a-z0-9])?')

class ThreadsError(RuntimeError):
    def __init__(self, message, code='threads_unavailable'):
        super().__init__(message)
        self.code = code

def canonical_threads_url(target_url):
    try:
        if not isinstance(target_url, str) or '\\' in target_url or any(ord(c) <= 32 or ord(c) == 127 for c in target_url):
            raise ValueError()
        p = urlsplit(target_url)
        if p.scheme not in ('http', 'https') or p.hostname not in HOSTS or p.username or p.password or p.port not in (None, 443):
            raise ValueError()
        if not ASCII_HOST.fullmatch(p.hostname) or p.netloc.lower() not in (p.hostname, p.hostname + ':443'):
            raise ValueError()
        match = POST_PATH.fullmatch(p.path)
        if not match: raise ValueError()
    except (ValueError, TypeError):
        raise ThreadsError('Enter a public Threads post URL.', 'threads_invalid_url') from None
    user, code = match.groups()
    return f'https://www.threads.com/@{user}/post/{code}', user, code

def _cdn_url(value, *, video=False):
    try:
        if not isinstance(value, str) or '\\' in value or any(ord(c) <= 32 or ord(c) == 127 for c in value):
            raise ValueError()
        p = urlsplit(value)
        if p.scheme != 'https' or p.username or p.password or p.port not in (None, 443) or p.fragment:
            raise ValueError()
        if not ASCII_HOST.fullmatch(p.hostname or '') or p.netloc.lower() not in (p.hostname, p.hostname + ':443'):
            raise ValueError()
        if not any((p.hostname or '').endswith('.' + suffix) for suffix in ('cdninstagram.com', 'fbcdn.net')):
            raise ValueError()
        if video and not p.path.lower().endswith('.mp4'): raise ValueError()
        if len(value) > 16384: raise ValueError()
    except (ValueError, TypeError):
        raise ThreadsError('Unsupported Threads media URL.', 'threads_invalid_media') from None
    return value

class _EmbedParser(HTMLParser):
    VOID = {'area','base','br','col','embed','hr','img','input','link','meta','param','source','track','wbr'}
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []
        self.video_count = 0
        self.sources = []
        self.post_links = []
        self.caption = []
        self.poster = None
        self.carousel = False
    def handle_starttag(self, tag, attrs):
        a = dict(attrs)
        classes = set(a.get('class', '').split())
        if any('carousel' in value.lower() or 'sidecar' in value.lower() for value in classes):
            self.carousel = True
        if tag == 'video':
            self.video_count += 1
            if a.get('src'): self.sources.append(a['src'])
            if a.get('poster'): self.poster = a['poster']
        if tag == 'source' and any(item[0] == 'video' for item in self.stack) and a.get('src'):
            self.sources.append(a['src'])
        if tag == 'a' and a.get('href'):
            try:
                _, user, code = canonical_threads_url(a['href'])
                self.post_links.append((user.casefold(), code))
            except ThreadsError:
                pass
        if tag not in self.VOID:
            self.stack.append((tag, classes))
    def handle_endtag(self, tag):
        for index in range(len(self.stack) - 1, -1, -1):
            if self.stack[index][0] == tag:
                del self.stack[index:]
                break
    def handle_data(self, data):
        if any('BodyTextContainer' in classes for _, classes in self.stack) and not any(tag in ('script','style') for tag, _ in self.stack):
            self.caption.append(data)

def parse_threads_embed(embed_html, target_url):
    """Parse already-fetched official embed HTML; convenient for offline tests."""
    url, user, code = canonical_threads_url(target_url)
    if not isinstance(embed_html, str) or len(embed_html.encode('utf-8')) > MAX_HTML_BYTES:
        raise ThreadsError('Threads response is too large.')
    parser = _EmbedParser()
    try: parser.feed(embed_html)
    except Exception as error: raise ThreadsError('Invalid Threads response.') from error
    if (user.casefold(), code) not in parser.post_links:
        raise ThreadsError('Threads post could not be verified.', 'threads_identity_mismatch')
    if parser.carousel or parser.video_count > 1:
        raise ThreadsError('Use a single-video Threads post.', 'threads_multiple_videos')
    if parser.video_count != 1:
        raise ThreadsError('No public video found.', 'threads_no_video')
    sources = list(dict.fromkeys(_cdn_url(value, video=True) for value in parser.sources))
    if len(sources) != 1:
        raise ThreadsError('Use a single-video Threads post.', 'threads_multiple_videos')
    title = ' '.join(''.join(parser.caption).split()).strip()[:2000] or f'@{user} · {code}'
    poster = _cdn_url(parser.poster) if parser.poster else None
    return {
        'id': code, 'extractor_key': 'ThreadsDirect', 'extractor': 'ThreadsDirect',
        'webpage_url': url, 'original_url': url, 'title': title,
        'uploader': user, 'channel': user, 'thumbnail': poster, 'duration': 0,
        'formats': [{'format_id': 'public-embed', 'url': sources[0], 'ext': 'mp4', 'protocol': 'https'}],
    }

def _fetch_embed(url, timeout_seconds):
    timeout = min(60.0, max(1.0, float(timeout_seconds)))
    deadline = time.monotonic() + timeout
    p = urlsplit(url)
    if p.scheme != 'https' or p.hostname != 'www.threads.com' or not p.path.endswith('/embed'):
        raise ThreadsError('Invalid Threads embed URL.', 'threads_invalid_url')
    conn = http.client.HTTPSConnection(p.hostname, 443, timeout=timeout, context=ssl.create_default_context())
    def connect_public(address, connect_timeout, source_address=None):
        # Pin the actual connection to a checked address, retaining TLS hostname verification.
        try: addresses = socket.getaddrinfo(address[0], address[1], type=socket.SOCK_STREAM)
        except OSError as error: raise ThreadsError('Threads is unreachable.') from error
        if not addresses or any(not ipaddress.ip_address(item[4][0]).is_global for item in addresses):
            raise ThreadsError('Threads host is unavailable.')
        last_error = None
        for family, kind, proto, _, sockaddr in addresses:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError()
            sock = socket.socket(family, kind, proto)
            try:
                sock.settimeout(remaining)
                sock.connect(sockaddr)
                return sock
            except OSError as error:
                last_error = error
                sock.close()
        raise ThreadsError('Threads is unreachable.') from last_error
    conn._create_connection = connect_public
    try:
        conn.request('GET', p.path, headers={'Accept': 'text/html', 'Accept-Encoding': 'identity', 'User-Agent': 'clipsave/1.0'})
        response = conn.getresponse()
        # Refuse redirects, login walls, and throttled responses; no retry with cookies.
        if response.status != 200:
            raise ThreadsError('Public Threads video unavailable.')
        if 'text/html' not in response.getheader('Content-Type', '').lower():
            raise ThreadsError('Invalid Threads response.')
        length = response.getheader('Content-Length')
        if length and (not length.isdigit() or int(length) > MAX_HTML_BYTES):
            raise ThreadsError('Threads response is too large.')
        if response.getheader('Content-Encoding', 'identity').lower() not in ('identity', ''):
            raise ThreadsError('Unsupported Threads response encoding.')
        parts = []; total = 0
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0: raise TimeoutError()
            if conn.sock: conn.sock.settimeout(remaining)
            part = response.read(min(65536, MAX_HTML_BYTES + 1 - total))
            if not part: break
            total += len(part)
            if total > MAX_HTML_BYTES: raise ThreadsError('Threads response is too large.')
            parts.append(part)
        return b''.join(parts).decode('utf-8', errors='strict')
    except ThreadsError:
        raise
    except (OSError, ValueError, UnicodeError, http.client.HTTPException) as error:
        raise ThreadsError('Public Threads video unavailable.') from error
    finally:
        conn.close()

def inspect_threads(target_url, timeout_seconds=30):
    url, _, _ = canonical_threads_url(target_url)
    return parse_threads_embed(_fetch_embed(url + '/embed', timeout_seconds), url)

if __name__ == '__main__':
    import argparse
    import json
    import sys
    parser = argparse.ArgumentParser()
    parser.add_argument('--url', required=True)
    parser.add_argument('--timeout', type=float, default=30)
    args = parser.parse_args()
    try:
        print(json.dumps(inspect_threads(args.url, args.timeout), ensure_ascii=False))
    except ThreadsError as error:
        print(json.dumps({'code': error.code, 'error': str(error)}), file=sys.stderr)
        sys.exit(1)
