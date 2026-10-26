#!/usr/bin/env python3
"""Build the local Apple Silicon app, including its relocatable media tools.

Run `python3 build_app.py`, or package dependencies before the UI exists with
`python3 build_app.py --package-tools-only`. No files outside this project are
modified. Original tool binaries are never patched.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parent
BUILD = ROOT / "build"
APP = (BUILD / "Vitract.app").resolve()
CONTENTS = APP / "Contents"
RESOURCES = CONTENTS / "Resources"
TOOLS = RESOURCES / "tools"
LIB = RESOURCES / "lib"
MANIFEST = RESOURCES / "dependency-manifest.json"
EXECUTABLE = CONTENTS / "MacOS" / "VideoSaver"
REVISION = 3


def run(args, *, capture=True, **kwargs):
    result = subprocess.run([str(x) for x in args], check=True, text=True,
                            stdout=subprocess.PIPE if capture else None,
                            stderr=subprocess.PIPE if capture else None, **kwargs)
    return result.stdout.strip() if capture else ""


def sha256(path):
    digest = hashlib.sha256()
    with open(path, "rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def write_if_changed(path, content):
    data = content.encode() if isinstance(content, str) else content
    path.parent.mkdir(parents=True, exist_ok=True)
    if not path.exists() or path.read_bytes() != data:
        path.write_bytes(data)


def load_commands(path):
    output = run(["/usr/bin/otool", "-l", path])
    dependencies, rpaths, minimums = [], [], []
    for block in output.split("Load command ")[1:]:
        if re.search(r"cmd LC_(?:LOAD|LOAD_WEAK|REEXPORT)_DYLIB\b", block):
            name = re.search(r"\n\s+name (.+?) \(offset", block)
            if name and name[1] not in dependencies:
                dependencies.append(name[1])
        elif re.search(r"cmd LC_RPATH\b", block):
            value = re.search(r"\n\s+path (.+?) \(offset", block)
            if value and value[1] not in rpaths:
                rpaths.append(value[1])
        if re.search(r"cmd LC_BUILD_VERSION\b", block):
            value = re.search(r"\bminos ([\d.]+)", block)
            if value:
                minimums.append(value[1])
        if re.search(r"cmd LC_VERSION_MIN_MACOSX\b", block):
            value = re.search(r"\bversion ([\d.]+)", block)
            if value:
                minimums.append(value[1])
    return {"dependencies": dependencies, "rpaths": rpaths, "minimums": minimums}


def system_library(name):
    return name.startswith(("/usr/lib/", "/System/Library/"))


def expand_loader(value, source, executable):
    return Path(value.replace("@loader_path", str(source.parent))
                .replace("@executable_path", str(executable.parent)))


def resolve_dependency(name, source, executable, commands, executable_commands):
    if name.startswith("@rpath/"):
        suffix = name[len("@rpath/"):]
        candidates = [expand_loader(p, source, executable) / suffix
                      for p in commands["rpaths"]]
        candidates += [expand_loader(p, executable, executable) / suffix
                       for p in executable_commands["rpaths"]]
    else:
        candidates = [expand_loader(name, source, executable)]
    for candidate in candidates:
        if candidate.is_file():
            return candidate.resolve()
    raise RuntimeError(f"Cannot resolve {name} required by {source}")


def dependency_graph(sources):
    graph, destinations = {}, {}
    for name, source in sources.items():
        if not source.is_file():
            raise RuntimeError(f"Missing required tool: {source}")
        destinations[source.resolve()] = TOOLS / name
    queue = [(p.resolve(), p.resolve()) for p in sources.values()]
    command_cache = {}
    while queue:
        source, executable = queue.pop(0)
        if source in graph:
            continue
        commands = command_cache.setdefault(source, load_commands(source))
        executable_commands = command_cache.get(executable) or load_commands(executable)
        edges = {}
        for name in commands["dependencies"]:
            if system_library(name):
                continue
            resolved = resolve_dependency(name, source, executable, commands, executable_commands)
            destination = LIB / resolved.name
            for existing, path in destinations.items():
                if path == destination and existing != resolved:
                    raise RuntimeError(f"Dylib name collision: {existing} and {resolved}")
            destinations.setdefault(resolved, destination)
            edges[name] = resolved
            queue.append((resolved, executable))
        graph[source] = {**commands, "edges": edges}
    return graph, destinations


def package_metadata(source):
    parts = source.parts
    if "Cellar" not in parts:
        return None
    index = parts.index("Cellar")
    if len(parts) < index + 3:
        return None
    name, version = parts[index + 1:index + 3]
    prefix = Path(*parts[:index + 3])
    formula_files = list((prefix / ".brew").glob("*.rb"))
    formula = formula_files[0].read_text() if formula_files else ""
    def field(key):
        match = re.search(rf'^\s*{key} "([^\"]+)"', formula, re.MULTILINE)
        return match[1] if match else None
    return {"name": name, "version": version, "homepage": field("homepage"),
            "source_url": field("url"), "source_sha256": field("sha256"),
            "license": field("license"), "prefix": str(prefix)}


def copy_licenses(packages):
    notices = RESOURCES / "ThirdPartyNotices"
    for package in packages.values():
        prefix = Path(package["prefix"])
        dest = notices / f"{package['name']}-{package['version']}"
        candidates = [p for p in prefix.iterdir() if p.is_file() and
                      p.name.upper().startswith(("LICENSE", "LICENCE", "COPYING", "NOTICE", "COPYRIGHT"))]
        candidates += list((prefix / ".brew").glob("*.rb"))
        for path in candidates:
            write_if_changed(dest / path.name, path.read_bytes())
        receipt = prefix / "INSTALL_RECEIPT.json"
        if receipt.exists():
            write_if_changed(dest / receipt.name, receipt.read_bytes())
    for path in (ROOT / "Tools").glob("*LICENSE*"):
        write_if_changed(notices / "yt-dlp" / path.name, path.read_bytes())
    write_if_changed(RESOURCES / "THIRD_PARTY_NOTICES.md", (ROOT / "THIRD_PARTY_NOTICES.md").read_bytes())


def package_tools():
    sources = {"yt-dlp": ROOT / "Tools" / "yt-dlp",
               **{name: Path("/opt/homebrew/bin") / name for name in ("ffmpeg", "ffprobe", "node")}}
    checksums = ROOT / "Tools" / "SHA2-256SUMS"
    if not checksums.is_file():
        raise RuntimeError("Missing official Tools/SHA2-256SUMS for yt-dlp verification.")
    checksum_match = re.search(r"^([0-9a-f]{64})\s+yt-dlp_macos$", checksums.read_text(), re.MULTILINE)
    if not checksum_match or sha256(sources["yt-dlp"]) != checksum_match[1]:
        raise RuntimeError("yt-dlp binary does not match the official macOS release checksum.")
    TOOLS.mkdir(parents=True, exist_ok=True)
    LIB.mkdir(parents=True, exist_ok=True)
    graph, destinations = dependency_graph(sources)
    previous = json.loads(MANIFEST.read_text()) if MANIFEST.exists() else {}
    old_files = previous.get("files", {}) if previous.get("packaging_revision") == REVISION else {}
    files, packages, minimums = {}, {}, ["14.0"]
    copied = 0
    for source, commands in graph.items():
        destination = destinations[source]
        relative = str(destination.relative_to(RESOURCES))
        fingerprint = sha256(source)
        edges = {name: "@loader_path/" + os.path.relpath(destinations[target], destination.parent)
                 for name, target in commands["edges"].items()}
        old = old_files.get(relative, {})
        unchanged = (destination.is_file() and old.get("source_sha256") == fingerprint
                     and old.get("rewritten_dependencies") == edges
                     and old.get("bundled_sha256") == sha256(destination))
        if not unchanged:
            shutil.copyfile(source, destination)
            destination.chmod(0o755)
            if destination.parent == LIB:
                run(["/usr/bin/install_name_tool", "-id", "@loader_path/" + destination.name, destination])
            for original, rewritten in edges.items():
                run(["/usr/bin/install_name_tool", "-change", original, rewritten, destination])
            # All non-system dependency paths are rewritten above. Discard build-machine rpaths.
            for rpath in commands["rpaths"]:
                if rpath.startswith(("/opt/homebrew/", "/usr/local/")):
                    run(["/usr/bin/install_name_tool", "-delete_rpath", rpath, destination])
            run(["/usr/bin/codesign", "--force", "--sign", "-", destination])
            copied += 1
        packaged = load_commands(destination)
        for dependency in packaged["dependencies"]:
            if not system_library(dependency):
                if not dependency.startswith("@loader_path/"):
                    raise RuntimeError(f"Nonportable dependency left in {destination}: {dependency}")
                resolved = expand_loader(dependency, destination, destination)
                if not resolved.is_file():
                    # Other files may not be copied yet; graph ensures their eventual presence.
                    if resolved.resolve() not in {p.resolve() for p in destinations.values()}:
                        raise RuntimeError(f"Missing bundled dependency {resolved}")
        minimums.extend(commands["minimums"])
        metadata = package_metadata(source)
        if metadata:
            packages[metadata["name"]] = metadata
        files[relative] = {"source": str(source), "source_sha256": fingerprint,
                           "bundled_sha256": sha256(destination),
                           "rewritten_dependencies": edges}
    clean_env = {**os.environ, "PATH": "/usr/bin:/bin:/usr/sbin:/sbin"}
    clean_env.pop("DYLD_LIBRARY_PATH", None)
    clean_env.pop("DYLD_FALLBACK_LIBRARY_PATH", None)
    versions = previous.get("tool_versions", {}) if copied == 0 else {}
    if set(versions) != set(sources):
        versions = {"yt-dlp": run([TOOLS / "yt-dlp", "--version"], env=clean_env, timeout=90),
                    "ffmpeg": run([TOOLS / "ffmpeg", "-version"], env=clean_env, timeout=30).splitlines()[0],
                    "ffprobe": run([TOOLS / "ffprobe", "-version"], env=clean_env, timeout=30).splitlines()[0],
                    "node": run([TOOLS / "node", "--version"], env=clean_env, timeout=30)}
    version = versions["yt-dlp"]
    packages["yt-dlp"] = {"name": "yt-dlp", "version": version,
        "homepage": "https://github.com/yt-dlp/yt-dlp", "license": "Unlicense (project); bundled dependencies retain their own licenses",
        "source_url": f"https://github.com/yt-dlp/yt-dlp/archive/refs/tags/{version}.tar.gz",
        "binary_url": f"https://github.com/yt-dlp/yt-dlp/releases/download/{version}/yt-dlp_macos"}
    minimum = max(minimums, key=lambda value: tuple(map(int, value.split("."))))
    manifest = {"packaging_revision": REVISION, "architecture": "arm64",
        "minimum_macos": minimum, "tool_versions": versions, "files": files,
        "packages": {name: {k: v for k, v in info.items() if k != "prefix"}
                     for name, info in sorted(packages.items())}}
    copy_licenses({name: p for name, p in packages.items() if "prefix" in p})
    write_if_changed(RESOURCES / "ThirdPartyNotices" / "yt-dlp" / "SHA2-256SUMS", checksums.read_bytes())
    write_if_changed(MANIFEST, json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(f"Media tools verified: {len(graph)} binaries, {copied} refreshed; macOS {minimum}+.", flush=True)
    return minimum


ICON_SWIFT = r'''
import AppKit
let output = CommandLine.arguments[1]
let sizes = [16, 32, 128, 256, 512]
for size in sizes {
    for scale in [1, 2] {
        let pixels = size * scale
        let image = NSImage(size: NSSize(width: pixels, height: pixels))
        image.lockFocus()
        let ctx = NSGraphicsContext.current!.cgContext
        ctx.scaleBy(x: CGFloat(pixels) / 1024, y: CGFloat(pixels) / 1024)
        let rect = CGRect(x: 48, y: 48, width: 928, height: 928)
        let bg = NSBezierPath(roundedRect: rect, xRadius: 220, yRadius: 220)
        NSColor(calibratedRed: 0.97, green: 0.97, blue: 0.98, alpha: 1).setFill()
        bg.fill()
        let gradient = NSGradient(colors: [NSColor(calibratedRed: 1.0, green: 0.12, blue: 0.18, alpha: 1), NSColor(calibratedRed: 0.89, green: 0.02, blue: 0.08, alpha: 1)])!
        let card = NSBezierPath(roundedRect: NSRect(x: 206, y: 322, width: 612, height: 430), xRadius: 76, yRadius: 76)
        gradient.draw(in: card, angle: -50)
        let play = NSBezierPath()
        play.move(to: NSPoint(x: 434, y: 445)); play.line(to: NSPoint(x: 434, y: 638)); play.line(to: NSPoint(x: 600, y: 541)); play.close()
        NSColor.white.setFill(); play.fill()
        let badge = NSBezierPath(ovalIn: NSRect(x: 566, y: 126, width: 284, height: 324))
        NSColor(calibratedRed: 0.88, green: 0.02, blue: 0.08, alpha: 1).setFill(); badge.fill()
        let arrow = NSBezierPath()
        arrow.lineWidth = 52; arrow.lineCapStyle = .round; arrow.lineJoinStyle = .round
        arrow.move(to: NSPoint(x: 708, y: 410)); arrow.line(to: NSPoint(x: 708, y: 202))
        arrow.move(to: NSPoint(x: 620, y: 282)); arrow.line(to: NSPoint(x: 708, y: 194)); arrow.line(to: NSPoint(x: 796, y: 282))
        NSColor.white.setStroke(); arrow.stroke()
        image.unlockFocus()
        let bitmap = NSBitmapImageRep(data: image.tiffRepresentation!)!
        let suffix = scale == 1 ? "" : "@2x"
        let path = "\(output)/icon_\(size)x\(size)\(suffix).png"
        try bitmap.representation(using: .png, properties: [:])!.write(to: URL(fileURLWithPath: path))
    }
}
'''


def build_icon():
    iconset = BUILD / "VideoSaver.iconset"
    iconset.mkdir(parents=True, exist_ok=True)
    source = BUILD / "draw-icon.swift"
    target = RESOURCES / "AppIcon.icns"
    changed = not source.exists() or source.read_text() != ICON_SWIFT
    write_if_changed(source, ICON_SWIFT)
    if changed or not target.exists():
        run(["/usr/bin/swift", "-module-cache-path", BUILD / "ModuleCache", source, iconset])
        run(["/usr/bin/iconutil", "-c", "icns", iconset, "-o", target])


def build_app(minimum):
    sources = sorted((ROOT / "Sources").glob("*.swift"))
    if not sources or not (ROOT / "Sources" / "App.swift").exists():
        raise RuntimeError("Sources/App.swift is not ready; use --package-tools-only.")
    EXECUTABLE.parent.mkdir(parents=True, exist_ok=True)
    run(["/usr/bin/swiftc", "-parse-as-library", "-O", "-target", "arm64-apple-macosx14.0",
         "-module-cache-path", BUILD / "ModuleCache", "-framework", "SwiftUI", "-framework", "AppKit",
         *sources, "-o", EXECUTABLE], capture=False)
    shutil.copytree(ROOT / "Plugins", RESOURCES / "plugins", dirs_exist_ok=True)
    build_icon()
    plist = {"CFBundleExecutable": "VideoSaver", "CFBundleIdentifier": "local.clipdock.videosaver",
        "CFBundleName": "Vitract", "CFBundleDisplayName": "Vitract", "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": "1.2.0", "CFBundleVersion": "3",
        "CFBundleIconFile": "AppIcon", "LSMinimumSystemVersion": minimum,
        "LSApplicationCategoryType": "public.app-category.video", "NSHighResolutionCapable": True,
        "NSPrincipalClass": "NSApplication", "NSHumanReadableCopyright": "© 2026 Vitract. Third-party notices included."}
    write_if_changed(CONTENTS / "Info.plist", plistlib.dumps(plist, sort_keys=True))
    write_if_changed(CONTENTS / "PkgInfo", b"APPL????")
    run(["/usr/bin/codesign", "--force", "--sign", "-", APP])
    run(["/usr/bin/codesign", "--verify", "--deep", "--strict", APP])
    print(f"Built and verified: {APP}", flush=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--package-tools-only", action="store_true")
    args = parser.parse_args()
    BUILD.mkdir(parents=True, exist_ok=True)
    minimum = package_tools()
    if not args.package_tools_only:
        build_app(minimum)


if __name__ == "__main__":
    try:
        main()
    except subprocess.CalledProcessError as error:
        print(error.stderr or f"Command failed: {error.cmd}", file=sys.stderr)
        raise SystemExit(error.returncode)
    except subprocess.TimeoutExpired as error:
        print(f"Tool verification timed out: {error.cmd}", file=sys.stderr)
        raise SystemExit(1)
    except (RuntimeError, OSError) as error:
        print(f"Build failed: {error}", file=sys.stderr)
        raise SystemExit(1)
