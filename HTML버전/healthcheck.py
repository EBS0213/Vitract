#!/usr/bin/env python3
"""Container readiness check with the configured public Host header."""
import json
import os
import sys
import urllib.parse
import urllib.request

origin = os.environ.get("PUBLIC_ORIGIN", "http://localhost:8766")
host = urllib.parse.urlsplit(origin).netloc
request = urllib.request.Request("http://127.0.0.1:8766/api/health", headers={"Host": host})
try:
    with urllib.request.urlopen(request, timeout=5) as response:
        result = json.load(response)
    sys.exit(0 if result.get("application") == "videosaver" and result.get("status") == "ok" and result.get("ready") is True else 1)
except Exception:
    sys.exit(1)
