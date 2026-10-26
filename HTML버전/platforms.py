"""Explicit platform URL, identity and CDN policy; never a generic URL fetcher.

Extractor semantics checked against the official yt-dlp 2026.08.19 sources:
https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/yt_dlp/extractor/instagram.py
https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/yt_dlp/extractor/twitter.py
https://github.com/yt-dlp/yt-dlp/blob/2026.08.19/yt_dlp/extractor/facebook.py
"""
from __future__ import annotations
from dataclasses import dataclass
import re
from urllib.parse import parse_qsl, urlsplit


class PlatformError(RuntimeError):
    def __init__(self, message: str, code: str = "invalid_url", status: int = 400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


LABELS = {"youtube": "YouTube", "instagram": "Instagram", "threads": "Threads", "x": "X", "facebook": "Facebook"}
HOSTS = {
    "youtube": {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com", "youtu.be", "www.youtu.be"},
    "instagram": {"instagram.com", "www.instagram.com"},
    "threads": {"threads.net", "www.threads.net", "threads.com", "www.threads.com"},
    "x": {"x.com", "www.x.com", "mobile.x.com", "twitter.com", "www.twitter.com", "mobile.twitter.com"},
    "facebook": {"facebook.com", "www.facebook.com", "m.facebook.com", "mbasic.facebook.com", "web.facebook.com"},
}
EXTRACTORS = {"youtube": "^youtube$", "instagram": "^instagram$", "x": "^twitter$", "facebook": "^facebook$"}
EXTRACTOR_KEYS = {"youtube": {"youtube"}, "instagram": {"instagram"}, "x": {"twitter"}, "facebook": {"facebook"}, "threads": {"threadsdirect"}}
THUMBNAIL_CSP = "https://i.ytimg.com https://i9.ytimg.com https://img.youtube.com https://pbs.twimg.com https://*.cdninstagram.com https://*.fbcdn.net"


@dataclass(frozen=True)
class VideoTarget:
    platform: str
    url: str
    source_id: str
    media_index: int | None = None


def capabilities() -> list[dict]:
    return [{"id": key, "label": label, "available": True} for key, label in LABELS.items()]


def canonical_url(value: str, platform: str = "youtube", *, allow_unavailable: bool = False) -> VideoTarget:
    if platform not in LABELS:
        raise PlatformError("Unsupported platform.", "unsupported_platform", 422)
    if not isinstance(value, str) or not value.strip() or len(value) > 2048:
        raise PlatformError("Enter a valid video URL.")
    value = value.strip()
    if any(ord(c) < 32 or c == "\\" for c in value):
        raise PlatformError("Enter a valid video URL.")
    if "://" not in value:
        value = "https://" + value
    try:
        parts = urlsplit(value)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or parts.username is not None or parts.password is not None or parts.port is not None or parts.netloc.lower() != host:
            raise ValueError()
        if host not in HOSTS[platform]:
            known = any(host in values for values in HOSTS.values())
            raise PlatformError("URL does not match the selected platform." if known else "Use a supported video URL.", "platform_mismatch" if known else "invalid_url")
        # No encoded paths, ambiguous duplicate IDs, or redirect parameters survive.
        if "%" in parts.path or "//" in parts.path:
            raise ValueError()
        query = parse_qsl(parts.query, keep_blank_values=True)
        values = {}
        for key, item in query:
            if key in values:
                raise ValueError()
            values[key] = item
        path = parts.path.strip("/").split("/")
        source_id = None
        media_index = None
        if platform == "youtube":
            if host in ("youtu.be", "www.youtu.be") and len(path) == 1:
                source_id = path[0]
            elif path == ["watch"]:
                source_id = values.get("v")
            elif len(path) == 2 and path[0] in ("shorts", "live", "embed"):
                source_id = path[1]
            if not source_id or not re.fullmatch(r"[A-Za-z0-9_-]{11}", source_id):
                raise ValueError()
            url = f"https://www.youtube.com/watch?v={source_id}"
        elif platform == "instagram":
            if path and path[0].lower() == "share":
                raise ValueError()
            if "img_index" in values:
                raise PlatformError("Use a single-video post.", "multiple_videos", 422)
            if len(path) == 3 and re.fullmatch(r"[A-Za-z0-9._]{1,30}", path[0]):
                path = path[1:]
            if len(path) != 2 or path[0] not in ("p", "reel", "reels", "tv") or not re.fullmatch(r"[A-Za-z0-9_-]{5,64}", path[1]):
                raise ValueError()
            source_id = path[1]
            kind = "reel" if path[0] == "reels" else path[0]
            url = f"https://www.instagram.com/{kind}/{source_id}/"
        elif platform == "x":
            if len(path) >= 2 and path[-2] in ("video", "photo"):
                if path[-2] != "video" or not re.fullmatch(r"[1-9][0-9]?", path[-1]):
                    raise ValueError()
                media_index, path = int(path[-1]), path[:-2]
            if len(path) == 3 and re.fullmatch(r"[A-Za-z0-9_]{1,30}", path[0]) and path[1] == "status":
                source_id = path[2]
            elif len(path) == 4 and path[:3] == ["i", "web", "status"]:
                source_id = path[3]
            elif len(path) == 2 and path[0] == "statuses":
                source_id = path[1]
            if not source_id or not re.fullmatch(r"[1-9][0-9]{0,24}", source_id):
                raise ValueError()
            url = f"https://x.com/i/web/status/{source_id}" + (f"/video/{media_index}" if media_index else "")
        elif platform == "facebook":
            if path in (["watch"], ["video.php"], ["video", "video.php"]):
                source_id = values.get("v")
                if "video_id" in values or "story_fbid" in values:
                    raise ValueError()
            elif len(path) == 2 and path[0] == "reel":
                source_id = path[1]
            elif len(path) in (3, 4) and path[1] == "videos" and re.fullmatch(r"[A-Za-z0-9.\-_]{1,100}", path[0]):
                source_id = path[-1]
            if not source_id or not re.fullmatch(r"[1-9][0-9]{0,29}", source_id):
                raise ValueError()
            url = f"https://www.facebook.com/watch/?v={source_id}"
        else:
            if len(path) != 3 or not re.fullmatch(r"@[A-Za-z0-9._]{1,30}", path[0]) or path[1] != "post" or not re.fullmatch(r"[A-Za-z0-9_-]{5,64}", path[2]):
                raise ValueError()
            source_id = path[2]
            url = f"https://www.threads.com/{path[0]}/post/{source_id}"
        return VideoTarget(platform, url, source_id, media_index)
    except (ValueError, TypeError) as exc:
        if isinstance(exc, PlatformError):
            raise
        raise PlatformError("Use a single video URL.") from None


def validate_identity(target: VideoTarget, info: dict) -> str:
    if not isinstance(info, dict):
        raise PlatformError("Invalid video details.", "invalid_metadata", 502)
    if info.get("_type") in ("playlist", "multi_video", "compat_list") or "entries" in info:
        raise PlatformError("Use a single-video post.", "multiple_videos", 422)
    if info.get("_type") not in (None, "video"):
        raise PlatformError("Unsupported video redirect.", "invalid_metadata", 502)
    key = info.get("extractor_key") or info.get("extractor")
    if not isinstance(key, str) or key.lower() not in EXTRACTOR_KEYS[target.platform]:
        raise PlatformError("Video source did not match.", "extractor_mismatch", 502)
    media_id = str(info.get("id") or "")
    if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", media_id):
        raise PlatformError("Invalid video ID.", "invalid_metadata", 502)
    identity = str(info.get("display_id") or media_id) if target.platform == "x" else media_id
    if identity != target.source_id:
        raise PlatformError("Video source did not match.", "metadata_mismatch", 502)
    webpage = info.get("webpage_url")
    if webpage:
        try:
            actual = canonical_url(webpage, target.platform, allow_unavailable=True)
        except PlatformError:
            raise PlatformError("Video source did not match.", "metadata_mismatch", 502) from None
        if actual.source_id != target.source_id or (target.media_index and actual.media_index != target.media_index):
            raise PlatformError("Video source did not match.", "metadata_mismatch", 502)
    return media_id


def _https_host(value: str | None) -> str | None:
    if not isinstance(value, str) or len(value) > 8192 or any(ord(c) < 32 or c == "\\" for c in value):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme != "https" or parts.username is not None or parts.password is not None or parts.port is not None or not parts.hostname or parts.netloc.lower() != parts.hostname.lower() or not re.fullmatch(r"[a-z0-9.-]+", parts.hostname.lower()):
            return None
        return parts.hostname.lower()
    except ValueError:
        return None


def _under(host: str, domain: str) -> bool:
    return host == domain or host.endswith("." + domain)


def safe_thumbnail(value: str | None, platform: str) -> str | None:
    host = _https_host(value)
    if not host:
        return None
    allowed = (
        host in ("i.ytimg.com", "i9.ytimg.com", "img.youtube.com") if platform == "youtube"
        else host == "pbs.twimg.com" if platform == "x"
        else any(_under(host, domain) and host != domain for domain in ("cdninstagram.com", "fbcdn.net"))
    )
    return value if allowed else None


def safe_media_url(value: str | None, platform: str) -> bool:
    host = _https_host(value)
    if not host:
        return False
    domains = ("googlevideo.com",) if platform == "youtube" else ("video.twimg.com", "pbs.twimg.com") if platform == "x" else ("cdninstagram.com", "fbcdn.net")
    return any(_under(host, domain) for domain in domains)
