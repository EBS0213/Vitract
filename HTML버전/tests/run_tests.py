#!/usr/bin/env python3
"""Run offline HTTP/media integration tests and retain a readable result record."""
from __future__ import annotations

from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path
import subprocess
import sys
import time
import unittest

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import test_http_integration as suite


class Tee(io.TextIOBase):
    def __init__(self, *streams):
        self.streams = streams

    def write(self, value):
        for stream in self.streams:
            stream.write(value)
            stream.flush()
        return len(value)

    def flush(self):
        for stream in self.streams:
            if not stream.closed:
                stream.flush()


class RecordedResult(unittest.TextTestResult):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.records = []
        self.started = {}

    def startTest(self, test):
        self.started[test.id()] = time.monotonic()
        super().startTest(test)

    def record(self, test, status, detail=None):
        self.records.append({"test": test.id(), "status": status,
                             "duration_seconds": round(time.monotonic() - self.started.get(test.id(), time.monotonic()), 3),
                             **({"detail": detail} if detail else {})})

    def addSuccess(self, test):
        self.record(test, "passed")
        super().addSuccess(test)

    def addFailure(self, test, err):
        self.record(test, "failed", self._exc_info_to_string(err, test))
        super().addFailure(test, err)

    def addError(self, test, err):
        self.record(test, "error", self._exc_info_to_string(err, test))
        super().addError(test, err)

    def addSkip(self, test, reason):
        self.record(test, "skipped", reason)
        super().addSkip(test, reason)

    def addSubTest(self, test, subtest, err):
        if err:
            self.record(subtest, "failed", self._exc_info_to_string(err, subtest))
        super().addSubTest(test, subtest, err)


def main():
    started = time.monotonic()
    report = {"started_at": datetime.now(timezone.utc).isoformat(), "external_network_used": False,
              "localhost_http_used": True, "fixture": "locally generated 1.2-second AV1/Opus MP4",
              "real_media_tools": ["FFmpeg", "FFprobe"],
              "platform_fixtures": list(suite.PLATFORM_URLS),
              "threads_fixture": "production embed parser, synthetic HTML, test-only CLI substitute",
              "source_sha256": {name: hashlib.sha256((HERE.parent / name).read_bytes()).hexdigest()
                                for name in ("server.py", "engine.py", "platforms.py", "threads_adapter.py",
                                             "tests/test_http_integration.py", "tests/fake_yt_dlp.py",
                                             "tests/fake_threads_adapter.py", "tests/server_fixture.py")},
              "scope": "Offline five-platform HTTP integration; live platform access and browser rendering are separate checks"}
    log_path = HERE / "last_run.log"
    with log_path.open("w", encoding="utf-8") as log:
        output = Tee(sys.stdout, log)
        with redirect_stdout(output), redirect_stderr(output):
            try:
                raw = subprocess.run([suite.tool("ffprobe"), "-v", "error", "-show_entries", "stream=codec_name",
                                      "-of", "json", str(suite.FIXTURE)], capture_output=True, text=True,
                                     check=True, timeout=15)
                codecs = {row["codec_name"] for row in json.loads(raw.stdout)["streams"]}
                if not {"av1", "opus"}.issubset(codecs):
                    raise AssertionError(f"Expected actual AV1/Opus fixture, got {codecs}")
                report["source_fixture_codecs"] = sorted(codecs)
                tests = unittest.defaultTestLoader.loadTestsFromTestCase(suite.WebIntegrationTests)
                result = unittest.TextTestRunner(stream=output, verbosity=2, resultclass=RecordedResult).run(tests)
                report.update({"tests_run": result.testsRun, "failures": len(result.failures),
                               "errors": len(result.errors), "skipped": len(result.skipped),
                               "tests": result.records, "exit_code": 0 if result.wasSuccessful() else 1})
            except Exception as exc:
                print(f"Test runner setup failed: {type(exc).__name__}: {exc}")
                report.update({"exit_code": 1, "setup_error": f"{type(exc).__name__}: {exc}"})
    report["duration_seconds"] = round(time.monotonic() - started, 3)
    report["finished_at"] = datetime.now(timezone.utc).isoformat()
    (HERE / "last_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
