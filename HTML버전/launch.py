#!/usr/bin/env python3
"""Start the local HTML application; keep its server attached to this terminal."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
import urllib.error
import urllib.request
import venv
import webbrowser

ROOT = Path(__file__).resolve().parent


def runtime_works(executable):
    try:
        result = subprocess.run([str(executable), "-c", "import sys, fastapi, uvicorn, pydantic; from pydantic import ConfigDict; assert sys.version_info >= (3, 10); assert int(pydantic.__version__.split('.')[0]) >= 2"],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10)
        return result.returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def runtime():
    python = ROOT / ".venv" / "bin" / "python"
    for candidate in (python, Path(sys.executable)):
        if runtime_works(candidate):
            return candidate
    if sys.version_info < (3, 10):
        raise RuntimeError("Python 3.10 이상이 필요합니다. Python 설치 후 다시 실행해 주세요.")
    print("첫 실행에 필요한 웹 서버 패키지를 전용 환경에 설치합니다.", flush=True)
    if not python.exists():
        venv.EnvBuilder(with_pip=True).create(ROOT / ".venv")
    subprocess.run([str(python), "-m", "pip", "install", "-r", str(ROOT / "requirements-runtime.txt")], check=True)
    return python


def health(url):
    try:
        with urllib.request.urlopen(url + "/api/health", timeout=2) as response:
            result = json.load(response)
        return result if result.get("application") == "videosaver" else None
    except (OSError, ValueError, urllib.error.URLError):
        return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8766)
    parser.add_argument("--no-browser", action="store_true")
    parser.add_argument("--check", action="store_true", help="Check runtime availability without starting a server")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("--port는 1부터 65535까지입니다.")
    python = runtime()
    if args.check:
        print(json.dumps({"python": str(python), "root": str(ROOT), "runtime_ready": True}, ensure_ascii=False))
        return
    url = "http://127.0.0.1:" + str(args.port)
    if health(url):
        print("이미 실행 중인 clipsave를 엽니다: " + url)
        if not args.no_browser:
            webbrowser.open(url)
        return
    env = os.environ.copy()
    env.setdefault("VIDEO_DATA_DIR", str(ROOT / "data"))
    process = subprocess.Popen([str(python), str(ROOT / "server.py"), "--host", "127.0.0.1", "--port", str(args.port)],
                               cwd=ROOT, env=env)
    try:
        for _ in range(120):
            if process.poll() is not None:
                raise RuntimeError("웹 서버가 시작되지 않았습니다. 위 오류를 확인해 주세요.")
            if health(url):
                print("clipsave: " + url, flush=True)
                print("이 터미널을 열어 두세요. 종료하려면 Control+C를 누르세요.", flush=True)
                if not args.no_browser:
                    webbrowser.open(url)
                break
            time.sleep(0.25)
        else:
            raise RuntimeError("서버 시작 시간이 초과됐습니다. 사용 중인 포트와 실행 환경을 확인해 주세요.")
        process.wait()
    except KeyboardInterrupt:
        print("\nclipsave 웹 서버를 종료합니다.")
    finally:
        if process.poll() is None:
            process.send_signal(signal.SIGINT)
            try:
                process.wait(timeout=20)
            except subprocess.TimeoutExpired:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print("실행 오류: " + str(error), file=sys.stderr)
        sys.exit(1)
