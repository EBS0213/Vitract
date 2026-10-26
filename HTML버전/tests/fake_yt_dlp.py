#!/usr/bin/env python3
"""Offline downloader executable used by the web HTTP integration suite.

The fixture emits real yt-dlp JSON progress records and copies generated AV1/Opus
media. It never fetches a URL. Control and subprocess PID files live in the test
run's temporary directory, selected through VIDEO_TEST_CONTROL.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import time
from urllib.parse import parse_qs, urlsplit


args = sys.argv[1:]
if "--version" in args:
    print("2026.10.16-offline-test")
    sys.exit(0)

control_path = Path(os.environ["VIDEO_TEST_CONTROL"])
control = json.loads(control_path.read_text(encoding="utf-8"))
workspace = control_path.parent
loaded_info = None
if "--load-info-json" in args:
    loaded_info = json.loads(Path(args[args.index("--load-info-json") + 1]).read_text(encoding="utf-8"))
url = loaded_info.get("webpage_url", "") if loaded_info else next((value for value in reversed(args) if value.startswith(("https://", "http://"))), "")
if loaded_info and not url:
    # A sealed download snapshot omits webpage_url to disable yt-dlp re-extraction.
    source_id = loaded_info.get("display_id") or loaded_info.get("id")
    source_key = str(loaded_info.get("extractor_key") or loaded_info.get("extractor") or "").lower()
    templates = {"youtube": "https://www.youtube.com/watch?v={}", "instagram": "https://www.instagram.com/reel/{}/",
                 "twitter": "https://x.com/i/web/status/{}", "facebook": "https://www.facebook.com/watch/?v={}",
                 "threadsdirect": "https://www.threads.com/@example/post/{}"}
    url = templates[source_key].format(source_id)
parts = urlsplit(url)
host = parts.hostname or ""
segments = [segment for segment in parts.path.split("/") if segment]
if host.endswith("youtube.com") or host == "youtu.be":
    platform, extractor = "youtube", "Youtube"
    video_id = parse_qs(parts.query).get("v", [segments[-1] if segments else "BaW_jenozKc"])[0]
elif host.endswith("instagram.com"):
    platform, extractor = "instagram", "Instagram"
    video_id = segments[-1]
elif host in ("threads.com", "www.threads.com", "threads.net", "www.threads.net"):
    platform, extractor = "threads", "ThreadsDirect"
    video_id = segments[-1]
elif host in ("x.com", "www.x.com", "twitter.com", "www.twitter.com"):
    platform, extractor = "x", "Twitter"
    video_id = segments[segments.index("status") + 1] if "status" in segments else segments[-1]
elif host.endswith("facebook.com") or host == "fb.watch":
    platform, extractor = "facebook", "Facebook"
    video_id = parse_qs(parts.query).get("v", [segments[-1] if segments else "10153231379946729"])[0]
else:
    raise RuntimeError("Unexpected URL reached offline fixture")
settings = dict(control.get("default", {}), **control.get("videos", {}).get(video_id, {}))
mode = settings.get("mode", "normal")
with (workspace / "calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps({"id": video_id, "platform": platform, "args": args, "pid": os.getpid(),
                          "loaded_info_id": loaded_info.get("id") if loaded_info else None,
                          "loaded_info_keys": sorted(loaded_info) if loaded_info else None,
                          "loaded_header_names": sorted((loaded_info.get("http_headers") or {}).keys()) if loaded_info else None,
                          "loaded_format_header_names": [sorted((item.get("http_headers") or {}).keys()) for item in loaded_info.get("formats", [])] if loaded_info else None}, ensure_ascii=False) + "\n")

metadata = {
    "id": video_id, "title": settings.get("title", "합성 영상: 변환 / 검증"),
    "uploader": "Offline web test", "duration": settings.get("duration", 1.2),
    "webpage_url": url, "extractor_key": extractor,
    "thumbnail": {
        "youtube": f"https://i.ytimg.com/vi/{video_id}/hqdefault.jpg",
        "instagram": "https://scontent.cdninstagram.com/v/test.jpg?signature=offline-only",
        "threads": "https://scontent.cdninstagram.com/v/test.jpg?signature=offline-only",
        "x": "https://pbs.twimg.com/ext_tw_video_thumb/test.jpg?name=small&format=jpg",
        "facebook": "https://scontent.xx.fbcdn.net/v/test.jpg?signature=offline-only",
    }[platform],
    "is_live": mode == "live", "live_status": "is_live" if mode == "live" else "not_live",
    "formats": [
        {"format_id": f"test-{height}", "ext": "mp4", "height": height,
         "width": round(height * 16 / 9), "vcodec": "av01.0.00M.08", "acodec": "opus",
         "url": {
             "youtube": "https://offline.googlevideo.com/videoplayback?signature=fixture",
             "instagram": "https://scontent.cdninstagram.com/v/test.mp4?signature=fixture",
             "threads": "https://scontent.cdninstagram.com/v/test.mp4?signature=fixture",
             "x": "https://video.twimg.com/ext_tw_video/test.mp4?signature=fixture",
             "facebook": "https://scontent.xx.fbcdn.net/v/test.mp4?signature=fixture",
         }[platform],
         "has_drm": mode == "drm"}
        for height in settings.get("heights", [720, 360, 180])
    ],
}
if platform == "x":
    metadata["display_id"] = video_id
    metadata["id"] = str(int(video_id) + 99)
metadata.update(settings.get("metadata", {}))
if mode == "carousel":
    metadata.update({"_type": "playlist", "entries": [{"id": video_id + "_1"}, {"id": video_id + "_2"}]})
if mode == "upcoming":
    metadata["live_status"] = "is_upcoming"
if mode == "fail":
    print("ERROR: Fixture deliberately failed", file=sys.stderr)
    sys.exit(1)
if "--dump-single-json" in args or "--dump-json" in args or "-J" in args:
    print("not valid metadata JSON" if mode == "malformed" else json.dumps(metadata, ensure_ascii=False))
    sys.exit(0)


def option(*names):
    for index, value in enumerate(args[:-1]):
        if value in names:
            return args[index + 1]
    return None


def progress(downloaded, total, status="downloading"):
    record = {"status": status, "downloaded_bytes": downloaded, "total_bytes": total,
              "total_bytes_estimate": total, "speed": 8192, "eta": 2}
    template = next((args[i + 1].split(":", 1)[-1] for i, arg in enumerate(args[:-1])
                     if arg == "--progress-template" and args[i + 1].startswith("download:")), "__DOWNLOAD__%(progress)j")
    prefix = template.split("%(", 1)[0]
    print(prefix + json.dumps(record), flush=True)


if mode == "hang":
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    child = subprocess.Popen([sys.executable, "-c",
                             "import signal,time; signal.signal(signal.SIGINT,signal.SIG_IGN); "
                             "signal.signal(signal.SIGTERM,signal.SIG_IGN); time.sleep(180)"])
    (workspace / f"pids-{video_id}.json").write_text(json.dumps({"parent": os.getpid(), "child": child.pid}))
    progress(1024, 8192)
    child.wait()
    sys.exit(0)

template = option("--output", "-o")
if not template:
    raise RuntimeError("Backend must provide a fixed output template")
destination = Path(template.replace("%(ext)s", "mp4").replace("%(id)s", video_id))
directory = option("--paths", "-P")
if directory and not destination.is_absolute():
    destination = Path(directory) / destination
destination.parent.mkdir(parents=True, exist_ok=True)
source = Path(settings.get("fixture_path", control["fixture_path"]))
reported_total = settings.get("reported_total", source.stat().st_size)
for fraction in (0.25, 0.5, 0.75):
    progress(round(reported_total * fraction), reported_total)
    time.sleep(settings.get("progress_pause_seconds", 0.08))
shutil.copyfile(source, destination)
progress(reported_total, reported_total, "finished")
for index, value in enumerate(args[:-1]):
    if value in ("--print", "-O"):
        output = args[index + 1].split(":", 1)[-1]
        output = output.replace("%(filepath)j", json.dumps(str(destination)))
        output = output.replace("%(filepath)s", str(destination))
        print(output, flush=True)
