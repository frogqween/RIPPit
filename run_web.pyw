#!/usr/bin/env pythonw
from __future__ import annotations

import logging
import os
import sys
import socket
import threading
import time
import urllib.request
import webbrowser
import random
from pathlib import Path

import uvicorn

# Ensure project root on sys.path
ROOT = Path(__file__).resolve().parent
# Ensure project root on sys.path
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app import app  # noqa: E402

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)
LOG_FILE = LOG_DIR / "server.log"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
    ],
)
log = logging.getLogger("rippit.launcher")

HOST = "127.0.0.1"


def _can_bind(port: int) -> bool:
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        s.bind((HOST, port))
        return True
    except OSError:
        return False
    finally:
        try:
            s.close()
        except Exception:
            pass


def _pick_port() -> int:
    # Try a few common ports first, then random high ports to avoid races
    candidates = list(range(8000, 8011))
    random.shuffle(candidates)
    for p in candidates:
        if _can_bind(p):
            return p
    for _ in range(50):
        p = random.randint(49152, 65535)
        if _can_bind(p):
            return p
    # Last resort: let OS choose (0). We won't know the port to open browser reliably.
    return 0


PORT = _pick_port()
HEALTH = f"http://{HOST}:{PORT}/api/health" if PORT else None
HOME = f"http://{HOST}:{PORT}/" if PORT else None


def _wait_and_open(port: int):
    health = f"http://{HOST}:{port}/api/health"
    home = f"http://{HOST}:{port}/"
    # Try health first, but also open UI after a short delay so it appears quickly
    opened = False
    for i in range(40):  # ~20s max
        try:
            with urllib.request.urlopen(health, timeout=2) as r:  # nosec - local only
                if r.status == 200 and not opened:
                    webbrowser.open(home, new=2)
                    opened = True
                    log.info("Health OK; browser opened on %s", home)
                    break
        except Exception:
            pass
        if i == 3 and not opened:  # ~2s
            try:
                webbrowser.open(home, new=2)
                opened = True
                log.info("Opened browser early while waiting for health: %s", home)
            except Exception:
                pass
        time.sleep(0.5)


def main():
    global PORT
    attempts = 0
    while True:
        attempts += 1
        if PORT == 0 or not _can_bind(PORT):
            PORT = _pick_port()
        log.info("Starting RIPPit server on %s:%s", HOST, PORT)
        t = threading.Thread(target=_wait_and_open, args=(PORT,), daemon=True)
        t.start()
        # Run uvicorn in-process; disable default console logging (pythonw has no stdout)
        config = uvicorn.Config(
            app=app,
            host=HOST,
            port=PORT,
            log_level="warning",
            log_config=None,
            access_log=False,
            loop="asyncio",
        )
        server = uvicorn.Server(config)
        try:
            server.run()
            break
        except OSError as e:
            # Port conflict/race: retry with a new port a few times
            log.warning("Uvicorn failed to bind on %s:%s (%s); retrying with a new port", HOST, PORT, e)
            if attempts >= 5:
                raise
            time.sleep(0.5)
            PORT = 0


if __name__ == "__main__":
    try:
        main()
    except Exception as e:
        log.exception("Launcher failed: %s", e)
