"""Development / single-host entry point.

    python run.py                 # http://127.0.0.1:8000
    MANIAC_HOST=0.0.0.0 python run.py
"""
import io
import os
import sys

import uvicorn

if os.name == "nt":  # UTF-8 console on Windows
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")


def main() -> None:
    host = os.environ.get("MANIAC_HOST", "127.0.0.1")
    port = int(os.environ.get("MANIAC_PORT", "8000"))
    reload = os.environ.get("MANIAC_RELOAD", "0") == "1"
    print("=" * 60)
    print("  MANIAC MEDIA  -  unified movie & TV automation")
    print(f"  http://{host}:{port}    API docs: http://{host}:{port}/api/docs")
    print("=" * 60)
    uvicorn.run("backend.main:app", host=host, port=port, reload=reload, log_config=None, proxy_headers=True)


if __name__ == "__main__":
    main()
