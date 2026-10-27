# Vitract

**Save videos as files. Turn captions into readable transcripts.**

Vitract is a video downloader and caption-to-text tool with a native macOS app and a self-hosted web version. Paste a supported video link, choose a quality, and save the result to your own folder. The macOS app can also export available creator-provided or automatic captions as TXT files without downloading the full video.

## Features

- Download public videos from YouTube, Instagram, Threads, X, and Facebook.
- Save MP4 files, merge available video and audio, and convert to H.264/AAC when needed.
- Export available captions as readable TXT transcripts in the macOS app.
- Resolve supported Instagram attachment cards from Threads links in the macOS app, skipping photo-only items in mixed posts.
- Choose a destination folder, preserve existing files, and cancel running jobs.

## Platform support

| Feature | macOS app | Web version |
|---|---|---|
| YouTube videos and Shorts | Supported | Supported |
| Public Instagram, Threads, X, and Facebook videos | Supported, subject to platform access | Public single-video posts |
| Multiple directly attached Threads videos | Video selection | Single-video posts only |
| Instagram attachment cards shared on Threads | Supported | Not supported |
| Captions to TXT | Supported | Not supported |
| Speech recognition for videos without captions | Not supported | Not supported |

Availability depends on each platform's public responses and access restrictions. Transcripts come from existing captions; automatic captions may contain errors. Vitract does not generate or translate scripts, and caption export does not require a paid AI API.

## Repository layout

- `macOS버전/` — SwiftUI app, download and caption engine, build tools, and tests.
- `HTML버전/` — browser interface, Python server, platform adapters, and Docker deployment configuration.

This repository contains source code. Built applications, downloaded media, user job data, and credentials are excluded. Committed MP4 test fixtures are locally generated synthetic media.

## Build the macOS app

Requirements: an Apple Silicon Mac, Xcode Command Line Tools, Python 3, and Homebrew. The minimum macOS version depends on the bundled media tools; the current local build requires macOS 26 or later.

```sh
brew install ffmpeg node
cd macOS버전
curl -fL https://github.com/yt-dlp/yt-dlp/releases/download/2026.08.19/yt-dlp_macos -o Tools/yt-dlp
chmod +x Tools/yt-dlp
python3 build_app.py
```

The builder checks yt-dlp against the official checksum stored in the repository and bundles the required libraries. The output is `build/Vitract.app`, signed ad hoc for local use. It is not an Apple-notarized distribution.

The interface is currently in Korean. To save a video, paste its link, select **영상 확인** (Check video), and then **MP4 저장** (Save MP4). To export captions, select an available caption language and **스크립트 저장 (TXT)** (Save transcript).

Run the offline checks from `macOS버전/`:

```sh
python3 Tests/test_threads.py
python3 Tests/run_tests.py
```

The Threads parser regression suite covers verified attachment cards, rejected links, post identity, direct-video priority, and mixed photo/video selection. The 13 backend tests cover caption export, file collision protection, MP4 conversion, cancellation, and other app behavior using synthetic fixtures. These checks do not guarantee availability across all external platforms.

## Run the web version

Requirements: Python 3.10 or later, yt-dlp, FFmpeg/FFprobe, and Node.js.

```sh
cd HTML버전
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python3 launch.py
```

See the [web deployment guide](HTML버전/서버배포안내.txt) for server and Docker/HTTPS setup. That guide is currently in Korean. Some internal web components retain the previous `clipsave` name.

## Usage and third-party tools

Use Vitract for content you own or have permission to download and copy. It does not bypass login requirements, DRM, or paid-content restrictions.

yt-dlp, FFmpeg, Node.js, and other bundled tools retain their respective licenses. See [third-party notices](macOS버전/THIRD_PARTY_NOTICES.md). A separate open-source license has not yet been assigned to this repository's own code.

## Changes

### 1.2.2

- Resolve verified Instagram attachment cards from public Threads embeds while keeping the original Threads link as the app input.
- Filter photo-only entries before processing mixed shared posts, preventing a photo from failing an otherwise valid video download.
- Preserve directly attached Threads videos and multiple-video selection.
- Show specific messages when a Threads embed or shared source has no extractable public video.
- Add five offline Threads regression tests. Verify the reported failure case through metadata extraction and MP4 download.

### 1.2.1

- Introduce the Vitract V symbol, teal interface, and dedicated app identifier.
- Normalize Threads `/media` links and support selecting among multiple videos in a post.
- Validate inspection, selection, and MP4 saving with a two-video Threads post.

### 1.2.0

- Replace YouTube-only URL validation with platform-specific validation in the macOS app.
- Bundle a public Threads video extractor.
- Offer original quality for social posts that do not provide video dimensions. Use individual post URLs instead of profiles, lists, or shortened share links.
