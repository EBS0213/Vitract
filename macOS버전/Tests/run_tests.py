#!/usr/bin/env python3
"""Compile the actual backend and test its behavior using only synthetic media."""

from pathlib import Path
import json
import os
import shutil
import subprocess
import tempfile
import time


PROJECT = Path(__file__).resolve().parents[1]
TESTS = PROJECT / "Tests"
TOOLS = PROJECT / "build" / "Vitract.app" / "Contents" / "Resources" / "tools"
if not TOOLS.is_dir():
    TOOLS = Path.home() / "Desktop" / "Vitract.app" / "Contents" / "Resources" / "tools"


def main():
    started = time.time()
    with tempfile.TemporaryDirectory(prefix="videosaver-backend-tests-") as directory:
        root = Path(directory)
        fixture_tools = root / "tools"
        fixture_tools.mkdir()
        shutil.copyfile(TESTS / "fake_yt_dlp.py", fixture_tools / "yt-dlp")
        os.chmod(fixture_tools / "yt-dlp", 0o755)
        shutil.copyfile(TESTS / "fixtures" / "synthetic-av1-opus.mp4",
                        fixture_tools / "synthetic-av1-opus.mp4")
        for name in ("ffmpeg", "ffprobe", "node"):
            (fixture_tools / name).symlink_to(TOOLS / name)
        executable = root / "backend-tests"
        module_cache = Path(tempfile.gettempdir()) / "videosaver-backend-swift-module-cache"
        compile_command = ["swiftc", "-parse-as-library", "-module-cache-path",
                           str(module_cache),
                           str(PROJECT / "Sources" / "Downloader.swift"),
                           str(TESTS / "BackendTests.swift"), "-o", str(executable),
                           "-framework", "AppKit", "-framework", "SwiftUI"]
        subprocess.run(compile_command, check=True)
        result = subprocess.run([str(executable), str(fixture_tools), str(root / "output")],
                                text=True, capture_output=True, timeout=120)
        print(result.stdout, end="")
        print(result.stderr, end="")
        report = {"exit_code": result.returncode,
                  "duration_seconds": round(time.time() - started, 2),
                  "network_used": False,
                  "fixture": "locally generated 1.2-second AV1/Opus MP4",
                  "stdout": result.stdout, "stderr": result.stderr}
        (TESTS / "last_result.json").write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n")
        if result.returncode:
            raise SystemExit(result.returncode)


if __name__ == "__main__":
    main()
