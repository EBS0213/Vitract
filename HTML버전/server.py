"""clipsave HTTP service. Run with one worker; see 서버배포안내.txt.

Local: python3 server.py --host 127.0.0.1 --port 8766
Public: PUBLIC_MODE=1 PUBLIC_ORIGIN=https://video.example.com
        uvicorn server:app --host 0.0.0.0 --port 8766 --workers 1 --no-proxy-headers
"""
from __future__ import annotations

import argparse
from collections import deque
from contextlib import asynccontextmanager
import hmac
import ipaddress
import os
from pathlib import Path
import time
from typing import Literal
from urllib.parse import urlsplit

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.concurrency import run_in_threadpool

from engine import Config, Engine, EngineError, VERSION, _limit
from platforms import PlatformError, THUMBNAIL_CSP, capabilities

COOKIE = "videosaver_session"
WEB_DIR = Path(__file__).resolve().parent


def normalize_origin(value: str) -> str:
    try:
        parts = urlsplit(value)
        if parts.scheme not in ("http", "https") or not parts.hostname or parts.username is not None or parts.password is not None or parts.query or parts.fragment or parts.path not in ("", "/"):
            raise ValueError()
        port = parts.port
        host = parts.hostname.lower()
        host = "[" + host + "]" if ":" in host else host
        default = 443 if parts.scheme == "https" else 80
        return f"{parts.scheme}://{host}" + (f":{port}" if port and port != default else "")
    except ValueError:
        raise RuntimeError("PUBLIC_ORIGIN must be a single http(s) origin without a path") from None


def loopback(value: str) -> bool:
    try:
        return ipaddress.ip_address(value).is_loopback
    except ValueError:
        return value.lower() == "localhost"


class Body(BaseModel):
    model_config = ConfigDict(extra="forbid")


class LoginBody(Body):
    password: StrictStr = Field(max_length=1024)


class InspectBody(Body):
    url: StrictStr = Field(min_length=1, max_length=2048)
    platform: Literal["youtube", "instagram", "threads", "x", "facebook"] = "youtube"


class JobBody(Body):
    metadata_token: StrictStr = Field(min_length=16, max_length=128)
    quality: StrictInt


def error(message: str, code: str, status: int) -> JSONResponse:
    return JSONResponse({"error": message, "code": code}, status_code=status)


def create_app(config: Config | None = None) -> FastAPI:
    config = config or Config.from_env()
    password = os.environ.get("APP_PASSWORD", "")
    public_mode = os.environ.get("PUBLIC_MODE", "") == "1"
    origin_setting = os.environ.get("PUBLIC_ORIGIN", "")
    public_origin = normalize_origin(origin_setting) if origin_setting else None
    if public_mode and not public_origin:
        raise RuntimeError("PUBLIC_MODE=1 requires PUBLIC_ORIGIN")
    if public_origin and not (password or public_mode):
        raise RuntimeError("PUBLIC_ORIGIN requires PUBLIC_MODE=1 or APP_PASSWORD")
    try:
        trusted_proxies = [ipaddress.ip_network(value.strip(), strict=False) for value in os.environ.get("VIDEO_TRUSTED_PROXIES", "").split(",") if value.strip()]
    except ValueError:
        raise RuntimeError("VIDEO_TRUSTED_PROXIES must contain comma-separated IP addresses or CIDRs") from None
    rate_limits = {"session": _limit("VIDEO_SESSION_RATE_PER_MINUTE", 60), "inspect": _limit("VIDEO_INSPECT_RATE_PER_MINUTE", 10), "job": _limit("VIDEO_JOB_RATE_PER_MINUTE", 5), "login": 10}
    engine = Engine(config)
    # Only keyed HMACs of peer/client IPs live here; no raw IP is written to SQLite.
    rate_buckets: dict[tuple[str, str], deque] = {}

    def check_rate(key: str, action: str):
        # Existing local/protected workflows keep their prior behavior except login.
        if not public_mode and action != "login":
            return
        now = time.monotonic()
        bucket_key = (key, action)
        with engine.lock:
            if bucket_key not in rate_buckets:
                for expired in [k for k, q in rate_buckets.items() if not q or q[-1] <= now - 60]:
                    del rate_buckets[expired]
                if len(rate_buckets) >= 4096:
                    raise EngineError("Too many requests. Try again in a minute.", "rate_limit", 429)
                rate_buckets[bucket_key] = deque()
            history = rate_buckets[bucket_key]
            while history and history[0] <= now - 60:
                history.popleft()
            if len(history) >= rate_limits[action]:
                raise EngineError("Too many requests. Try again in a minute.", action + "_rate_limit", 429)
            history.append(now)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        try:
            await run_in_threadpool(engine.start)
            engine.bind_auth_context(password, public_origin, public_mode)
            yield
        finally:
            await run_in_threadpool(engine.close)

    app = FastAPI(title="clipsave", version=VERSION, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.engine = engine
    app.state.public_origin = public_origin
    app.state.public_mode = public_mode

    def session_payload(session: dict) -> dict:
        return {"authenticated": bool(session["authenticated"]), "csrf_token": session["csrf"], "requires_password": bool(password), "public_mode": public_mode, "platforms": capabilities(), "limits": {"max_duration_seconds": config.max_duration_seconds, "max_file_bytes": config.max_file_bytes}, "readiness": engine.readiness, "version": VERSION}

    def set_cookie(response, token: str, request: Request):
        response.set_cookie(COOKIE, token, httponly=True, secure=bool(public_origin and public_origin.startswith("https://")) or request.url.scheme == "https", samesite="strict", path="/")

    def current(request: Request, authenticated: bool = True) -> dict:
        session = request.state.session
        if authenticated and not session["authenticated"]:
            raise EngineError("Sign in to continue.", "authentication_required", 401)
        return session

    @app.middleware("http")
    async def boundary(request: Request, call_next):
        # This check also protects uvicorn server:app when the operator accidentally
        # binds 0.0.0.0 without the public-mode environment. Never trust forwarded
        # headers to decide the permitted origin.
        peer = request.client.host if request.client else ""
        try:
            peer_ip = ipaddress.ip_address(peer)
        except ValueError:
            return error("Invalid client address.", "invalid_client", 403)
        effective_ip = peer_ip
        forwarded = request.headers.getlist("x-forwarded-for")
        if forwarded:
            # Our documented proxy overwrites, rather than appends, this header.
            # Uvicorn's own proxy interpretation must remain disabled.
            if not any(peer_ip in network for network in trusted_proxies):
                return error("Untrusted proxy.", "untrusted_proxy_header", 403)
            try:
                if len(forwarded) != 1 or "," in forwarded[0]:
                    raise ValueError()
                effective_ip = ipaddress.ip_address(forwarded[0].strip())
            except ValueError:
                return error("Invalid proxy address.", "invalid_proxy_header", 403)
        request.state.client_key = engine._hash(str(effective_ip))
        host_headers = request.headers.getlist("host")
        if len(host_headers) != 1:
            return error("Invalid server address.", "invalid_host", 403)
        host_value = host_headers[0]
        try:
            host_parts = urlsplit("http://" + host_value)
            hostname = host_parts.hostname
            if not hostname or host_parts.username is not None or host_parts.password is not None or host_parts.path or host_parts.query or host_parts.fragment:
                raise ValueError()
            host_parts.port
            request_origin = normalize_origin(f"{request.url.scheme}://{host_value}")
        except (ValueError, RuntimeError):
            return error("Invalid server address.", "invalid_host", 403)
        if public_origin:
            expected_host = urlsplit(public_origin).netloc
            actual_host = urlsplit(normalize_origin(urlsplit(public_origin).scheme + "://" + host_value)).netloc
            if actual_host != expected_host:
                return error("Invalid server address.", "invalid_host", 403)
            allowed_origin = public_origin
        else:
            client = request.client.host if request.client else ""
            if hostname.lower() not in ("localhost", "127.0.0.1", "::1") or not loopback(client):
                return error("Public access is not configured.", "public_configuration_required", 403)
            allowed_origin = request_origin
        if request.method not in ("GET", "HEAD", "POST"):
            return error("Method not allowed.", "method_not_allowed", 405)
        if request.method == "POST":
            origins = request.headers.getlist("origin")
            if len(origins) != 1 or origins[0] != allowed_origin:
                return error("Invalid request origin.", "invalid_origin", 403)
            if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
                return error("Send a JSON request.", "invalid_content_type", 415)
            try:
                length = int(request.headers.get("content-length", "0"))
            except ValueError:
                return error("Invalid request size.", "invalid_request", 400)
            if length < 0 or length > 8192:
                return error("Request is too large.", "request_too_large", 413)
            # Stream the bounded body so missing Content-Length/chunked requests
            # cannot bypass the limit. Reinsert the bytes for FastAPI validation.
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > 8192:
                    return error("Request is too large.", "request_too_large", 413)
            request._body = bytes(body)
        token_to_set = None
        if request.url.path.startswith("/api/") and request.url.path != "/api/health":
            try:
                session, token_to_set = engine.session(request.cookies.get(COOKIE), not bool(password), lambda: check_rate(request.state.client_key, "session"))
            except EngineError as exc:
                return error(exc.message, exc.code, exc.status)
            request.state.session = session
            if request.method == "POST":
                tokens = request.headers.getlist("x-csrf-token")
                if len(tokens) != 1 or not hmac.compare_digest(tokens[0].encode("utf-8"), session["csrf"].encode("ascii")):
                    response = error("Session check expired. Refresh the page.", "invalid_csrf", 403)
                    if token_to_set:
                        set_cookie(response, token_to_set, request)
                    return response
        response = await call_next(request)
        # A successful login/logout owns the rotated cookie, not the initial one.
        if token_to_set and not getattr(request.state, "rotated_cookie", False):
            set_cookie(response, token_to_set, request)
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data: " + THUMBNAIL_CSP + "; connect-src 'self'; object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'"
        response.headers["Cache-Control"] = "no-store"
        return response

    @app.exception_handler(PlatformError)
    async def engine_error(request: Request, exc: PlatformError):
        return error(exc.message, exc.code, exc.status)

    @app.exception_handler(RequestValidationError)
    async def validation_error(request: Request, exc: RequestValidationError):
        return error("Invalid request.", "invalid_request", 422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException):
        return error("Not found." if exc.status_code == 404 else "Request failed.", "not_found" if exc.status_code == 404 else "http_error", exc.status_code)

    @app.exception_handler(Exception)
    async def unexpected_error(request: Request, exc: Exception):
        return error("Server error. Try again later.", "internal_error", 500)

    @app.get("/api/health")
    def health():
        return {"status": "ok", "ready": engine.readiness["ready"], "application": "videosaver"}

    @app.get("/api/session")
    def session(request: Request):
        return session_payload(current(request, authenticated=False))

    @app.post("/api/login")
    def login(request: Request, body: LoginBody):
        session = current(request, authenticated=False)
        check_rate(request.state.client_key, "login")
        if password and not hmac.compare_digest(body.password.encode("utf-8"), password.encode("utf-8")):
            raise EngineError("Incorrect password.", "invalid_password", 401)
        check_rate(request.state.client_key, "session")
        updated, raw = engine.rotate_session(session["id"], True)
        response = JSONResponse(session_payload(updated))
        set_cookie(response, raw, request)
        request.state.rotated_cookie = True
        return response

    @app.post("/api/logout")
    def logout(request: Request, body: Body):
        session = current(request, authenticated=False)
        check_rate(request.state.client_key, "session")
        updated, raw = engine.rotate_session(session["id"], not bool(password))
        response = JSONResponse(session_payload(updated))
        set_cookie(response, raw, request)
        request.state.rotated_cookie = True
        return response

    @app.post("/api/inspect")
    def inspect(request: Request, body: InspectBody):
        owner = current(request)["id"]
        check_rate(request.state.client_key, "inspect")
        return engine.inspect(owner, body.url, body.platform)

    @app.post("/api/jobs")
    def create_job(request: Request, body: JobBody):
        owner = current(request)["id"]
        check_rate(request.state.client_key, "job")
        return engine.create_job(owner, body.metadata_token, body.quality)

    @app.get("/api/jobs")
    def list_jobs(request: Request):
        if public_mode:
            raise EngineError("Not found.", "not_found", 404)
        return {"jobs": engine.list_jobs(current(request)["id"])}

    @app.get("/api/jobs/{job_id}")
    def get_job(request: Request, job_id: str):
        return engine.get_job(job_id, current(request)["id"])

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(request: Request, job_id: str, body: Body):
        return engine.cancel_job(job_id, current(request)["id"])

    @app.get("/api/jobs/{job_id}/file")
    def download(request: Request, job_id: str):
        path, filename = engine.file_for_job(job_id, current(request)["id"])
        return FileResponse(path, media_type="video/mp4", filename=filename, content_disposition_type="attachment", headers={"Cache-Control": "private, no-store"})

    @app.get("/")
    def index():
        path = WEB_DIR / "index.html"
        if not path.is_file():
            raise EngineError("Page files are missing.", "frontend_missing", 503)
        return FileResponse(path, media_type="text/html")

    @app.get("/app.js")
    def javascript():
        return FileResponse(WEB_DIR / "app.js", media_type="text/javascript")

    @app.get("/styles.css")
    def stylesheet():
        return FileResponse(WEB_DIR / "styles.css", media_type="text/css")

    return app


app = create_app()


def main():
    parser = argparse.ArgumentParser(description="clipsave 웹 서버")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--data-dir", default=None)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port must be between 1 and 65535")
    if not loopback(args.host) and not (os.environ.get("PUBLIC_ORIGIN") and (os.environ.get("APP_PASSWORD") or os.environ.get("PUBLIC_MODE") == "1")):
        parser.error("외부 접속에는 PUBLIC_ORIGIN과 PUBLIC_MODE=1 또는 APP_PASSWORD가 필요합니다.")
    import uvicorn
    application = create_app(Config.from_env(args.data_dir))
    uvicorn.run(application, host=args.host, port=args.port, workers=1, proxy_headers=False, access_log=False, server_header=False)


if __name__ == "__main__":
    main()
