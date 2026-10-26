# Offline web verification

Run from the web folder:

```sh
python3 tests/run_tests.py
```

The suite starts disposable local HTTP servers and temporary storage. It uses
the fake downloader in this directory and the real FFmpeg/FFprobe executables.
No YouTube or other external service is contacted. HTTPX is a test dependency;
the server's runtime dependencies must also be installed.

Tool locations can be supplied with `VIDEO_FFMPEG`, `VIDEO_FFPROBE`, and
`VIDEO_NODE`. Otherwise the tests use the existing macOS app bundle (including
the delivered copy in `~/Documents/영상제작 APP/macOS버전`), falling
back to executables on `PATH`. `VIDEO_YTDLP` is always replaced with the offline
fixture executable. Production settings and stored downloads are not used.

The included media fixture is a generated 1.2-second color test pattern with a
sine-wave audio track, encoded as AV1 and Opus. The suite confirms those source
codecs before testing the actual conversion to H.264/yuv420p + AAC MP4.

Coverage includes URL allowlisting, canonicalization, CSRF and Origin checks,
password login, session isolation, metadata-token and quality validation,
actual conversion/duration/height, JSON progress, repeat downloads, failure
recovery, process-group cancellation, the two-worker queue, output-size limits,
and interrupted-job recovery after a server restart. The abrupt restart test
explicitly reaps fixture subprocesses in the harness; graceful cancellation's
process-tree cleanup is tested separately without that assistance.
Session-capacity regressions also verify that 4,096 anonymous sessions cannot
evict an authenticated owner, and that a fully authenticated capacity returns
a JSON 503 to new guests while existing owners can still complete downloads.

Public-mode coverage verifies anonymous saving with no history endpoint,
browser-session cookies, owner-only file access and manual download retries,
completion-based file expiry, expired-session isolation, IP-based session /
inspection / job limits across separate cookies, trusted-proxy attribution,
and rejection of untrusted, duplicate, or malformed forwarded-IP headers.
At global public-session capacity, empty sessions can be reclaimed while
sessions owning inspection tokens or jobs remain available.
It also checks terminal timestamps for successful, failed, and cancelled jobs.
TTL fixtures use real short expiry windows or alter only the disposable test
database to reach a boundary; no production download or setting is touched.

Multi-platform fixtures model YouTube, Instagram, Threads, X, and Facebook metadata and
exercise each platform through the same real FFmpeg conversion and protected
download route. They check platform/URL agreement, strict source and media IDs,
generic extractor and carousel rejection, signed-thumbnail host boundaries,
metadata-token ownership, reinspection before download, and sealed download
snapshots that disable fallback URL extraction. Threads uses the production
embed parser on synthetic official-page HTML through a test-only CLI substitute;
the HTTP server, process handling, identity checks, conversion, and file route
remain the real implementation. Parser cases reject mixed identities, carousels,
unsafe CDN URLs, and oversized HTML. An additional 640×480 H.264/AAC fixture verifies
that unknown source dimensions are actually resized to the selected 360p cap;
unknown metadata duration is still bounded by the probed media duration.
These fixtures do not establish that a live platform currently permits a
particular public post to be downloaded.

`last_result.json` records individual results and timing, `last_run.log` records
the test output, and `results/server-*.log` records each disposable server.
Temporary media and databases are removed after the tests. Live YouTube
availability, browser UI rendering, and an actual production deployment need
separate checks.
