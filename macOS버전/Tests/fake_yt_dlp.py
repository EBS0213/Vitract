#!/usr/bin/python3
"""Offline yt-dlp fixture. Copies synthetic AV1/Opus media, never uses a network."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys


ROOT = Path(__file__).resolve().parent
args = sys.argv[1:]
with (ROOT / "calls.jsonl").open("a") as stream:
    stream.write(json.dumps(args) + "\n")

mode_file = ROOT / "mode"
mode = mode_file.read_text().strip() if mode_file.exists() else "normal"

if mode == "fail":
    print("ERROR: Fixture download deliberately failed", file=sys.stderr)
    sys.exit(1)

if mode == "hang":
    # A real process tree lets the test detect cancellation that only kills
    # yt-dlp's direct process and leaves its media subprocess running.
    descendant = subprocess.Popen(["/bin/sh", "-c", "trap '' INT TERM; exec /bin/sleep 120"])
    (ROOT / "parent.pid").write_text(str(os.getpid()))
    (ROOT / "descendant.pid").write_text(str(descendant.pid))
    print("fixture: waiting for cancellation", flush=True)
    descendant.wait()
    sys.exit(0)

metadata = {
    "id": "BaW_jenozKc",
    "subtitles": {"ko": [{"ext": "json3"}]},
    "automatic_captions": {"en": [{"ext": "json3"}]},
    "title": "합성 영상: 변환 / 검증",
    "uploader": "Local synthetic fixture",
    "duration": 1.2,
    "webpage_url": "https://www.youtube.com/watch?v=BaW_jenozKc",
    "thumbnail": "https://i.ytimg.com/vi/BaW_jenozKc/hqdefault.jpg",
    "live_status": "not_live",
    "is_live": False,
    "formats": [
        {"format_id": "av1", "ext": "mp4", "height": 180, "width": 320,
         "vcodec": "av01.0.00M.08", "acodec": "opus"},
        {"format_id": "higher", "ext": "mp4", "height": 360, "width": 640,
         "vcodec": "av01.0.00M.08", "acodec": "opus"},
    ],
}
title_file = ROOT / "title.txt"
if title_file.exists():
    metadata["title"] = title_file.read_text()

if mode == "live":
    metadata["is_live"] = True
    metadata["live_status"] = "is_live"

if mode == "malformed":
    print("This is not video metadata JSON")
    sys.exit(0)

if "--dump-single-json" in args or "--dump-json" in args or "-J" in args:
    print(json.dumps(metadata))
    sys.exit(0)

def option(*names):
    for index, argument in enumerate(args):
        if argument in names:
            return args[index + 1]
    return None


template = option("--output", "-o")
if template is None:
    raise RuntimeError("The model did not provide an explicit output template")
if "--write-subs" in args or "--write-auto-subs" in args:
    destination = Path(template.replace("%(ext)s", "ko.json3"))
    destination.parent.mkdir(parents=True, exist_ok=True)
    destination.write_text(json.dumps({"events": [{"segs": [{"utf8": "안녕하세요. "}, {"utf8": "테스트입니다."}]}, {"segs": [{"utf8": "다음 문장입니다."}]}]}))
    sys.exit(0)
destination = template.replace("%(ext)s", "mp4")
destination = destination.replace("%(id)s", metadata["id"])
destination = destination.replace("%(title)s", metadata["title"].replace("/", "_"))
destination = Path(destination)
directory = option("--paths", "-P")
if directory and not destination.is_absolute():
    destination = Path(directory) / destination
destination.parent.mkdir(parents=True, exist_ok=True)
shutil.copyfile(ROOT / "synthetic-av1-opus.mp4", destination)
print("[download] 100.0% of 20.0KiB at 1.0MiB/s ETA 00:00", flush=True)
for index, argument in enumerate(args):
    if argument in ("--print", "-O") and index + 1 < len(args):
        value = args[index + 1].split(":", 1)[-1]
        print(value.replace("%(filepath)s", str(destination))
                   .replace("%(filepath)j", json.dumps(str(destination))), flush=True)
