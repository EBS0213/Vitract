# Backend verification

Run `python3 Tests/run_tests.py` from the project folder after building the app. The runner also finds the delivered app on the Desktop if the build bundle has been moved there. Swift command line tools and Python 3 are required for these developer tests; the finished app does not require them.

The test compiles the actual `Sources/Downloader.swift`. An offline yt-dlp fixture provides metadata and copies a locally generated 1.2-second AV1/Opus MP4 into the model's temporary working folder. The real bundled FFmpeg and FFprobe then perform and validate the model's conversion. No YouTube video is downloaded by these tests.

Checks include:

- Accepted single-video URL forms and removal of playlist/tracking arguments.
- Rejection of host spoofing, credentials, redirects, playlist-only URLs, malformed IDs, and duplicate video IDs.
- Actual H.264/yuv420p video and AAC audio output, duration preservation, selected-quality arguments, and scratch-folder cleanup.
- Repeated saves and atomic publication without overwriting existing files.
- Long emoji filenames measured in UTF-8 bytes and saved to the filesystem.
- Changed-link metadata guards, selection retention, malformed metadata, live broadcasts, and tool failures.
- Immediate cancellation races and termination of a stubborn descendant that ignores interrupt/termination signals and keeps output pipes open.

`last_result.json` records the latest result. Temporary media outputs and fixture executables are removed automatically. This offline suite does not establish that every YouTube video is downloadable; live YouTube extraction is a separate network check.

The synthetic fixture was generated using:

```sh
ffmpeg -hide_banner -loglevel error \
  -f lavfi -i testsrc2=size=320x180:rate=15 \
  -f lavfi -i sine=frequency=440:sample_rate=48000 \
  -t 1.2 -c:v libsvtav1 -preset 12 -crf 45 -threads 2 \
  -c:a libopus synthetic-av1-opus.mp4
```
