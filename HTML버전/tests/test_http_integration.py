"""Offline HTTP contracts plus real FFmpeg/FFprobe conversion verification."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
import json
import os
from pathlib import Path
import shutil
import signal
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest

import httpx

TESTS = Path(__file__).resolve().parent
WEB = TESTS.parent
FIXTURE = TESTS / "fixtures/synthetic-av1-opus.mp4"
GOOD = "BaW_jenozKc"
OTHER = "aqz-KE-bpKQ"
THIRD = "HANG0000001"
PLATFORM_URLS = {
    "youtube": (GOOD, f"https://www.youtube.com/watch?v={GOOD}"),
    "instagram": ("Chunk8-jurw", "https://www.instagram.com/reel/Chunk8-jurw/"),
    "threads": ("ABCDEFG1234", "https://www.threads.com/@example/post/ABCDEFG1234"),
    "x": ("1790000000000000001", "https://x.com/example/status/1790000000000000001"),
    "facebook": ("10153231379946729", "https://www.facebook.com/watch/?v=10153231379946729"),
}
TERMINAL = {"completed", "cancelled", "failed"}


def tool(name):
    configured = os.environ.get("VIDEO_" + name.upper())
    bundled = WEB.parent / "build/영상저장.app/Contents/Resources/tools" / name
    delivered = Path.home() / "Documents/영상제작 APP/macOS버전/build/영상저장.app/Contents/Resources/tools" / name
    value = configured or (str(bundled) if bundled.is_file() else str(delivered) if delivered.is_file() else shutil.which(name))
    if not value or not os.access(value, os.X_OK):
        raise RuntimeError(f"Executable {name} is required for offline integration tests")
    return str(Path(value).resolve())


def alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False


def wait_until(check, timeout=12, message="condition"):
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = check()
        if last:
            return last
        time.sleep(0.05)
    raise AssertionError(f"Timed out waiting for {message}; last value={last!r}")


class Server:
    serial = 0

    def __init__(self, *, password=None, public=False, limits=None):
        Server.serial += 1
        self.number = Server.serial
        (TESTS / "results").mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(prefix="videosaver-web-test-")
        self.root = Path(self.temp.name)
        self.data = self.root / "data"
        self.control = self.root / "control.json"
        self.set_modes({})
        with socket.socket() as bound:
            bound.bind(("127.0.0.1", 0))
            self.port = bound.getsockname()[1]
        self.url = f"http://127.0.0.1:{self.port}"
        self.env = dict(os.environ)
        for key in list(self.env):
            if key.startswith("VIDEO_") or key in ("APP_PASSWORD", "PUBLIC_ORIGIN", "PUBLIC_MODE"):
                self.env.pop(key, None)
        self.env.update({
            "VIDEO_TEST_CONTROL": str(self.control), "VIDEO_YTDLP": str(TESTS / "fake_yt_dlp.py"),
            "VIDEO_FFMPEG": tool("ffmpeg"), "VIDEO_FFPROBE": tool("ffprobe"), "VIDEO_NODE": tool("node"),
            "VIDEO_MAX_DURATION_SECONDS": "30", "VIDEO_MAX_FILE_BYTES": "1048576",
            "VIDEO_MAX_STORAGE_BYTES": "134217728", "VIDEO_MIN_FREE_BYTES": "0",
            "VIDEO_JOB_TIMEOUT_SECONDS": "20", "VIDEO_METADATA_TIMEOUT_SECONDS": "8",
            "VIDEO_MAX_ACTIVE_JOBS": "8", "VIDEO_MAX_OWNER_JOBS": "2",
        })
        if password is not None:
            self.env["APP_PASSWORD"] = password
            self.env["PUBLIC_ORIGIN"] = self.url
        if public:
            self.env["PUBLIC_MODE"] = "1"
            self.env["PUBLIC_ORIGIN"] = self.url
        self.env.update({key: str(value) for key, value in (limits or {}).items()})
        self.process = None
        self.log = None
        self.start()

    def set_modes(self, videos, default=None):
        value = {"fixture_path": str(FIXTURE), "videos": videos, "default": default or {}}
        self.control.write_text(json.dumps(value), encoding="utf-8")

    def start(self):
        self.log = (TESTS / "results" / f"server-{self.number}.log").open("ab")
        self.process = subprocess.Popen(
            [sys.executable, str(TESTS / "server_fixture.py"), "--host", "127.0.0.1", "--port", str(self.port),
             "--data-dir", str(self.data)], cwd=WEB, env=self.env, stdout=self.log,
            stderr=subprocess.STDOUT, start_new_session=True)

        def ready():
            if self.process.poll() is not None:
                raise AssertionError(f"Test server exited {self.process.returncode}; see results/server-{self.number}.log")
            try:
                return httpx.get(self.url + "/api/health", timeout=0.5).status_code == 200
            except httpx.TransportError:
                return False
        wait_until(ready, timeout=30, message="local HTTP server startup")

    def fixture_pids(self):
        rows = []
        for path in self.root.glob("pids-*.json"):
            try:
                rows.extend(json.loads(path.read_text()).values())
            except (ValueError, OSError):
                pass
        return rows

    def stop(self, abrupt=False):
        if self.process and self.process.poll() is None:
            self.process.kill() if abrupt else self.process.terminate()
            try:
                self.process.wait(timeout=12)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        if self.log:
            self.log.close()
            self.log = None

    def clean_fixture_processes(self):
        # Cleanup only fixture-owned PIDs recorded inside this test's workspace.
        for pid in self.fixture_pids():
            try:
                os.kill(pid, signal.SIGKILL)
            except ProcessLookupError:
                pass

    def close(self):
        self.stop()
        self.clean_fixture_processes()
        self.temp.cleanup()

    def calls(self):
        path = self.root / "calls.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@contextmanager
def server_context(**kwargs):
    server = Server(**kwargs)
    try:
        yield server
    finally:
        server.close()


class Client:
    def __init__(self, server, headers=None):
        self.server = server
        self.http = httpx.Client(base_url=server.url, timeout=15, follow_redirects=False, headers=headers)
        self.refresh()

    def refresh(self):
        response = self.http.get("/api/session")
        response.raise_for_status()
        self.session = response.json()
        self.csrf = self.session["csrf_token"]
        return self.session

    def post(self, path, body=None, *, csrf=True, origin=True, origin_value=None):
        headers = {}
        if csrf:
            headers["X-CSRF-Token"] = self.csrf
        if origin:
            headers["Origin"] = origin_value or self.server.url
        return self.http.post(path, json=body or {}, headers=headers)

    def inspect(self, video_id=GOOD, url=None, platform=None):
        response = self.post("/api/inspect", {"url": url or f"https://youtu.be/{video_id}", **({"platform": platform} if platform else {})})
        if response.status_code != 200:
            raise AssertionError(f"Inspect failed: {response.status_code} {response.text[:500]}")
        value = response.json()
        assert value.get("metadata_token") and isinstance(value.get("qualities"), list), value
        return value

    def create(self, metadata=None, quality=360, video_id=GOOD):
        metadata = metadata or self.inspect(video_id)
        response = self.post("/api/jobs", {"metadata_token": metadata["metadata_token"], "quality": quality})
        if response.status_code not in (200, 201, 202):
            raise AssertionError(f"Create failed: {response.status_code} {response.text[:500]}")
        value = response.json()
        assert value.get("id") and value.get("status"), value
        return value

    def job(self, ident):
        response = self.http.get(f"/api/jobs/{ident}")
        response.raise_for_status()
        return response.json()

    def terminal(self, ident, timeout=25, samples=None):
        def done():
            job = self.job(ident)
            if samples is not None:
                samples.append(job)
            return job if job["status"] in TERMINAL else None
        return wait_until(done, timeout, f"terminal job {ident}")

    def cancel(self, ident):
        return self.post(f"/api/jobs/{ident}/cancel")


class WebIntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = Server()

    @classmethod
    def tearDownClass(cls):
        cls.server.close()

    def setUp(self):
        self.server.set_modes({})
        self.client = Client(self.server)
        self.clients = [self.client]

    def tearDown(self):
        for client in self.clients:
            try:
                response = client.http.get("/api/jobs")
                for job in response.json().get("jobs", []):
                    if job["status"] not in TERMINAL:
                        client.cancel(job["id"])
                        client.terminal(job["id"], timeout=12)
            finally:
                client.http.close()

    def second_client(self):
        client = Client(self.server)
        self.clients.append(client)
        return client

    def test_01_url_allowlist_short_links_and_playlist_suppression(self):
        accepted = [f"https://youtube.com/shorts/{GOOD}?si=tracking",
                    f"https://www.youtube.com/watch?v={GOOD}&list=PLignored&index=2",
                    f"  youtu.be/{GOOD}?t=4  "]
        for url in accepted:
            info = self.client.inspect(url=url)
            self.assertIn(360, info["qualities"])
            call = self.server.calls()[-1]
            self.assertEqual(call["args"][-1], f"https://www.youtube.com/watch?v={GOOD}")
            self.assertIn("--no-playlist", call["args"])
            self.assertIn("--ignore-config", call["args"])
        rejected = [f"https://youtube.com.evil.example/watch?v={GOOD}",
                    f"https://evilyoutube.com/watch?v={GOOD}", f"https://evil@youtube.com/watch?v={GOOD}",
                    f"https://youtube.com:8080/watch?v={GOOD}", "https://youtube.com/playlist?list=PL123",
                    "https://youtube.com/redirect?q=http://127.0.0.1", "file:///etc/passwd",
                    f"https://127.0.0.1/watch?v={GOOD}", f"https://youtube.com/watch?v={GOOD}&v={OTHER}",
                    f"https://youtu.be/{GOOD}%0A--exec", "http://169.254.169.254/latest/meta-data/"]
        before = len(self.server.calls())
        for url in rejected:
            with self.subTest(url=url):
                response = self.client.post("/api/inspect", {"url": url})
                self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(len(self.server.calls()), before, "Rejected URLs reached the downloader")

    def test_02_csrf_and_origin_required_before_tool_invocation(self):
        body = {"url": f"https://youtu.be/{GOOD}"}
        before = len(self.server.calls())
        for options in ({"csrf": False}, {"origin": False}, {"origin_value": "https://evil.invalid"}):
            response = self.client.post("/api/inspect", body, **options)
            self.assertEqual(response.status_code, 403, response.text)
        self.assertEqual(len(self.server.calls()), before)

    def test_03_metadata_tokens_are_session_bound_and_quality_validated(self):
        first = self.client.inspect(GOOD)
        second = self.client.inspect(OTHER)
        self.assertNotEqual(first["metadata_token"], second["metadata_token"])
        stranger = self.second_client()
        for client, token, quality in ((stranger, first["metadata_token"], 360),
                                       (self.client, "tampered-token", 360),
                                       (self.client, first["metadata_token"], 99999),
                                       (self.client, first["metadata_token"], -1)):
            response = client.post("/api/jobs", {"metadata_token": token, "quality": quality})
            self.assertIn(response.status_code, (400, 403, 404, 409, 422), response.text)
        # A later inspection must never silently replace the earlier token's video.
        job = self.client.create(first)
        done = self.client.terminal(job["id"])
        self.assertEqual(done["status"], "completed", done)
        calls = [row for row in self.server.calls() if "-f" in row["args"]]
        self.assertEqual(calls[-1]["id"], GOOD)

    def test_04_real_av1_opus_conversion_progress_and_private_download(self):
        self.server.set_modes({GOOD: {"progress_pause_seconds": 0.3,
                                     "title": '<script>alert(1)</script> / 제목 👨‍👩‍👧‍👦'}})
        job = self.client.create()
        samples = []
        done = self.client.terminal(job["id"], samples=samples)
        self.assertEqual(done["status"], "completed", done)
        self.assertTrue(any(row["status"] == "downloading" and row.get("progress", 0) > 0 for row in samples), samples)
        response = self.client.http.get(f"/api/jobs/{job['id']}/file")
        self.assertEqual(response.status_code, 200, response.text[:200] if response.status_code != 200 else "")
        self.assertIn("video/mp4", response.headers.get("content-type", ""))
        self.assertIn("attachment", response.headers.get("content-disposition", ""))
        output = self.server.root / "downloaded-result.mp4"
        output.write_bytes(response.content)
        probe = subprocess.run([tool("ffprobe"), "-v", "error", "-show_entries",
                                "stream=codec_name,codec_type,pix_fmt,width,height:format=duration,format_name",
                                "-of", "json", str(output)], capture_output=True, text=True, timeout=10, check=True)
        info = json.loads(probe.stdout)
        video = next(row for row in info["streams"] if row["codec_type"] == "video")
        audio = next(row for row in info["streams"] if row["codec_type"] == "audio")
        self.assertEqual((video["codec_name"], video["pix_fmt"], audio["codec_name"]), ("h264", "yuv420p", "aac"))
        self.assertEqual(video["height"], 180, "Conversion should preserve the available lower-resolution source")
        self.assertLessEqual(video["height"], 360)
        self.assertAlmostEqual(float(info["format"]["duration"]), 1.2, delta=0.15)
        self.assertIn("mp4", info["format"]["format_name"])
        stranger = self.second_client()
        for path in (f"/api/jobs/{job['id']}", f"/api/jobs/{job['id']}/file"):
            self.assertIn(stranger.http.get(path).status_code, (403, 404))
        self.assertIn(stranger.cancel(job["id"]).status_code, (403, 404))
        self.assertNotIn(job["id"], [row["id"] for row in stranger.http.get("/api/jobs").json()["jobs"]])
        self.assertNotIn(str(self.server.data), json.dumps(done), "Server filesystem path leaked to browser")
        args = [row["args"] for row in self.server.calls() if "-f" in row["args"]][-1]
        self.assertIn("bv*[height<=360]+ba/b[height<=360]", args)

    def test_05_repeated_save_keeps_previous_download_bytes(self):
        info = self.client.inspect()
        first = self.client.create(info)
        self.assertEqual(self.client.terminal(first["id"])["status"], "completed")
        first_bytes = self.client.http.get(f"/api/jobs/{first['id']}/file").content
        second = self.client.create(info)
        self.assertEqual(self.client.terminal(second["id"])["status"], "completed")
        self.assertNotEqual(first["id"], second["id"])
        self.assertEqual(self.client.http.get(f"/api/jobs/{first['id']}/file").content, first_bytes)

    def test_06_bad_metadata_live_drm_and_duration_recover(self):
        for settings in ({"mode": "malformed"}, {"mode": "live"}, {"mode": "upcoming"},
                         {"mode": "drm"}, {"mode": "fail"}, {"duration": 3600}):
            with self.subTest(settings=settings):
                self.server.set_modes({GOOD: settings})
                response = self.client.post("/api/inspect", {"url": f"https://youtu.be/{GOOD}"})
                self.assertIn(response.status_code, (400, 413, 422, 502), response.text)
        self.server.set_modes({})
        self.client.inspect()

    def test_07_cancel_kills_stubborn_process_group_and_never_publishes(self):
        self.server.set_modes({GOOD: {"mode": "hang"}})
        job = self.client.create()
        path = self.server.root / f"pids-{GOOD}.json"
        wait_until(path.exists, message="stubborn descendant spawn")
        pids = json.loads(path.read_text())
        self.assertIn(self.client.cancel(job["id"]).status_code, (200, 202))
        done = self.client.terminal(job["id"], timeout=12)
        self.assertEqual(done["status"], "cancelled", done)
        wait_until(lambda: all(not alive(pid) for pid in pids.values()), timeout=10, message="entire cancelled process group exit")
        self.assertIn(self.client.http.get(f"/api/jobs/{job['id']}/file").status_code, (404, 409))

    def test_08_two_workers_queue_and_queued_cancel(self):
        self.server.set_modes({ident: {"mode": "hang"} for ident in (GOOD, OTHER, THIRD)})
        first = self.client.create(video_id=GOOD)
        second = self.client.create(video_id=OTHER)
        wait_until(lambda: (self.server.root / f"pids-{GOOD}.json").exists() and
                   (self.server.root / f"pids-{OTHER}.json").exists(), message="two worker slots occupied")
        stranger = self.second_client()
        third = stranger.create(video_id=THIRD)
        time.sleep(0.15)
        self.assertEqual(stranger.job(third["id"])["status"], "queued")
        self.assertFalse((self.server.root / f"pids-{THIRD}.json").exists())
        self.assertIn(stranger.cancel(third["id"]).status_code, (200, 202))
        self.assertEqual(stranger.terminal(third["id"])["status"], "cancelled")
        for job in (first, second):
            self.client.cancel(job["id"])
            self.assertEqual(self.client.terminal(job["id"])["status"], "cancelled")

    def test_09_password_authentication_and_login_csrf(self):
        with server_context(password="offline-test-password-123") as server:
            client = Client(server)
            try:
                self.assertFalse(client.session["authenticated"])
                self.assertEqual(client.post("/api/inspect", {"url": f"https://youtu.be/{GOOD}"}).status_code, 401)
                self.assertEqual(client.post("/api/login", {"password": "offline-test-password-123"}, csrf=False).status_code, 403)
                self.assertEqual(client.post("/api/login", {"password": "wrong"}).status_code, 401)
                response = client.post("/api/login", {"password": "offline-test-password-123"})
                self.assertEqual(response.status_code, 200, response.text)
                client.refresh()
                self.assertTrue(client.session["authenticated"])
                client.inspect()
            finally:
                client.http.close()

    def test_10_file_size_limit_prevents_publication(self):
        with server_context(limits={"VIDEO_MAX_FILE_BYTES": "1024"}) as server:
            client = Client(server)
            try:
                metadata = client.inspect()
                response = client.post("/api/jobs", {"metadata_token": metadata["metadata_token"], "quality": 360})
                if response.status_code in (400, 413, 422):
                    return
                self.assertIn(response.status_code, (200, 201, 202), response.text)
                job = response.json()
                self.assertEqual(client.terminal(job["id"])["status"], "failed")
                self.assertIn(client.http.get(f"/api/jobs/{job['id']}/file").status_code, (404, 409))
            finally:
                client.http.close()

    def test_11_restart_marks_interrupted_work_failed(self):
        with server_context() as server:
            client = Client(server)
            try:
                server.set_modes({GOOD: {"mode": "hang"}})
                job = client.create()
                wait_until((server.root / f"pids-{GOOD}.json").exists, message="job before simulated abrupt restart")
                server.stop(abrupt=True)
                # An uncatchable server crash is outside graceful process cleanup.
                # The harness reaps its own fixture children before state recovery.
                server.clean_fixture_processes()
                server.start()
                done = client.terminal(job["id"], timeout=8)
                self.assertEqual(done["status"], "failed", done)
                self.assertIn(client.http.get(f"/api/jobs/{job['id']}/file").status_code, (404, 409))
            finally:
                client.http.close()

    def test_12_total_storage_limit_preserves_existing_completed_file(self):
        with server_context(limits={"VIDEO_MAX_FILE_BYTES": "100000", "VIDEO_MAX_STORAGE_BYTES": "100000"}) as server:
            client = Client(server)
            try:
                first = client.create()
                self.assertEqual(client.terminal(first["id"])["status"], "completed")
                url = f"/api/jobs/{first['id']}/file"
                original = client.http.get(url).content
                metadata = client.inspect(OTHER)
                response = client.post("/api/jobs", {"metadata_token": metadata["metadata_token"], "quality": 360})
                if response.status_code in (200, 201, 202):
                    second = response.json()
                    self.assertEqual(client.terminal(second["id"])["status"], "failed")
                    self.assertIn(client.http.get(f"/api/jobs/{second['id']}/file").status_code, (404, 409))
                else:
                    self.assertIn(response.status_code, (413, 429, 507), response.text)
                self.assertEqual(client.http.get(url).content, original)
            finally:
                client.http.close()

    def test_13_job_deadline_kills_process_group(self):
        with server_context(limits={"VIDEO_JOB_TIMEOUT_SECONDS": "2"}) as server:
            client = Client(server)
            try:
                server.set_modes({GOOD: {"mode": "hang"}})
                job = client.create()
                path = server.root / f"pids-{GOOD}.json"
                wait_until(path.exists, message="timeout fixture process group")
                pids = json.loads(path.read_text())
                done = client.terminal(job["id"], timeout=12)
                self.assertEqual(done["status"], "failed", done)
                wait_until(lambda: all(not alive(pid) for pid in pids.values()), timeout=10, message="deadline process group exit")
                self.assertIn(client.http.get(f"/api/jobs/{job['id']}/file").status_code, (404, 409))
            finally:
                client.http.close()

    def test_14_anonymous_session_churn_preserves_authenticated_owner(self):
        # Exercise the real session allocator; no HTTP or production DB is used.
        sys.path.insert(0, str(WEB))
        try:
            from engine import Config, Engine
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="videosaver-session-churn-") as directory:
            engine = Engine(Config(data_dir=Path(directory)))
            engine.start()
            try:
                # This disposable fixture tests capacity semantics, not disk durability.
                engine.db.execute("PRAGMA synchronous=OFF")
                engine.bind_auth_context("synthetic-password", "https://video.example.test")
                owner, cookie = engine.session(None, True)
                for _ in range(4096):
                    engine.session(None, False)
                current, replacement = engine.session(cookie, False)
                self.assertEqual(current["id"], owner["id"])
                self.assertTrue(current["authenticated"])
                self.assertIsNone(replacement)
                self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 4096)
            finally:
                engine.close()

    def test_15_full_authenticated_capacity_returns_json_without_revoking_owner(self):
        with server_context(password="offline-capacity-password") as server:
            client = Client(server)
            try:
                response = client.post("/api/login", {"password": "offline-capacity-password"})
                self.assertEqual(response.status_code, 200, response.text)
                client.refresh()
                metadata = client.inspect()
                # Populate only this server's temporary fixture DB to reach the boundary.
                now = time.time()
                with sqlite3.connect(server.data / "state.sqlite3") as db:
                    db.execute("DELETE FROM sessions WHERE authenticated=0")
                    count = db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
                    db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", [
                        (f"synthetic-capacity-{i}", f"csrf-{i}", 1, now + i / 10000, now + 3600)
                        for i in range(4096 - count)
                    ])
                with httpx.Client(base_url=server.url, timeout=10) as guest:
                    denied = guest.get("/api/session")
                self.assertEqual(denied.status_code, 503, denied.text)
                self.assertEqual(denied.json()["code"], "session_limit")
                self.assertTrue(client.refresh()["authenticated"])
                job = client.create(metadata=metadata)
                self.assertEqual(client.terminal(job["id"])["status"], "completed")
                self.assertEqual(client.http.get(f"/api/jobs/{job['id']}/file").status_code, 200)
            finally:
                client.http.close()

    def test_16_public_mode_anonymous_save_without_history_or_persistent_cookie(self):
        with server_context(public=True) as server:
            owner, other = Client(server), Client(server)
            try:
                self.assertTrue(owner.session["authenticated"])
                self.assertFalse(owner.session["requires_password"])
                with httpx.Client(base_url=server.url) as fresh:
                    cookie = fresh.get("/api/session").headers["set-cookie"].lower()
                self.assertIn("httponly", cookie)
                self.assertIn("samesite=strict", cookie)
                self.assertNotIn("max-age", cookie)
                self.assertNotIn("expires=", cookie)
                self.assertEqual(owner.http.get("/api/jobs").status_code, 404)
                self.assertEqual(owner.http.get("/api/session", headers={"Host": "spoof.example"}).status_code, 403)
                job = owner.create()
                self.assertEqual(owner.terminal(job["id"])["status"], "completed")
                self.assertEqual(other.http.get(f"/api/jobs/{job['id']}").status_code, 404)
                self.assertEqual(other.http.get(f"/api/jobs/{job['id']}/file").status_code, 404)
                first = owner.http.get(f"/api/jobs/{job['id']}/file")
                self.assertEqual(first.status_code, 200)
                # Browser auto-save can fail; a same-session retry remains available.
                self.assertEqual(owner.http.get(f"/api/jobs/{job['id']}/file").content, first.content)
                self.assertEqual(owner.http.get("/api/jobs").status_code, 404)
            finally:
                owner.http.close()
                other.http.close()

    def test_17_terminal_ttl_starts_at_completion_not_job_creation(self):
        with server_context(public=True, limits={"VIDEO_TTL_SECONDS": "2"}) as server:
            client = Client(server)
            try:
                server.set_modes({GOOD: {"progress_pause_seconds": 1.0}})
                started = time.time()
                job = client.create()
                self.assertEqual(client.terminal(job["id"])["status"], "completed")
                self.assertGreater(time.time() - started, 2)
                url = f"/api/jobs/{job['id']}/file"
                self.assertEqual(client.http.get(url).status_code, 200)
                with sqlite3.connect(server.data / "state.sqlite3") as db:
                    created, finished = db.execute("SELECT created_epoch,finished_epoch FROM jobs WHERE id=?", (job["id"],)).fetchone()
                self.assertGreater(finished - created, 2)
                path = server.data / "jobs" / job["id"] / "video.mp4"
                wait_until(lambda: not path.exists(), timeout=7, message="completed-file TTL cleanup")
                self.assertEqual(client.http.get(url).status_code, 404)
                self.assertEqual(client.http.get(f"/api/jobs/{job['id']}").status_code, 404)
            finally:
                client.http.close()

    def test_18_expired_public_session_cannot_reuse_metadata_or_download(self):
        with server_context(public=True, limits={"VIDEO_SESSION_TTL_SECONDS": "60"}) as server:
            client = Client(server)
            try:
                metadata = client.inspect()
                job = client.create(metadata=metadata)
                self.assertEqual(client.terminal(job["id"])["status"], "completed")
                old_csrf = client.csrf
                old_cookie = client.http.cookies.get("videosaver_session")
                # Advance this disposable session to its expiry boundary without a sleep.
                with sqlite3.connect(server.data / "state.sqlite3") as db:
                    created, expires = db.execute("SELECT created,expires FROM sessions").fetchone()
                    self.assertAlmostEqual(expires - created, 60, delta=.1)
                    db.execute("UPDATE sessions SET expires=?", (time.time() - 1,))
                self.assertTrue(client.refresh()["authenticated"])
                self.assertNotEqual(client.csrf, old_csrf)
                self.assertNotEqual(client.http.cookies.get("videosaver_session"), old_cookie)
                self.assertEqual(client.http.get(f"/api/jobs/{job['id']}/file").status_code, 404)
                response = client.post("/api/jobs", {"metadata_token": metadata["metadata_token"], "quality": 360})
                self.assertEqual(response.status_code, 409, response.text)
            finally:
                client.http.close()

    def test_19_public_inspect_limit_applies_across_sessions(self):
        with server_context(public=True, limits={"VIDEO_INSPECT_RATE_PER_MINUTE": "2"}) as server:
            clients = [Client(server) for _ in range(3)]
            try:
                clients[0].inspect()
                clients[1].inspect(OTHER)
                response = clients[2].post("/api/inspect", {"url": f"https://youtu.be/{GOOD}"})
                self.assertEqual(response.status_code, 429, response.text)
                self.assertEqual(len(server.calls()), 2)
            finally:
                for client in clients:
                    client.http.close()

    def test_20_public_job_limit_applies_across_sessions(self):
        with server_context(public=True, limits={"VIDEO_JOB_RATE_PER_MINUTE": "2"}) as server:
            clients = [Client(server) for _ in range(3)]
            try:
                for client in clients[:2]:
                    job = client.create()
                    self.assertEqual(client.terminal(job["id"])["status"], "completed")
                metadata = clients[2].inspect()
                before = len(server.calls())
                denied = clients[2].post("/api/jobs", {"metadata_token": metadata["metadata_token"], "quality": 360})
                self.assertEqual(denied.status_code, 429, denied.text)
                self.assertEqual(len(server.calls()), before)
            finally:
                for client in clients:
                    client.http.close()

    def test_21_public_session_limit_preserves_existing_session(self):
        with server_context(public=True, limits={"VIDEO_SESSION_RATE_PER_MINUTE": "2"}) as server:
            clients = [Client(server) for _ in range(2)]
            try:
                with httpx.Client(base_url=server.url) as guest:
                    denied = guest.get("/api/session")
                self.assertEqual(denied.status_code, 429, denied.text)
                self.assertTrue(clients[0].refresh()["authenticated"])
                clients[0].inspect()
            finally:
                for client in clients:
                    client.http.close()

    def test_22_forwarded_ip_requires_trusted_peer_and_single_valid_address(self):
        with server_context(public=True) as server:
            with httpx.Client(base_url=server.url) as guest:
                denied = guest.get("/api/session", headers={"X-Forwarded-For": "203.0.113.7"})
            self.assertEqual(denied.status_code, 403, denied.text)
        with server_context(public=True, limits={"VIDEO_TRUSTED_PROXIES": "127.0.0.0/8", "VIDEO_INSPECT_RATE_PER_MINUTE": "1"}) as server:
            one = Client(server, headers={"X-Forwarded-For": "203.0.113.7"})
            same = Client(server, headers={"X-Forwarded-For": "203.0.113.7"})
            two = Client(server, headers={"X-Forwarded-For": "203.0.113.8"})
            try:
                one.inspect()
                denied = same.post("/api/inspect", {"url": f"https://youtu.be/{GOOD}"})
                self.assertEqual(denied.status_code, 429, denied.text)
                two.inspect()
                for forwarded in ("203.0.113.7, 203.0.113.8", "invalid", "http://203.0.113.7", "203.0.113.7:1234"):
                    with self.subTest(forwarded=forwarded):
                        bad = one.http.get("/api/session", headers={"X-Forwarded-For": forwarded})
                        self.assertEqual(bad.status_code, 403, bad.text)
                duplicate = one.http.get("/api/session", headers=[("X-Forwarded-For", "203.0.113.7"), ("X-Forwarded-For", "203.0.113.8")])
                self.assertEqual(duplicate.status_code, 403, duplicate.text)
            finally:
                one.http.close()
                same.http.close()
                two.http.close()

    def test_23_cancelled_and_failed_jobs_record_terminal_time(self):
        with server_context(public=True) as server:
            client = Client(server)
            try:
                metadata = client.inspect()
                server.set_modes({GOOD: {"mode": "fail"}})
                failed = client.create(metadata=metadata)
                self.assertEqual(client.terminal(failed["id"])["status"], "failed")
                server.set_modes({GOOD: {"mode": "hang"}})
                cancelled = client.create()
                wait_until((server.root / f"pids-{GOOD}.json").exists, message="public cancellation fixture")
                response = client.cancel(cancelled["id"])
                self.assertEqual(response.status_code, 200)
                self.assertEqual(client.terminal(cancelled["id"])["status"], "cancelled")
                with sqlite3.connect(server.data / "state.sqlite3") as db:
                    rows = db.execute("SELECT created_epoch,finished_epoch FROM jobs WHERE id IN (?,?)", (failed["id"], cancelled["id"])).fetchall()
                self.assertEqual(len(rows), 2)
                for created, finished in rows:
                    self.assertIsNotNone(finished)
                    self.assertGreaterEqual(finished, created)
                    self.assertLess(time.time() - finished, 6)
            finally:
                client.http.close()

    def test_24_public_capacity_reclaims_only_empty_sessions(self):
        sys.path.insert(0, str(WEB))
        try:
            from engine import Config, Engine, utc_now
        finally:
            sys.path.pop(0)
        with tempfile.TemporaryDirectory(prefix="clipsave-public-capacity-") as directory:
            engine = Engine(Config(data_dir=Path(directory)))
            engine.start()
            try:
                engine.bind_auth_context("", "https://video.example.test", True)
                metadata_owner, metadata_cookie = engine.session(None, True)
                job_owner, job_cookie = engine.session(None, True)
                empty, _ = engine.session(None, True)
                now = time.time()
                engine.db.execute("BEGIN")
                for offset, owner in enumerate((metadata_owner, job_owner, empty)):
                    engine.db.execute("UPDATE sessions SET created=? WHERE id=?", (now - 200 + offset, owner["id"]))
                engine.db.execute("INSERT INTO metadata VALUES (?,?,?,?)", ("fixture-metadata-token", metadata_owner["id"], "{}", now + 600))
                engine.db.execute("INSERT INTO jobs(id,owner,status,title,quality,progress,stage_message,created_at,updated_at,created_epoch,video_id,url,duration,finished_epoch) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                                  ("a" * 32, job_owner["id"], "failed", "Capacity fixture", 360, None, "fixture", utc_now(), utc_now(), now, GOOD, f"https://youtu.be/{GOOD}", 1.2, now))
                engine.db.executemany("INSERT INTO sessions VALUES (?,?,?,?,?)", [
                    (f"public-capacity-{i}", f"csrf-{i}", 1, now + i / 10000, now + 3600)
                    for i in range(4093)
                ])
                engine.db.execute("COMMIT")
                new, new_cookie = engine.session(None, True)
                self.assertTrue(new_cookie)
                self.assertNotEqual(new["id"], empty["id"])
                self.assertIsNone(engine.db.execute("SELECT id FROM sessions WHERE id=?", (empty["id"],)).fetchone())
                for original, cookie in ((metadata_owner, metadata_cookie), (job_owner, job_cookie)):
                    preserved, replacement = engine.session(cookie, True)
                    self.assertEqual(preserved["id"], original["id"])
                    self.assertIsNone(replacement)
                self.assertEqual(engine.db.execute("SELECT COUNT(*) FROM sessions").fetchone()[0], 4096)
            finally:
                engine.close()

    def test_25_each_available_platform_saves_real_h264_aac_mp4(self):
        other = self.second_client()
        advertised = {item["id"]: item["available"] for item in self.client.session["platforms"]}
        self.assertEqual(set(advertised), {"youtube", "instagram", "threads", "x", "facebook"})
        for platform, (video_id, url) in PLATFORM_URLS.items():
            with self.subTest(platform=platform):
                self.assertTrue(advertised[platform])
                metadata = self.client.inspect(url=url, platform=platform)
                self.assertEqual(metadata["platform"], platform)
                self.assertEqual(metadata["id"], video_id)
                job = self.client.create(metadata=metadata)
                done = self.client.terminal(job["id"])
                self.assertEqual(done["status"], "completed", done)
                self.assertEqual(done["platform"], platform)
                response = self.client.http.get(f"/api/jobs/{job['id']}/file")
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers["content-type"], "video/mp4")
                self.assertEqual(other.http.get(f"/api/jobs/{job['id']}/file").status_code, 404)
                path = self.server.data / "jobs" / job["id"] / "video.mp4"
                probe = subprocess.run([tool("ffprobe"), "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)], capture_output=True, text=True, check=True, timeout=15)
                media = json.loads(probe.stdout)
                video = next(stream for stream in media["streams"] if stream["codec_type"] == "video")
                audio = next(stream for stream in media["streams"] if stream["codec_type"] == "audio")
                self.assertEqual((video["codec_name"], video["pix_fmt"], audio["codec_name"]), ("h264", "yuv420p", "aac"))
                self.assertLessEqual(video["height"], 360)
                self.assertAlmostEqual(float(media["format"]["duration"]), 1.2, delta=.15)
                downloads = [call for call in self.server.calls() if call["id"] == video_id and "--no-simulate" in call["args"]]
                self.assertTrue(downloads)
                self.assertEqual(downloads[-1]["platform"], platform)
                self.assertIn("--load-info-json", downloads[-1]["args"])
                self.assertEqual(downloads[-1]["loaded_info_id"], str(int(video_id) + 99) if platform == "x" else video_id)
                self.assertNotIn("webpage_url", downloads[-1]["loaded_info_keys"])
                self.assertNotIn("original_url", downloads[-1]["loaded_info_keys"])
                arguments = downloads[-1]["args"]
                self.assertEqual(arguments[arguments.index("--use-extractors") + 1], "^$")

    def test_26_platform_url_mismatch_and_ssrf_forms_never_reach_tool(self):
        invalid = [
            ("youtube", PLATFORM_URLS["instagram"][1]), ("instagram", PLATFORM_URLS["youtube"][1]),
            ("facebook", PLATFORM_URLS["x"][1]), ("x", PLATFORM_URLS["facebook"][1]),
            ("instagram", "http://127.0.0.1/private"), ("facebook", "http://169.254.169.254/latest/meta-data/"),
            ("x", "file:///etc/passwd"), ("instagram", "https://instagram.com.evil.invalid/reel/Chunk8-jurw/"),
            ("x", "https://x.com@evil.invalid/example/status/1790000000000000001"),
            ("facebook", "https://facebook.com:8443/watch/?v=10153231379946729"),
            ("instagram", "https://www.instagram.com/reel/%2e%2e/"),
            ("instagram", "https://www.instagram.com/reel/Chunk8-jurw/../../private"),
            ("x", "https://x.com/example/\nstatus/1790000000000000001"),
            ("x", "https://x.com/example/status/1790000000000000001\\evil"),
            ("facebook", "https://www.facebook.com/l.php?u=http://127.0.0.1/private"),
            ("instagram", "https://www.instagram.com/explore/tags/test/"),
            ("x", "https://x.com/example"), ("facebook", "https://www.facebook.com/example"),
        ]
        before = len(self.server.calls())
        for platform, url in invalid:
            with self.subTest(platform=platform, url=url):
                response = self.client.post("/api/inspect", {"url": url, "platform": platform})
                self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(len(self.server.calls()), before)

    def test_27_rejects_generic_wrong_extractor_carousel_and_unsafe_metadata_ids(self):
        invalid = [
            ("youtube", {"extractor_key": None, "extractor": None}, "normal"),
            ("instagram", {"extractor_key": "Generic"}, "normal"),
            ("instagram", {"extractor_key": "Youtube"}, "normal"),
            ("facebook", {"extractor_key": "Generic"}, "normal"),
            ("x", {"extractor_key": "Generic"}, "normal"),
            ("instagram", {}, "carousel"), ("facebook", {}, "carousel"),
            ("instagram", {"id": "../../escaped"}, "normal"),
            ("facebook", {"id": "10153231379946730"}, "normal"),
            ("x", {"display_id": "1790000000000000002"}, "normal"),
            ("x", {"id": "../outside", "display_id": "1790000000000000001"}, "normal"),
            ("instagram", {"webpage_url": "https://127.0.0.1/private"}, "normal"),
            ("instagram", {"_type": "url_transparent", "url": "https://127.0.0.1/private"}, "normal"),
        ]
        for platform, overrides, mode in invalid:
            video_id, url = PLATFORM_URLS[platform]
            with self.subTest(platform=platform, overrides=overrides, mode=mode):
                self.server.set_modes({video_id: {"metadata": overrides, "mode": mode}})
                response = self.client.post("/api/inspect", {"url": url, "platform": platform})
                self.assertIn(response.status_code, (400, 422, 502), response.text)
                self.assertNotIn("metadata_token", response.json())
        self.assertFalse(any(call["platform"] != "youtube" and "--no-simulate" in call["args"] for call in self.server.calls()[-len(invalid):]))

    def test_28_signed_thumbnail_scope_rejects_credentials_ports_and_host_spoofing(self):
        safe = {
            "instagram": "https://scontent.cdninstagram.com/v/test.jpg?signature=offline%2Bonly&expires=123",
            "x": "https://pbs.twimg.com/ext_tw_video_thumb/test.jpg?format=jpg&name=small",
            "facebook": "https://scontent.xx.fbcdn.net/v/test.jpg?signature=offline%2Bonly&expires=123",
        }
        for platform, thumbnail in safe.items():
            video_id, url = PLATFORM_URLS[platform]
            self.server.set_modes({video_id: {"metadata": {"thumbnail": thumbnail}}})
            self.assertEqual(self.client.inspect(url=url, platform=platform)["thumbnail"], thumbnail)
        video_id, url = PLATFORM_URLS["instagram"]
        bad = ["http://scontent.cdninstagram.com/test.jpg", "https://127.0.0.1/test.jpg",
               "https://scontent.cdninstagram.com.evil.invalid/test.jpg", "https://evil.invalid/test.jpg",
               "https://user:password@scontent.cdninstagram.com/test.jpg", "https://scontent.cdninstagram.com:8443/test.jpg",
               "https://127.0.0.1\\.cdninstagram.com/test.jpg",
               "https://pbs.twimg.com/test.jpg", "data:image/png;base64,AAAA", "file:///etc/passwd"]
        for thumbnail in bad:
            with self.subTest(thumbnail=thumbnail):
                self.server.set_modes({video_id: {"metadata": {"thumbnail": thumbnail}}})
                metadata = self.client.inspect(url=url, platform="instagram")
                self.assertIsNone(metadata["thumbnail"])

    def test_29_unknown_source_metadata_is_probed_and_resized_to_selected_height(self):
        video_id, url = PLATFORM_URLS["instagram"]
        large = self.server.root / "synthetic-640x480-h264-aac.mp4"
        subprocess.run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin", "-i", str(FIXTURE),
                        "-vf", "scale=640:480", "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                        "-c:a", "aac", "-movflags", "+faststart", "-n", str(large)], check=True, capture_output=True, timeout=30)
        self.server.set_modes({video_id: {"fixture_path": str(large), "metadata": {
            "duration": None, "formats": [{"format_id": "unknown", "vcodec": "h264", "acodec": "aac", "ext": "mp4", "url": "https://scontent.cdninstagram.com/v/test.mp4?signature=fixture"}]}}})
        metadata = self.client.inspect(url=url, platform="instagram")
        self.assertIn(metadata["duration"], (0, None))
        self.assertIn(360, metadata["qualities"])
        job = self.client.create(metadata=metadata, quality=360)
        done = self.client.terminal(job["id"])
        self.assertEqual(done["status"], "completed", done)
        path = self.server.data / "jobs" / job["id"] / "video.mp4"
        probe = subprocess.run([tool("ffprobe"), "-v", "error", "-show_streams", "-of", "json", str(path)], capture_output=True, text=True, check=True, timeout=15)
        video = next(row for row in json.loads(probe.stdout)["streams"] if row["codec_type"] == "video")
        self.assertEqual(video["height"], 360)
        self.assertEqual(video["width"], 480)
        self.assertEqual(video["codec_name"], "h264")

    def test_30_unknown_duration_still_obeys_actual_media_duration_limit(self):
        with server_context(limits={"VIDEO_MAX_DURATION_SECONDS": "1"}) as server:
            client = Client(server)
            try:
                video_id, url = PLATFORM_URLS["facebook"]
                # Exceed the 1-second policy plus the explicit 0.5-second
                # container-duration tolerance; the baseline 1.2s would fit.
                too_long = server.root / "synthetic-2s-h264-aac.mp4"
                subprocess.run([tool("ffmpeg"), "-hide_banner", "-loglevel", "error", "-nostdin",
                                "-stream_loop", "-1", "-i", str(FIXTURE), "-t", "2",
                                "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p",
                                "-c:a", "aac", "-movflags", "+faststart", "-n", str(too_long)],
                               check=True, capture_output=True, timeout=30)
                server.set_modes({video_id: {"fixture_path": str(too_long), "metadata": {"duration": None}}})
                metadata = client.inspect(url=url, platform="facebook")
                job = client.create(metadata=metadata)
                done = client.terminal(job["id"])
                self.assertEqual(done["status"], "failed", done)
                self.assertIn("duration limit", done["error"])
                self.assertIn(client.http.get(f"/api/jobs/{job['id']}/file").status_code, (404, 409))
            finally:
                client.http.close()

    def test_31_platform_enum_threads_available_and_legacy_youtube_default(self):
        before = len(self.server.calls())
        for platform in ("unknown", "Instagram", "", None, 123, ["youtube"]):
            with self.subTest(platform=platform):
                response = self.client.post("/api/inspect", {"url": PLATFORM_URLS["youtube"][1], "platform": platform})
                self.assertEqual(response.status_code, 422, response.text)
        self.assertEqual(len(self.server.calls()), before)
        self.assertTrue(next(item["available"] for item in self.client.session["platforms"] if item["id"] == "threads"))
        video_id, url = PLATFORM_URLS["threads"]
        metadata = self.client.inspect(url=url, platform="threads")
        self.assertEqual(metadata["id"], video_id)
        for mode, code in (("carousel", "threads_multiple_videos"), ("no_video", "threads_no_video")):
            self.server.set_modes({video_id: {"mode": mode}})
            response = self.client.post("/api/inspect", {"url": url, "platform": "threads"})
            self.assertEqual(response.status_code, 422, response.text)
            self.assertEqual(response.json()["code"], code)
        self.server.set_modes({})
        self.assertEqual(self.client.inspect()["platform"], "youtube")

    def test_32_platform_is_bound_to_metadata_token_and_not_job_request(self):
        _, instagram_url = PLATFORM_URLS["instagram"]
        _, facebook_url = PLATFORM_URLS["facebook"]
        instagram = self.client.inspect(url=instagram_url, platform="instagram")
        self.client.inspect(url=facebook_url, platform="facebook")
        other = self.second_client()
        denied = other.post("/api/jobs", {"metadata_token": instagram["metadata_token"], "quality": 360})
        self.assertIn(denied.status_code, (400, 403, 409))
        mutation = self.client.post("/api/jobs", {"metadata_token": instagram["metadata_token"], "quality": 360, "platform": "x"})
        self.assertEqual(mutation.status_code, 422, mutation.text)
        job = self.client.create(metadata=instagram)
        done = self.client.terminal(job["id"])
        self.assertEqual(done["status"], "completed", done)
        self.assertEqual(done["platform"], "instagram")
        downloads = [call for call in self.server.calls() if "--no-simulate" in call["args"]]
        self.assertEqual(downloads[-1]["platform"], "instagram")

    def test_33_changed_x_media_id_is_rejected_before_download(self):
        video_id, url = PLATFORM_URLS["x"]
        metadata = self.client.inspect(url=url, platform="x")
        self.assertEqual(metadata["id"], video_id)
        before = len(self.server.calls())
        self.server.set_modes({video_id: {"metadata": {"id": str(int(video_id) + 200)}}})
        job = self.client.create(metadata=metadata)
        done = self.client.terminal(job["id"])
        self.assertEqual(done["status"], "failed", done)
        self.assertFalse(any("--no-simulate" in call["args"] for call in self.server.calls()[before:]))
        self.assertIn(self.client.http.get(f"/api/jobs/{job['id']}/file").status_code, (404, 409))

    def test_34_media_urls_stay_within_the_selected_platform_cdn(self):
        video_id, url = PLATFORM_URLS["instagram"]
        blocked = ["http://scontent.cdninstagram.com/video.mp4", "https://127.0.0.1/video.mp4",
                   "https://169.254.169.254/latest/meta-data/", "https://localhost/video.mp4",
                   "https://scontent.cdninstagram.com.evil.invalid/video.mp4",
                   "https://user:password@scontent.cdninstagram.com/video.mp4",
                   "https://scontent.cdninstagram.com:8443/video.mp4",
                   "https://127.0.0.1\\.cdninstagram.com/video.mp4",
                   "https://offline.googlevideo.com/video.mp4", "file:///etc/passwd"]
        for media_url in blocked:
            with self.subTest(media_url=media_url):
                self.server.set_modes({video_id: {"metadata": {"formats": [
                    {"format_id": "bad", "vcodec": "h264", "acodec": "aac", "height": 360,
                     "width": 640, "ext": "mp4", "url": media_url}]}}})
                response = self.client.post("/api/inspect", {"url": url, "platform": "instagram"})
                self.assertIn(response.status_code, (400, 422, 502), response.text)
                self.assertNotIn("metadata_token", response.json())

    def test_35_known_post_variants_canonicalize_without_redirect_parameters(self):
        ig = PLATFORM_URLS["instagram"][0]
        xid = PLATFORM_URLS["x"][0]
        fb = PLATFORM_URLS["facebook"][0]
        cases = [
            ("instagram", f"https://instagram.com/p/{ig}/?igsh=tracking", f"https://www.instagram.com/p/{ig}/"),
            ("instagram", f"https://www.instagram.com/example/reels/{ig}/", f"https://www.instagram.com/reel/{ig}/"),
            ("x", f"https://mobile.twitter.com/example/status/{xid}?s=20", f"https://x.com/i/web/status/{xid}"),
            ("x", f"https://twitter.com/statuses/{xid}", f"https://x.com/i/web/status/{xid}"),
            ("x", f"https://x.com/example/status/{xid}/video/2", f"https://x.com/i/web/status/{xid}/video/2"),
            ("facebook", f"https://www.facebook.com/reel/{fb}/?mibextid=tracking", f"https://www.facebook.com/watch/?v={fb}"),
            ("facebook", f"https://m.facebook.com/example/videos/{fb}/", f"https://www.facebook.com/watch/?v={fb}"),
            ("facebook", f"https://www.facebook.com/example/videos/test-title/{fb}/", f"https://www.facebook.com/watch/?v={fb}"),
        ]
        for platform, url, canonical in cases:
            with self.subTest(platform=platform, url=url):
                metadata = self.client.inspect(url=url, platform=platform)
                self.assertEqual(metadata["platform"], platform)
                self.assertEqual(self.server.calls()[-1]["args"][-1], canonical)
        before = len(self.server.calls())
        for platform, url in (("facebook", "https://fb.watch/abc123/"),
                              ("facebook", "https://www.facebook.com/share/v/abc123/"),
                              ("instagram", "https://www.instagram.com/share/reel/abc123/"),
                              ("instagram", f"https://www.instagram.com/p/{ig}/?img_index=2")):
            response = self.client.post("/api/inspect", {"url": url, "platform": platform})
            self.assertIn(response.status_code, (400, 422), response.text)
        self.assertEqual(len(self.server.calls()), before)

    def test_36_fragment_urls_and_download_snapshot_headers_are_bounded(self):
        video_id, url = PLATFORM_URLS["instagram"]
        base = {"format_id": "segmented", "vcodec": "h264", "acodec": "aac", "height": 360,
                "width": 640, "ext": "mp4", "url": "https://scontent.cdninstagram.com/v/video.mp4?signature=fixture"}
        for unsafe in ({"manifest_url": "https://127.0.0.1/manifest.m3u8"},
                       {"fragment_base_url": "https://127.0.0.1/"},
                       {"fragments": [{"url": "https://127.0.0.1/segment.m4s"}]},
                       {"fragments": [{"url": "//169.254.169.254/latest/meta-data/"}]}):
            with self.subTest(unsafe=unsafe):
                self.server.set_modes({video_id: {"metadata": {"formats": [{**base, **unsafe}]}}})
                response = self.client.post("/api/inspect", {"url": url, "platform": "instagram"})
                self.assertIn(response.status_code, (400, 422, 502), response.text)
        self.server.set_modes({video_id: {"metadata": {
            "http_headers": {"Authorization": "synthetic-secret", "Cookie": "synthetic=only", "User-Agent": "OfflineFixture"},
            "formats": [{**base, "fragment_base_url": "https://scontent.cdninstagram.com/v/",
                         "fragments": [{"url": "segment.m4s", "duration": 1.2}],
                         "http_headers": {"Authorization": "synthetic-only", "Cookie": "synthetic=only", "Referer": url}}]}}})
        metadata = self.client.inspect(url=url, platform="instagram")
        job = self.client.create(metadata=metadata)
        done = self.client.terminal(job["id"])
        self.assertEqual(done["status"], "completed", done)
        loaded = [call for call in self.server.calls() if "--load-info-json" in call["args"]][-1]
        names = [name.lower() for name in loaded["loaded_header_names"]]
        names += [name.lower() for headers in loaded["loaded_format_header_names"] for name in headers]
        self.assertNotIn("authorization", names)
        self.assertNotIn("cookie", names)
        self.assertIn("user-agent", names)
        self.assertIn("referer", names)

    def test_37_threads_parser_checks_identity_single_video_and_cdn_before_download(self):
        sys.path.insert(0, str(WEB))
        try:
            from threads_adapter import MAX_HTML_BYTES, ThreadsError, canonical_threads_url, parse_threads_embed
        finally:
            sys.path.pop(0)
        video_id, url = PLATFORM_URLS["threads"]
        link = f'<a href="{url}">post</a>'
        video = '<video><source src="https://video.cdninstagram.com/public.mp4?signature=fixture"></video>'
        valid = link + '<span class="BodyTextContainer">Caption &amp; 한글<script>ignored()</script></span>' + video
        info = parse_threads_embed(valid, url)
        self.assertEqual(info["id"], video_id)
        self.assertEqual(info["title"], "Caption & 한글")
        self.assertEqual(info["extractor_key"], "ThreadsDirect")
        self.assertEqual(info["duration"], 0)
        self.assertEqual(canonical_threads_url(url.replace("threads.com", "threads.net") + "?x=tracking")[0], url)
        cases = [
            (valid.replace(video_id, "DIFFERENT01", 1), "threads_identity_mismatch"),
            (link, "threads_no_video"),
            (link + video + video, "threads_multiple_videos"),
            (link + '<div class="CarouselContainer">' + video + '</div>', "threads_multiple_videos"),
            (link + video.replace('</video>', '<source src="https://video.cdninstagram.com/second.mp4"></video>'), "threads_multiple_videos"),
            (valid.replace("https://video.cdninstagram.com/public.mp4?signature=fixture", "https://127.0.0.1/private.mp4"), "threads_invalid_media"),
            (valid.replace("https://video.cdninstagram.com/public.mp4?signature=fixture", "https://127.0.0.1\\.cdninstagram.com/private.mp4"), "threads_invalid_media"),
            (valid.replace("https://video.cdninstagram.com/public.mp4?signature=fixture", "https://user:pass@video.cdninstagram.com/public.mp4"), "threads_invalid_media"),
            (valid.replace("https://video.cdninstagram.com/public.mp4?signature=fixture", "http://video.cdninstagram.com/public.mp4"), "threads_invalid_media"),
            ("x" * (MAX_HTML_BYTES + 1), "threads_unavailable"),
        ]
        for html, code in cases:
            with self.subTest(code=code, length=len(html)):
                with self.assertRaises(ThreadsError) as caught:
                    parse_threads_embed(html, url)
                self.assertEqual(caught.exception.code, code)
        for invalid in (url.replace("threads.com", "threads.com.evil.invalid"),
                        url.replace("threads.com", "threads.com:8443"),
                        url.replace("www.threads.com", "127.0.0.1\\.threads.com"),
                        url.replace("@example", "@exam\nple"), url.replace("@example", "@" + "x" * 31)):
            with self.subTest(url=invalid), self.assertRaises(ThreadsError):
                canonical_threads_url(invalid)


if __name__ == "__main__":
    unittest.main(verbosity=2)
