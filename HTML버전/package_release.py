#!/usr/bin/env python3
"""Build a portable web source ZIP from an explicit, reviewable allowlist."""
from __future__ import annotations
import argparse
import datetime as dt
import hashlib
import json
import os
from pathlib import Path
import tempfile
import zipfile

ROOT = Path(__file__).resolve().parent
FILES = (
    "server.py", "engine.py", "platforms.py", "threads_adapter.py", "index.html", "styles.css", "app.js",
    "requirements.txt", "requirements-runtime.txt", "Dockerfile", "compose.yaml",
    "compose.https.yaml", "Caddyfile", "healthcheck.py", "launch.py",
    ".env.example", ".dockerignore", ".gitignore", "웹버전실행.command",
    "시작하기.txt", "서버배포안내.txt", "광고영역안내.txt", "package_release.py",
)
PREFIX = "clipsave_웹배포/"


def build(output: Path, replace: bool = False) -> dict:
    output = output.expanduser().absolute()
    if output.is_symlink() or (output.exists() and (not replace or not output.is_file())):
        raise RuntimeError("기존 파일을 덮어쓰지 않습니다. 교체하려면 --replace를 사용하세요: " + str(output))
    entries = []
    for relative in FILES:
        path = ROOT / relative
        if path.is_symlink() or not path.is_file() or path.resolve().parent != ROOT:
            raise RuntimeError("필수 배포 파일이 없거나 심볼릭 링크입니다: " + relative)
        entries.append((relative, path.read_bytes()))
    manifest = {
        "format_version": 1,
        "created_at": dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds"),
        "files": [{"path": name, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()} for name, data in entries],
        "excluded": [".env", "data/", ".venv/", "tests/", "logs", "media", "database", "secret.key", "external tool executables"],
    }
    entries.append(("배포파일목록.json", (json.dumps(manifest, ensure_ascii=False, indent=2) + "\n").encode()))
    output.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".videosaver-release-", suffix=".zip", dir=output.parent)
    os.close(fd)
    temporary = Path(temporary)
    try:
        with zipfile.ZipFile(temporary, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
            for name, data in entries:
                item = zipfile.ZipInfo(PREFIX + name, dt.datetime.now().timetuple()[:6])
                item.compress_type = zipfile.ZIP_DEFLATED
                item.create_system = 3
                item.external_attr = (0o100755 if name.endswith(".command") else 0o100644) << 16
                archive.writestr(item, data)
        with zipfile.ZipFile(temporary) as archive:
            assert archive.testzip() is None
            assert archive.namelist() == [PREFIX + name for name, _ in entries]
            for name, data in entries:
                assert archive.read(PREFIX + name) == data
        os.chmod(temporary, 0o644)
        if replace:
            if output.is_symlink():
                raise RuntimeError("심볼릭 링크를 교체하지 않습니다.")
            os.replace(temporary, output)
        else:
            os.link(temporary, output)  # Exclusive publication; never clobber a race.
            temporary.unlink()
        return {"output": str(output), "files": len(entries), "bytes": output.stat().st_size, "sha256": hashlib.sha256(output.read_bytes()).hexdigest(), "verified": True}
    finally:
        temporary.unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / "clipsave_웹배포.zip")
    parser.add_argument("--replace", action="store_true")
    args = parser.parse_args()
    print(json.dumps(build(args.output, args.replace), ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
