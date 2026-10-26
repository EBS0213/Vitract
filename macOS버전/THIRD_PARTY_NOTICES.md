# Vitract — third-party components

This personal, local macOS application runs the following independent command-line
tools as child processes. Their original licenses and copyrights remain in force.
The application does not include, grant rights to, or distribute downloaded videos.

- **yt-dlp** — <https://github.com/yt-dlp/yt-dlp>. The project source is distributed
  under the Unlicense; the standalone release also contains third-party libraries
  with their own licenses. The exact release, official binary URL, source URL, and
  SHA-256 are recorded in `dependency-manifest.json` inside the app's Resources.
- **FFmpeg and ffprobe 9.0.1** — <https://ffmpeg.org/>. This Homebrew build enables
  `--enable-gpl` and `--enable-version3`, so its applicable license is
  **GPL-3.0-or-later**. Its source is
  <https://ffmpeg.org/releases/ffmpeg-9.0.1.tar.xz>. The original FFmpeg license
  files, complete local Homebrew formula, and installation receipt are bundled in
  `Resources/ThirdPartyNotices/ffmpeg-9.0.1_1/`.
- **Node.js 25.8.2** — <https://nodejs.org/>. Node is used by yt-dlp's JavaScript
  challenge support. Node's license file includes the MIT license and the notices
  for its bundled third-party code. Original notices and its local build formula
  are bundled in `Resources/ThirdPartyNotices/node-25.8.2/`.
- **Transitive media/runtime libraries** — Required dynamic libraries are copied
  into `Resources/lib`. Their exact versions, source archive URLs, checksums,
  source and bundled binary hashes, and rewritten linkage are recorded in
  `Resources/dependency-manifest.json`. Available original license/copyright files,
  build formulas, and installation receipts are copied per component into
  `Resources/ThirdPartyNotices/`.

`build_app.py` copies these tools without altering the installed originals. It
changes only Mach-O library references and signatures in the application bundle,
using bundle-relative paths and local ad-hoc signatures. The package needs no
Homebrew installation at runtime. This build targets Apple Silicon and requires
macOS 26 or later because that is the minimum supported by its local FFmpeg build.

The source application and build script are delivered alongside the local app.
This package is intended for the owner's use on this Mac. Before distributing a
binary package to others, the distributor must satisfy each component's license,
including any GPL source-code and notice obligations; source archive links alone
are not represented here as a complete GPL binary-redistribution arrangement.
