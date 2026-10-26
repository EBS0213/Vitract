"""Offline embed fetch substitute; uses the production Threads HTML parser."""
from __future__ import annotations

import argparse
from html import escape
import json
import os
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from threads_adapter import ThreadsError, parse_threads_embed

parser = argparse.ArgumentParser()
parser.add_argument("--url", required=True)
parser.add_argument("--timeout", type=float, required=True)
args = parser.parse_args()
control_path = Path(os.environ["VIDEO_TEST_CONTROL"])
control = json.loads(control_path.read_text(encoding="utf-8"))
video_id = args.url.rstrip("/").rsplit("/", 1)[-1]
settings = dict(control.get("default", {}), **control.get("videos", {}).get(video_id, {}))
with (control_path.parent / "calls.jsonl").open("a", encoding="utf-8") as log:
    log.write(json.dumps({"id": video_id, "platform": "threads", "args": sys.argv[1:],
                          "pid": os.getpid(), "loaded_info_id": None}) + "\n")

mode = settings.get("mode", "normal")
video = '<video poster="https://scontent.cdninstagram.com/test.jpg?signature=fixture"><source src="https://video.cdninstagram.com/public.mp4?signature=fixture"></video>'
html = ('<a href="' + escape(args.url, quote=True) + '">post</a>'
        '<span class="BodyTextContainer">' + escape(settings.get("title", "합성 Threads 영상")) + '</span>'
        + video * (2 if mode == "carousel" else 0 if mode == "no_video" else 1))
try:
    if mode == "fail":
        raise ThreadsError("Fixture deliberately failed")
    info = parse_threads_embed(html, args.url)
    info.update(settings.get("metadata", {}))
    print(json.dumps(info, ensure_ascii=False))
except ThreadsError as error:
    print(json.dumps({"code": error.code, "error": str(error)}), file=sys.stderr)
    sys.exit(1)
