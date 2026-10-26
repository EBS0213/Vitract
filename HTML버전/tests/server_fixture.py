"""Launch the actual HTTP server with only the Threads network CLI replaced."""
from pathlib import Path
import sys

TESTS = Path(__file__).resolve().parent
sys.path.insert(0, str(TESTS.parent))
import engine

engine.THREADS_ADAPTER_PATH = TESTS / "fake_threads_adapter.py"
import server

if __name__ == "__main__":
    server.main()
