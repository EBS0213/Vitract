"""Private, bounded single-process download queue for the 영상저장 web UI.

Only allowlisted platform video URLs reach yt-dlp. Every process has its own process
group, every job has a server-generated directory, and publication is serialized
with cancellation. This module does not import the HTTP framework.
"""
from __future__ import annotations

import concurrent.futures
import contextlib
import dataclasses
import datetime as dt
import fcntl
import hashlib
import hmac
import json
import math
import os
from pathlib import Path
import re
import secrets
import selectors
import shutil
import signal
import sqlite3
import struct
import subprocess
import sys
import threading
import time
from typing import Any, Callable
from urllib.parse import urlsplit, urljoin

from platforms import (PlatformError, VideoTarget, LABELS, EXTRACTORS, canonical_url,
                       validate_identity, safe_thumbnail, safe_media_url)

VERSION = "1.2.0"
THREADS_ADAPTER_PATH = Path(__file__).resolve().with_name("threads_adapter.py")
ACTIVE = ("queued", "downloading", "converting", "validating")
TERMINAL = ("completed", "cancelled", "failed")


class EngineError(PlatformError):
    def __init__(self, message: str, code: str = "operation_failed", status: int = 400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


class Cancelled(EngineError):
    def __init__(self):
        super().__init__("Cancelled.", "cancelled", 409)


def utc_now() -> str:
    return dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def canonical_youtube_url(value: str) -> tuple[str, str]:
    try:
        target = canonical_url(value, "youtube")
        return target.url, target.source_id
    except PlatformError as exc:
        raise EngineError(exc.message, exc.code, exc.status) from None


def safe_filename(title: str, video_id: str) -> str:
    title = re.sub(r'[\\/:<>"|?*\x00-\x1f\x7f]', " ", title)
    title = " ".join(title.split()).strip(". ") or "Video"
    stem = title.encode("utf-8")[:180].decode("utf-8", "ignore").rstrip(". ")
    safe_id = re.sub(r"[^A-Za-z0-9_-]", "", str(video_id))[:64] or "video"
    return f"{stem or 'Video'} [{safe_id}].mp4"


def _limit(name: str, default: int, minimum: int = 1) -> int:
    raw = os.environ.get(name, str(default))
    try:
        value = int(raw)
        if value < minimum:
            raise ValueError()
        return value
    except ValueError:
        raise RuntimeError(f"{name} must be an integer >= {minimum}") from None


@dataclasses.dataclass(frozen=True)
class Config:
    data_dir: Path
    max_duration_seconds: int = 10800
    max_file_bytes: int = 2 * 1024**3
    max_storage_bytes: int = 8 * 1024**3
    min_free_bytes: int = 512 * 1024**2
    job_timeout_seconds: int = 3600
    metadata_timeout_seconds: int = 90
    ttl_seconds: int = 3600
    session_ttl_seconds: int = 7200
    max_active_jobs: int = 8
    max_owner_jobs: int = 2

    @classmethod
    def from_env(cls, data_dir: str | Path | None = None) -> "Config":
        base = Path(__file__).resolve().parent
        return cls(
            data_dir=Path(data_dir or os.environ.get("VIDEO_DATA_DIR", base / "data")).expanduser().resolve(),
            max_duration_seconds=_limit("VIDEO_MAX_DURATION_SECONDS", 10800),
            max_file_bytes=_limit("VIDEO_MAX_FILE_BYTES", 2 * 1024**3),
            max_storage_bytes=_limit("VIDEO_MAX_STORAGE_BYTES", 8 * 1024**3),
            min_free_bytes=_limit("VIDEO_MIN_FREE_BYTES", 512 * 1024**2, 0),
            job_timeout_seconds=_limit("VIDEO_JOB_TIMEOUT_SECONDS", 3600),
            metadata_timeout_seconds=_limit("VIDEO_METADATA_TIMEOUT_SECONDS", 90),
            ttl_seconds=_limit("VIDEO_TTL_SECONDS", 3600),
            session_ttl_seconds=_limit("VIDEO_SESSION_TTL_SECONDS", 7200),
            max_active_jobs=_limit("VIDEO_MAX_ACTIVE_JOBS", 8),
            max_owner_jobs=_limit("VIDEO_MAX_OWNER_JOBS", 2),
        )


def discover_tools() -> dict[str, str | None]:
    base = Path(__file__).resolve().parent
    suffix = Path("build/영상저장.app/Contents/Resources/tools")
    roots = [base.parent / suffix, base.parent / "macOS버전" / suffix,
             Path.home() / "Documents" / "영상제작 APP" / "macOS버전" / suffix]
    found: dict[str, str | None] = {}
    for name, env in (("yt-dlp", "VIDEO_YTDLP"), ("ffmpeg", "VIDEO_FFMPEG"), ("ffprobe", "VIDEO_FFPROBE"), ("node", "VIDEO_NODE")):
        override = os.environ.get(env)
        if override:
            candidates = [Path(override).expanduser()]
        else:
            candidates = [root / name for root in roots]
            on_path = shutil.which(name)
            if on_path:
                candidates.append(Path(on_path))
        found[name] = next((str(p.resolve()) for p in candidates if p.is_file() and os.access(p, os.X_OK)), None)
    return found


def _private_file(path: Path, flags: int) -> int:
    return os.open(path, flags | getattr(os, "O_NOFOLLOW", 0), 0o600)


class Engine:
    def __init__(self, config: Config):
        self.config = config
        self.tools = discover_tools()
        self.lock = threading.RLock()
        self.stop = threading.Event()
        self.processes: dict[str, subprocess.Popen] = {}
        self.cancel_events: dict[str, threading.Event] = {}
        self.metadata_slots = threading.BoundedSemaphore(2)
        self.db: sqlite3.Connection | None = None
        self.pool: concurrent.futures.ThreadPoolExecutor | None = None
        self.lock_file = None
        self.housekeeper = None

    @property
    def readiness(self) -> dict:
        missing = [name for name, path in self.tools.items() if path is None]
        return {"ready": not missing, "missing_tools": missing}

    def start(self) -> None:
        root = self.config.data_dir
        root.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(root, 0o700)
        self.jobs_dir = root / "jobs"
        self.jobs_dir.mkdir(exist_ok=True, mode=0o700)
        if self.jobs_dir.is_symlink():
            raise RuntimeError("jobs directory must not be a symlink")
        self.lock_file = os.fdopen(_private_file(root / "app.lock", os.O_RDWR | os.O_CREAT), "a+")
        try:
            fcntl.flock(self.lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            self.lock_file.close()
            self.lock_file = None
            raise RuntimeError("이 데이터 폴더를 사용하는 서버가 이미 있습니다. workers=1로 실행하세요.") from None
        secret_path = root / "secret.key"
        try:
            fd = _private_file(secret_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL)
        except FileExistsError:
            with os.fdopen(_private_file(secret_path, os.O_RDONLY), "rb") as handle:
                self.secret = handle.read()
            if len(self.secret) != 32:
                raise RuntimeError("Invalid application secret")
        else:
            self.secret = secrets.token_bytes(32)
            with os.fdopen(fd, "wb") as handle:
                handle.write(self.secret)
                handle.flush()
                os.fsync(handle.fileno())
        os.chmod(secret_path, 0o600)
        db_path = root / "state.sqlite3"
        if db_path.is_symlink():
            raise RuntimeError("database must not be a symlink")
        # Pre-create privately; sqlite will not need a globally writable umask.
        os.close(_private_file(db_path, os.O_RDWR | os.O_CREAT))
        os.chmod(db_path, 0o600)
        self.db = sqlite3.connect(db_path, check_same_thread=False, isolation_level=None)
        self.db.row_factory = sqlite3.Row
        self.db.executescript("""
            PRAGMA foreign_keys=ON;
            PRAGMA busy_timeout=5000;
            CREATE TABLE IF NOT EXISTS sessions (
              id TEXT PRIMARY KEY, csrf TEXT NOT NULL, authenticated INTEGER NOT NULL,
              created REAL NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS metadata (
              token TEXT PRIMARY KEY, owner TEXT NOT NULL, payload TEXT NOT NULL, expires REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS metadata_owner ON metadata(owner);
            CREATE TABLE IF NOT EXISTS jobs (
              id TEXT PRIMARY KEY, owner TEXT NOT NULL, status TEXT NOT NULL,
              title TEXT NOT NULL, quality INTEGER NOT NULL, progress REAL,
              stage_message TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
              created_epoch REAL NOT NULL, filename TEXT, bytes INTEGER, error TEXT,
              video_id TEXT NOT NULL, url TEXT NOT NULL, duration REAL NOT NULL);
            CREATE INDEX IF NOT EXISTS jobs_owner ON jobs(owner, created_epoch);
            CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
        """)
        if "finished_epoch" not in {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}:
            self.db.execute("ALTER TABLE jobs ADD COLUMN finished_epoch REAL")
        columns = {row[1] for row in self.db.execute("PRAGMA table_info(jobs)")}
        if "platform" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN platform TEXT NOT NULL DEFAULT 'youtube'")
        if "media_id" not in columns:
            self.db.execute("ALTER TABLE jobs ADD COLUMN media_id TEXT")
        with self.lock:
            rows = self.db.execute("SELECT id FROM jobs WHERE status IN ('queued','downloading','converting','validating')").fetchall()
            self.db.execute("UPDATE jobs SET status='failed', progress=NULL, error=?, stage_message=?, updated_at=?,finished_epoch=? WHERE status IN ('queued','downloading','converting','validating')", ("Server restarted. Start a new download.", "Interrupted", utc_now(), time.time()))
            for row in rows:
                self._remove_job_files(row["id"])
        self.cleanup()
        self.pool = concurrent.futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="video-job")
        self.housekeeper = threading.Thread(target=self._housekeep, name="video-cleanup", daemon=True)
        self.housekeeper.start()

    def close(self) -> None:
        self.stop.set()
        with self.lock:
            for event in self.cancel_events.values():
                event.set()
            processes = list(self.processes.values())
        for process in processes:
            self._terminate(process)
        if self.pool:
            self.pool.shutdown(wait=True, cancel_futures=True)
        if self.housekeeper:
            self.housekeeper.join(timeout=2)
        with self.lock:
            if self.db:
                self.db.close()
                self.db = None
            if self.lock_file:
                fcntl.flock(self.lock_file, fcntl.LOCK_UN)
                self.lock_file.close()
                self.lock_file = None

    def _hash(self, raw: str) -> str:
        return hmac.new(self.secret, raw.encode(), hashlib.sha256).hexdigest()

    def session(self, raw: str | None, auto_auth: bool, on_create: Callable[[], None] | None = None) -> tuple[dict, str | None]:
        now = time.time()
        with self.lock:
            if raw and len(raw) <= 128:
                row = self.db.execute("SELECT * FROM sessions WHERE id=? AND expires>?", (self._hash(raw), now)).fetchone()
                if row:
                    return dict(row), None
            if on_create:
                on_create()
            self.db.execute("DELETE FROM sessions WHERE expires<=?", (now,))
            if self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 4096:
                # Anonymous churn must never revoke an authenticated owner's jobs.
                self.db.execute("DELETE FROM sessions WHERE id=(SELECT id FROM sessions WHERE authenticated=0 ORDER BY created LIMIT 1)")
                if getattr(self, "anonymous_public", False) and self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 4096:
                    # Public sessions authorize a temporary transfer, not an
                    # account. Reclaim only empty sessions, retaining every
                    # owner with a job or an unexpired inspection token.
                    self.db.execute("DELETE FROM sessions WHERE id=(SELECT s.id FROM sessions s WHERE NOT EXISTS(SELECT 1 FROM jobs j WHERE j.owner=s.id) AND NOT EXISTS(SELECT 1 FROM metadata m WHERE m.owner=s.id AND m.expires>?) ORDER BY s.created LIMIT 1)", (now,))
                if self.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0] >= 4096:
                    raise EngineError("Service is busy. Try again later.", "session_limit", 503)
            token = secrets.token_urlsafe(32)
            item = {"id": self._hash(token), "csrf": secrets.token_urlsafe(32), "authenticated": int(auto_auth), "created": now, "expires": now + self.config.session_ttl_seconds}
            self.db.execute("INSERT INTO sessions VALUES (:id,:csrf,:authenticated,:created,:expires)", item)
            return item, token

    def rotate_session(self, old_id: str, authenticated: bool) -> tuple[dict, str]:
        with self.lock:
            self.db.execute("DELETE FROM sessions WHERE id=?", (old_id,))
            self.db.execute("DELETE FROM metadata WHERE owner=?", (old_id,))
            return self.session(None, authenticated)

    def bind_auth_context(self, password: str, public_origin: str | None, public_mode: bool = False):
        """Revoke cookies if the password or local/public security mode changes."""
        context = self._hash(json.dumps([password, public_origin, public_mode], ensure_ascii=False))
        with self.lock:
            self.public_mode = public_mode
            self.anonymous_public = public_mode and not password
            old = self.db.execute("SELECT value FROM settings WHERE key='auth_context'").fetchone()
            if not old or not hmac.compare_digest(old[0], context):
                self.db.execute("DELETE FROM sessions")
                self.db.execute("DELETE FROM metadata")
                self.db.execute("INSERT OR REPLACE INTO settings VALUES ('auth_context',?)", (context,))

    def _require_tools(self):
        if not self.readiness["ready"]:
            raise EngineError("Missing tools: " + ", ".join(self.readiness["missing_tools"]), "tools_missing", 503)

    def _common(self, platform: str = "youtube", *, snapshot: bool = False) -> list[str]:
        return ["--ignore-config", "--no-plugin-dirs", "--no-playlist", "--use-extractors", "^$" if snapshot else EXTRACTORS.get(platform, "^$"), "--no-colors", "--no-cache-dir", "--abort-on-error", "--socket-timeout", "15", "--retries", "2", "--fragment-retries", "2", "--ffmpeg-location", str(Path(self.tools["ffmpeg"]).parent), "--js-runtimes", "node:" + self.tools["node"]]

    def _read_metadata(self, target: VideoTarget, key: str, deadline: float, directory: Path | None = None) -> tuple[dict, list[int]]:
        if target.platform == "threads":
            command = [sys.executable, str(THREADS_ADAPTER_PATH), "--url", target.url, "--timeout", str(max(.1, deadline - time.monotonic()))]
        else:
            command = [self.tools["yt-dlp"], *self._common(target.platform), "--dump-single-json", "--skip-download", "--", target.url]
        data = self._run(command, key=key, deadline=deadline, directory=directory, capture_limit=32 * 1024**2)
        try:
            info = json.loads(data)
        except (ValueError, UnicodeDecodeError):
            raise EngineError("Could not read video details. Try again.", "invalid_metadata", 502) from None
        validate_identity(target, info)
        if info.get("is_live") or info.get("live_status") in ("is_live", "is_upcoming", "post_live"):
            raise EngineError("Live and upcoming videos are not supported.", "live_not_supported")
        duration = max(0, _number(info.get("duration")))
        if duration > self.config.max_duration_seconds:
            raise EngineError("Video exceeds the duration limit.", "duration_limit")
        if info.get("has_drm"):
            raise EngineError("DRM videos are not supported.", "no_formats")
        formats = info.get("formats")
        if not isinstance(formats, list):
            formats = [info] if info.get("url") else []
        clean = []
        for item in formats:
            if not isinstance(item, dict) or item.get("has_drm") or (item.get("vcodec") == "none" and item.get("acodec") == "none"):
                continue
            if not safe_media_url(item.get("url"), target.platform):
                continue
            if item.get("protocol") not in (None, "https", "http", "m3u8", "m3u8_native", "http_dash_segments"):
                continue
            if any(item.get(k) and not safe_media_url(item[k], target.platform) for k in ("manifest_url", "fragment_base_url")):
                continue
            fragments = item.get("fragments", [])
            if not isinstance(fragments, list) or any(not isinstance(f, dict) or (f.get("url") and not safe_media_url(urljoin(item.get("fragment_base_url") or item["url"], f["url"]), target.platform)) for f in fragments):
                continue
            clean.append(dict(item))
        video_formats = [f for f in clean if f.get("vcodec") != "none" and f.get("ext") not in ("m4a", "mp3", "aac", "ogg", "opus", "wav")]
        if not video_formats:
            raise EngineError("No downloadable video formats.", "no_formats")
        heights = {int(_number(f.get("height"))) for f in video_formats if _number(f.get("height")) > 0}
        if target.platform == "youtube" and heights:
            qualities = [q for q in (1080, 720, 480, 360) if any(h <= q for h in heights) and q <= max(heights)] or ([360] if max(heights) < 360 else [])
        else:
            qualities = [q for q in (1080, 720, 480, 360) if not heights or q <= max(heights)] or [360]
        if not qualities:
            raise EngineError("No downloadable video formats.", "no_formats")
        info["formats"], info["duration"] = clean, duration
        return info, qualities

    @staticmethod
    def _download_info(info: dict, target: VideoTarget) -> dict:
        # Re-use exactly the validated extraction, without another URL redirect or
        # playlist evaluation inside the download invocation.
        fields = ("url", "ext", "format_id", "format_note", "protocol", "manifest_url", "fragment_base_url", "fragments", "width", "height", "vcodec", "acodec", "abr", "vbr", "tbr", "fps", "asr", "audio_channels", "quality", "language", "source_preference", "preference", "resolution", "container", "filesize", "filesize_approx", "extra_param_to_segment_url")
        def headers(value):
            return {k: v for k, v in (value or {}).items() if k.lower() in ("user-agent", "accept", "accept-language", "referer", "origin") and isinstance(v, str) and len(v) <= 4096 and "\n" not in v and "\r" not in v} if isinstance(value, dict) else {}
        formats = []
        for item in info["formats"]:
            entry = {k: item[k] for k in fields if k in item}
            if "fragments" in entry:
                entry["fragments"] = [{k: f[k] for k in ("url", "duration", "range", "index") if k in f} for f in entry["fragments"]]
            entry["http_headers"] = headers(item.get("http_headers"))
            options = item.get("downloader_options")
            chunk = _number(options.get("http_chunk_size")) if isinstance(options, dict) else 0
            if chunk > 0:
                entry["downloader_options"] = {"http_chunk_size": min(int(chunk), 250 * 1024**2)}
            formats.append(entry)
        # Omit webpage_url/original_url: yt-dlp's info-file loader otherwise
        # re-extracts those URLs after DownloadError, bypassing this snapshot.
        return {"id": str(info["id"]), "display_id": target.source_id, "title": str(info.get("title") or LABELS[target.platform] + " video"), "extractor_key": info.get("extractor_key"), "extractor": info.get("extractor"), "duration": info["duration"], "formats": formats, "http_headers": headers(info.get("http_headers"))}

    def inspect(self, owner: str, input_url: str, platform: str = "youtube") -> dict:
        target = canonical_url(input_url, platform)
        self._require_tools()
        if not self.metadata_slots.acquire(blocking=False):
            raise EngineError("Video lookup is busy. Try again.", "metadata_busy", 429)
        try:
            with self.lock:
                if self.db.execute("SELECT COUNT(*) FROM metadata WHERE owner=? AND expires>?", (owner, time.time())).fetchone()[0] >= 20:
                    raise EngineError("Too many lookups. Try again later.", "metadata_limit", 429)
            info, qualities = self._read_metadata(target, "inspect-" + secrets.token_hex(12), time.monotonic() + self.config.metadata_timeout_seconds)
            thumbnail = safe_thumbnail(info.get("thumbnail"), platform)
            if not thumbnail:
                thumbnail = next((safe_thumbnail(t.get("url"), platform) for t in reversed(info.get("thumbnails") or []) if isinstance(t, dict) and safe_thumbnail(t.get("url"), platform)), None)
            result = {"id": target.source_id, "platform": platform, "title": str(info.get("title") or LABELS[platform] + " video")[:1000], "channel": str(info.get("channel") or info.get("uploader") or "")[:300], "duration": info["duration"], "thumbnail": thumbnail, "qualities": qualities}
            token = secrets.token_urlsafe(32)
            with self.lock:
                self.db.execute("INSERT INTO metadata VALUES (?,?,?,?)", (self._hash(token), owner, json.dumps({**result, "url": target.url, "media_id": str(info["id"])}, ensure_ascii=False), time.time() + 1800))
            return {**result, "metadata_token": token}
        finally:
            self.metadata_slots.release()

    def _public_job(self, row: sqlite3.Row) -> dict:
        keys = ("id", "platform", "status", "title", "quality", "progress", "stage_message", "created_at", "updated_at", "filename", "bytes", "error")
        result = {k: row[k] for k in keys}
        result["download_url"] = f"/api/jobs/{row['id']}/file" if row["status"] == "completed" else None
        return result

    def list_jobs(self, owner: str) -> list[dict]:
        with self.lock:
            return [self._public_job(row) for row in self.db.execute("SELECT * FROM jobs WHERE owner=? ORDER BY created_epoch DESC LIMIT 200", (owner,))]

    def _owned(self, job_id: str, owner: str) -> sqlite3.Row:
        row = self.db.execute("SELECT * FROM jobs WHERE id=? AND owner=?", (job_id, owner)).fetchone()
        if not row:
            raise EngineError("Download not found.", "job_not_found", 404)
        if row["status"] in TERMINAL:
            finished = row["finished_epoch"]
            if finished is None:
                with contextlib.suppress(ValueError):
                    finished = dt.datetime.fromisoformat(row["updated_at"].replace("Z", "+00:00")).timestamp()
            if finished is not None and finished < time.time() - self.config.ttl_seconds:
                self.cleanup()
                raise EngineError("Download expired. Check the link again.", "job_expired", 404)
        return row

    def get_job(self, job_id: str, owner: str) -> dict:
        with self.lock:
            return self._public_job(self._owned(job_id, owner))

    def create_job(self, owner: str, metadata_token: str, quality: int) -> dict:
        self._require_tools()
        self.cleanup()
        with self.lock:
            row = self.db.execute("SELECT payload FROM metadata WHERE token=? AND owner=? AND expires>?", (self._hash(metadata_token), owner, time.time())).fetchone()
            if not row:
                raise EngineError("Video details expired. Check the link again.", "metadata_expired", 409)
            metadata = json.loads(row[0])
            if quality not in metadata["qualities"]:
                raise EngineError("Quality is unavailable.", "invalid_quality")
            active = self.db.execute("SELECT owner FROM jobs WHERE status IN ('queued','downloading','converting','validating')").fetchall()
            if len(active) >= self.config.max_active_jobs or sum(r[0] == owner for r in active) >= self.config.max_owner_jobs:
                raise EngineError("Download queue is full. Try again later.", "queue_full", 429)
            self._check_space()
            job_id = secrets.token_hex(16)
            now = utc_now()
            (self.jobs_dir / job_id).mkdir(mode=0o700)
            self.db.execute("INSERT INTO jobs(id,owner,status,title,quality,progress,stage_message,created_at,updated_at,created_epoch,video_id,url,duration,platform,media_id) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (job_id, owner, "queued", metadata["title"], quality, 0, "Queued", now, now, time.time(), metadata["id"], metadata["url"], metadata["duration"], metadata.get("platform", "youtube"), metadata.get("media_id", metadata["id"])))
            self.cancel_events[job_id] = threading.Event()
            self.pool.submit(self._work, job_id)
            return self.get_job(job_id, owner)

    def cancel_job(self, job_id: str, owner: str) -> dict:
        with self.lock:
            row = self._owned(job_id, owner)
            if row["status"] in ACTIVE:
                self.cancel_events[job_id].set()
                self.db.execute("UPDATE jobs SET status='cancelled',progress=NULL,stage_message=?,updated_at=?,finished_epoch=? WHERE id=?", ("Cancelled", utc_now(), time.time(), job_id))
                process = self.processes.get(job_id)
            else:
                process = None
            result = self.get_job(job_id, owner)
        if process:
            self._terminate(process)
        return result

    def file_for_job(self, job_id: str, owner: str) -> tuple[Path, str]:
        with self.lock:
            row = self._owned(job_id, owner)
            if row["status"] != "completed":
                raise EngineError("Download is not ready.", "file_not_ready", 409)
            path = self._directory(job_id) / "video.mp4"
            if path.is_symlink() or not path.is_file() or path.stat().st_size != row["bytes"]:
                raise EngineError("File expired or is missing.", "file_missing", 404)
            return path, row["filename"]

    def _directory(self, job_id: str) -> Path:
        if not re.fullmatch(r"[0-9a-f]{32}", job_id):
            raise EngineError("Invalid download path.", "invalid_job", 404)
        path = self.jobs_dir / job_id
        if path.is_symlink() or path.resolve().parent != self.jobs_dir.resolve():
            raise EngineError("Invalid download path.", "invalid_job", 404)
        return path

    def _update(self, job_id: str, status: str | None = None, progress: float | None = None, message: str | None = None):
        with self.lock:
            event = self.cancel_events.get(job_id)
            if self.stop.is_set() or (event and event.is_set()):
                raise Cancelled()
            row = self.db.execute("SELECT status,progress,stage_message FROM jobs WHERE id=?", (job_id,)).fetchone()
            if not row or row["status"] not in ACTIVE:
                raise Cancelled()
            self.db.execute("UPDATE jobs SET status=?,progress=?,stage_message=?,updated_at=? WHERE id=?", (status or row["status"], max(row["progress"] or 0, progress) if progress is not None else row["progress"], message or row["stage_message"], utc_now(), job_id))

    def _work(self, job_id: str):
        directory = self._directory(job_id)
        deadline = time.monotonic() + self.config.job_timeout_seconds
        try:
            self._update(job_id, "downloading", 0, "Downloading…")
            with self.lock:
                job = dict(self.db.execute("SELECT * FROM jobs WHERE id=?", (job_id,)).fetchone())
            self._check_space()
            target = canonical_url(job["url"], job["platform"])
            info, _ = self._read_metadata(target, job_id, min(deadline, time.monotonic() + self.config.metadata_timeout_seconds), directory)
            if str(info["id"]) != (job["media_id"] or job["video_id"]):
                raise EngineError("Video changed. Check the link again.", "metadata_changed", 409)
            info_path = directory / "download-info.json"
            with os.fdopen(_private_file(info_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL), "w", encoding="utf-8") as handle:
                json.dump(self._download_info(info, target), handle, ensure_ascii=False)
            format_selector = f"bv*[height<={job['quality']}]+ba/b[height<={job['quality']}]"
            if job["platform"] != "youtube" or not any(_number(f.get("height")) > 0 for f in info["formats"] if f.get("vcodec") != "none"):
                format_selector += "/bv*+ba/b"
            def on_download(line: str):
                if line.startswith("__DOWNLOAD__"):
                    try:
                        state = json.loads(line[len("__DOWNLOAD__"):])
                        total = _number(state.get("total_bytes") or state.get("total_bytes_estimate"))
                        done = _number(state.get("downloaded_bytes"))
                        if total > self.config.max_file_bytes or done > self.config.max_file_bytes:
                            raise EngineError("File exceeds the size limit.", "file_size_limit")
                        if total:
                            self._update(job_id, progress=min(78, done / total * 78))
                    except (ValueError, TypeError, AttributeError):
                        pass
                elif line.startswith("__POSTPROCESS__"):
                    self._update(job_id, progress=80, message="Merging…")
            args = [self.tools["yt-dlp"], *self._common(job["platform"], snapshot=True), "--no-simulate", "--progress", "--newline", "--progress-delta", "0.5", "--concurrent-fragments", "2", "--max-filesize", str(self.config.max_file_bytes), "-f", format_selector, "-S", "res,vcodec:h264,acodec:aac", "--merge-output-format", "mkv", "-P", str(directory), "-o", "source.%(ext)s", "--progress-template", "download:__DOWNLOAD__%(progress.{status,downloaded_bytes,total_bytes,total_bytes_estimate})j", "--progress-template", "postprocess:__POSTPROCESS__%(progress.{status,postprocessor})j", "--print", "after_move:__FILE__%(filepath)j", "--load-info-json", str(info_path)]
            output = self._run(args, key=job_id, deadline=deadline, directory=directory, on_line=on_download)
            lines = output.decode("utf-8", "replace").splitlines()
            try:
                source = Path(json.loads(next(line[8:] for line in reversed(lines) if line.startswith("__FILE__"))))
            except (ValueError, StopIteration, TypeError):
                raise EngineError("Download is missing or exceeds the size limit.", "download_missing") from None
            if source.is_symlink() or source.resolve().parent != directory.resolve() or not source.is_file() or not source.name.startswith("source."):
                raise EngineError("Invalid output path.", "invalid_output")
            if not 0 < source.stat().st_size <= self.config.max_file_bytes:
                raise EngineError("File exceeds the size limit.", "file_size_limit")
            original = self._probe(source, job_id, deadline, directory)
            resize_needed = _number(original["video"].get("height")) > job["quality"]
            copy_video = original["video"].get("codec_name") == "h264" and original["video"].get("pix_fmt") == "yuv420p" and not resize_needed
            copy_audio = original["audio"] is None or original["audio"].get("codec_name") == "aac"
            self._update(job_id, "converting", 82, "Finalizing MP4…" if copy_video and copy_audio else "Converting…")
            staged = directory / "finished.mp4"
            encode = [self.tools["ffmpeg"], "-hide_banner", "-nostdin", "-loglevel", "error", "-stats_period", "0.5", "-progress", "pipe:1", "-i", str(source), "-map", "0:v:0", "-map", "0:a:0?", "-map_metadata", "0", "-map_chapters", "0"]
            scale = f"scale=-2:{job['quality']}" if resize_needed else "scale=trunc(iw/2)*2:trunc(ih/2)*2"
            encode += ["-c:v", "copy"] if copy_video else ["-c:v", "libx264", "-preset", "medium", "-crf", "20", "-pix_fmt", "yuv420p", "-vf", scale, "-threads", "2"]
            encode += ["-c:a", "copy"] if copy_audio else ["-c:a", "aac", "-b:a", "192k"]
            encode += ["-movflags", "+faststart", "-n", str(staged)]
            def on_encode(line: str):
                if line.startswith("out_time_us="):
                    value = _number(line.partition("=")[2])
                    self._update(job_id, progress=min(98, 82 + value / 1e6 / original["duration"] * 16))
            self._run(encode, key=job_id, deadline=deadline, directory=directory, on_line=on_encode)
            self._update(job_id, "validating", 99, "Checking MP4…")
            final = self._probe(staged, job_id, deadline, directory)
            size = staged.stat().st_size
            if final["video"].get("codec_name") != "h264" or final["video"].get("pix_fmt") != "yuv420p" or _number(final["video"].get("height")) > job["quality"] or (final["audio"] is not None and final["audio"].get("codec_name") != "aac") or (original["audio"] is not None and final["audio"] is None) or size <= 0 or size > self.config.max_file_bytes or abs(final["duration"] - original["duration"]) > max(2, original["duration"] * .02) or not is_faststart_mp4(staged):
                raise EngineError("MP4 check failed. Try again.", "validation_failed")
            os.chmod(staged, 0o600)
            with self.lock:
                self._update(job_id)  # Cancellation and publication share this lock.
                destination = directory / "video.mp4"
                if destination.exists():
                    raise EngineError("Output file already exists.", "output_exists")
                staged.rename(destination)
                self.db.execute("UPDATE jobs SET status='completed',progress=100,stage_message=?,updated_at=?,finished_epoch=?,filename=?,bytes=?,error=NULL WHERE id=?", ("Ready", utc_now(), time.time(), safe_filename(job["title"], job["video_id"]), size, job_id))
            self._remove_job_files(job_id, keep_final=True)
        except Exception as exc:
            message = exc.message if isinstance(exc, PlatformError) else "Download failed. Try again."
            with self.lock:
                row = self.db.execute("SELECT status FROM jobs WHERE id=?", (job_id,)).fetchone()
                if row and row["status"] in ACTIVE:
                    cancelled = isinstance(exc, Cancelled) and not self.stop.is_set()
                    self.db.execute("UPDATE jobs SET status=?,progress=NULL,stage_message=?,updated_at=?,finished_epoch=?,error=? WHERE id=?", ("cancelled" if cancelled else "failed", "Cancelled" if cancelled else "Failed", utc_now(), time.time(), None if cancelled else message, job_id))
            self._remove_job_files(job_id)
        finally:
            with self.lock:
                self.cancel_events.pop(job_id, None)

    def _probe(self, path: Path, key: str, deadline: float, directory: Path) -> dict:
        result = self._run([self.tools["ffprobe"], "-v", "error", "-show_entries", "stream=codec_type,codec_name,pix_fmt,width,height:format=duration", "-of", "json", str(path)], key=key, deadline=deadline, directory=directory)
        try:
            info = json.loads(result)
            video = next(s for s in info["streams"] if s.get("codec_type") == "video")
            audio = next((s for s in info["streams"] if s.get("codec_type") == "audio"), None)
            duration = _number(info["format"].get("duration"))
            if duration <= 0 or _number(video.get("width")) <= 0 or _number(video.get("height")) <= 0:
                raise ValueError()
            if duration > self.config.max_duration_seconds + .5:
                raise EngineError("Video exceeds the duration limit.", "duration_limit")
            return {"video": video, "audio": audio, "duration": duration}
        except (ValueError, KeyError, StopIteration, TypeError):
            raise EngineError("Video check failed.", "probe_failed") from None

    @staticmethod
    def _terminate(process: subprocess.Popen):
        # A downloader may exit while a grandchild still holds its stdout pipe.
        # Reap the entire group even when the group leader already exited.
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            process.wait(timeout=1.5)
        except subprocess.TimeoutExpired:
            pass
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGKILL)
        with contextlib.suppress(subprocess.TimeoutExpired):
            process.wait(timeout=2)

    def _run(self, args: list[str], *, key: str, deadline: float, directory: Path | None = None, on_line: Callable[[str], None] | None = None, capture_limit: int = 2 * 1024**2) -> bytes:
        with self.lock:
            event = self.cancel_events.get(key)
            if self.stop.is_set() or (event and event.is_set()):
                raise Cancelled()
            # No shell or inherited secrets/cookies/proxy configuration.
            environment = {"PATH": os.pathsep.join(dict.fromkeys([str(Path(p).parent) for p in self.tools.values() if p] + ["/usr/bin", "/bin"])), "LANG": "en_US.UTF-8", "LC_ALL": "en_US.UTF-8", "HOME": str(self.config.data_dir), "TMPDIR": str(directory or self.config.data_dir)}
            # Explicit local integration fixture, never set through an HTTP input.
            if os.environ.get("VIDEO_TEST_CONTROL"):
                environment["VIDEO_TEST_CONTROL"] = os.environ["VIDEO_TEST_CONTROL"]
            try:
                process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, cwd=directory or self.config.data_dir, env=environment, start_new_session=True, shell=False, umask=0o077)
            except OSError:
                raise EngineError("Could not start the video tools.", "tool_start_failed", 503) from None
            self.processes[key] = process
        selector = selectors.DefaultSelector()
        selector.register(process.stdout, selectors.EVENT_READ, "stdout")
        selector.register(process.stderr, selectors.EVENT_READ, "stderr")
        stdout, stderr = bytearray(), bytearray()
        pending = {"stdout": bytearray(), "stderr": bytearray()}
        last_space = 0.0
        try:
            while selector.get_map():
                if self.stop.is_set() or (event and event.is_set()):
                    raise Cancelled()
                now = time.monotonic()
                if now > deadline:
                    raise EngineError("Request timed out. Try again.", "timeout", 504)
                if directory and now - last_space >= .5:
                    self._check_space(directory)
                    last_space = now
                for selected, _ in selector.select(timeout=.2):
                    chunk = os.read(selected.fileobj.fileno(), 65536)
                    if not chunk:
                        selector.unregister(selected.fileobj)
                        continue
                    stream = selected.data
                    target = stdout if stream == "stdout" else stderr
                    target.extend(chunk)
                    if stream == "stdout" and len(target) > capture_limit:
                        if key.startswith("inspect-"):
                            raise EngineError("Video details are too large.", "metadata_too_large", 502)
                        del target[:-capture_limit]
                    elif stream == "stderr" and len(target) > 65536:
                        del target[:-65536]
                    if on_line:
                        pending[stream].extend(chunk)
                        while b"\n" in pending[stream]:
                            line, _, rest = pending[stream].partition(b"\n")
                            pending[stream] = bytearray(rest)
                            on_line(line.decode("utf-8", "replace").strip())
                        if len(pending[stream]) > 65536:
                            pending[stream].clear()
            process.wait(timeout=max(.1, deadline - time.monotonic()))
            if self.stop.is_set() or (event and event.is_set()):
                raise Cancelled()
            if process.returncode:
                if len(args) > 1 and args[1] == str(THREADS_ADAPTER_PATH):
                    try:
                        code = json.loads(stderr).get("code")
                    except (ValueError, TypeError, AttributeError):
                        code = None
                    errors = {
                        "threads_multiple_videos": ("Use a single-video Threads post.", 422),
                        "threads_no_video": ("No public video found.", 422),
                        "threads_invalid_url": ("Enter a public Threads post URL.", 400),
                        "threads_invalid_media": ("Unsupported Threads media.", 422),
                        "threads_identity_mismatch": ("Threads post could not be verified.", 422),
                    }
                    message, status = errors.get(code, ("Public Threads video unavailable.", 422))
                    raise EngineError(message, code if code in errors else "threads_unavailable", status)
                detail = stderr.decode("utf-8", "replace").lower()
                if any(s in detail for s in ("sign in", "log in", "login", "private video", "members-only", "age-restricted", "login_required")):
                    raise EngineError("Video requires sign-in or permission.", "restricted_video", 422)
                if any(s in detail for s in ("rate limit", "rate-limit", "too many requests", "http error 429")):
                    raise EngineError("Platform is busy. Try again later.", "platform_rate_limit", 429)
                if "no space left" in detail:
                    raise EngineError("Not enough disk space.", "disk_full", 507)
                if any(s in detail for s in ("unavailable", "not available", "removed")):
                    raise EngineError("Video is unavailable.", "unavailable_video", 422)
                if args[0] == self.tools["yt-dlp"] and "--skip-download" in args:
                    raise EngineError("Video unavailable. Login or access may be required.", "unavailable_video", 422)
                raise EngineError("Processing failed. Check the link and try again.", "tool_failed", 502)
            return bytes(stdout)
        finally:
            self._terminate(process)
            selector.close()
            process.stdout.close()
            process.stderr.close()
            with self.lock:
                if self.processes.get(key) is process:
                    self.processes.pop(key, None)
            if directory and stderr and not getattr(self, "public_mode", False):
                # Private diagnostic tail only; never return subprocess text over HTTP.
                log = directory / "tool.log"
                with contextlib.suppress(OSError):
                    with os.fdopen(_private_file(log, os.O_WRONLY | os.O_CREAT | os.O_TRUNC), "wb") as handle:
                        handle.write(stderr[-65536:])

    def _check_space(self, directory: Path | None = None):
        if shutil.disk_usage(self.config.data_dir).free < self.config.min_free_bytes:
            raise EngineError("Not enough disk space.", "disk_full", 507)
        total = 0
        for job_dir in self.jobs_dir.iterdir():
            if job_dir.is_symlink() or not job_dir.is_dir():
                continue
            job_bytes = 0
            for path in job_dir.rglob("*"):
                if path.is_file() and not path.is_symlink():
                    try:
                        size = path.stat().st_size
                    except FileNotFoundError:
                        continue
                    job_bytes += size
                    if directory == job_dir and path.suffix != ".log" and size > self.config.max_file_bytes:
                        raise EngineError("File exceeds the size limit.", "file_size_limit")
            # During remux/encode both input and output exist; bound scratch as well.
            if directory == job_dir and job_bytes > self.config.max_file_bytes * 2 + 1048576:
                raise EngineError("Temporary storage limit exceeded.", "file_size_limit")
            total += job_bytes
        if total >= self.config.max_storage_bytes:
            raise EngineError("Storage is full. Try again later.", "storage_full", 507)

    def _remove_job_files(self, job_id: str, keep_final: bool = False):
        with contextlib.suppress(OSError, EngineError):
            directory = self._directory(job_id)
            if not directory.exists():
                return
            for item in directory.iterdir():
                if item.name == "tool.log" or (keep_final and item.name == "video.mp4"):
                    continue
                if item.is_dir() and not item.is_symlink():
                    shutil.rmtree(item)
                else:
                    item.unlink(missing_ok=True)

    def cleanup(self):
        with self.lock:
            now = time.time()
            rows = self.db.execute("SELECT id FROM jobs WHERE COALESCE(finished_epoch,CAST(strftime('%s',updated_at) AS REAL),created_epoch)<? AND status IN ('completed','cancelled','failed')", (now - self.config.ttl_seconds,)).fetchall()
            for row in rows:
                if row["id"] in self.cancel_events:
                    continue
                try:
                    directory = self._directory(row["id"])
                    if directory.exists():
                        shutil.rmtree(directory)
                    self.db.execute("DELETE FROM jobs WHERE id=?", (row["id"],))
                except OSError:
                    continue
            self.db.execute("DELETE FROM metadata WHERE expires<?", (now,))
            self.db.execute("DELETE FROM sessions WHERE expires<?", (now,))

    def _housekeep(self):
        while not self.stop.wait(min(60, self.config.ttl_seconds)):
            with contextlib.suppress(Exception):
                self.cleanup()


def _number(value: Any) -> float:
    try:
        result = float(value)
        return result if math.isfinite(result) else 0
    except (ValueError, TypeError, OverflowError):
        return 0


def is_faststart_mp4(path: Path) -> bool:
    """Check top-level MP4 atoms without reading the media payload into memory."""
    size = path.stat().st_size
    saw_ftyp = saw_moov = False
    with path.open("rb") as handle:
        offset = 0
        while offset + 8 <= size:
            handle.seek(offset)
            length, kind = struct.unpack(">I4s", handle.read(8))
            header = 8
            if length == 1:
                extra = handle.read(8)
                if len(extra) != 8:
                    return False
                length, header = struct.unpack(">Q", extra)[0], 16
            if length == 0:
                length = size - offset
            if length < header or offset + length > size:
                return False
            if kind == b"ftyp":
                saw_ftyp = True
            if kind == b"moov":
                saw_moov = True
            if kind == b"mdat":
                return saw_ftyp and saw_moov
            offset += length
    return False
