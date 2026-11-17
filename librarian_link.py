"""The web app's side of the Librarian: start/stop its daemon, notify it of
changes, and proxy /api/librarian/* to it. Standard library only.

The Librarian itself (librarian/) has dependencies and its own venv, so it
runs as a separate process on 127.0.0.1:8766. If librarian/.venv is missing
the app works exactly as before: every function here degrades to a no-op and
the UI hides the Librarian's panes.
"""

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
LIB_DIR = ROOT / "librarian"
PYTHON = LIB_DIR / ".venv" / "bin" / "python"
HOST, PORT = "127.0.0.1", int(os.environ.get("LIBRARIAN_PORT", "8766"))
BASE = f"http://{HOST}:{PORT}"
LOG_PATH = LIB_DIR / "work" / "daemon.log"

_proc = None


def installed():
    return PYTHON.exists()


def _get(path, timeout=3):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return json.loads(r.read().decode("utf-8"))


def healthy():
    try:
        return bool(_get("/health", 1).get("ok"))
    except Exception:
        return False


def start(wait=8.0):
    """Start the daemon unless one is already running. Returns a status line."""
    global _proc
    if not installed():
        return "not installed (run: cd librarian && uv sync)"
    if healthy():
        return "already running"
    LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    log = open(LOG_PATH, "a", encoding="utf-8")
    _proc = subprocess.Popen(
        [str(PYTHON), "-m", "librarian", "serve", "--port", str(PORT)],
        cwd=str(LIB_DIR), stdout=log, stderr=subprocess.STDOUT,
        env={**os.environ, "PYTHONPATH": str(LIB_DIR), "PYTHONUNBUFFERED": "1"},
        start_new_session=True)
    deadline = time.time() + wait
    while time.time() < deadline:
        if _proc.poll() is not None:
            return f"failed to start (see {LOG_PATH.relative_to(ROOT)})"
        if healthy():
            try:
                s = _get("/status")
                return f"running ({s['cataloged']}/{s['papers']} cataloged, {s['queued']} queued)"
            except Exception:
                return "running"
        time.sleep(0.25)
    return f"starting (slow; see {LOG_PATH.relative_to(ROOT)})"


def stop():
    """Stop the daemon we started (not one somebody else started)."""
    if _proc and _proc.poll() is None:
        try:
            os.killpg(os.getpgid(_proc.pid), signal.SIGTERM)
            _proc.wait(timeout=10)
        except (ProcessLookupError, PermissionError, subprocess.TimeoutExpired):
            try:
                os.killpg(os.getpgid(_proc.pid), signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass


def notify(path, payload):
    """Fire-and-forget POST (e.g. /enqueue after a paper is added)."""
    if not installed():
        return
    try:
        req = urllib.request.Request(BASE + path, data=json.dumps(payload).encode("utf-8"),
                                     headers={"Content-Type": "application/json"},
                                     method="POST")
        urllib.request.urlopen(req, timeout=2).close()
    except Exception:
        pass


def request(method, path, body=None, timeout=30):
    """(status, bytes, content_type) from the daemon, for the /api proxy."""
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(BASE + path, data=data, method=method,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read(), r.headers.get("Content-Type", "application/json")
    except urllib.error.HTTPError as e:
        return e.code, e.read(), "application/json"
    except Exception as e:
        msg = json.dumps({"error": f"librarian unavailable: {e}", "offline": True})
        return 503, msg.encode("utf-8"), "application/json"


def stream(path, body):
    """Open a streaming POST (SSE) to the daemon; returns the response object."""
    req = urllib.request.Request(BASE + path, data=json.dumps(body).encode("utf-8"),
                                 headers={"Content-Type": "application/json"}, method="POST")
    return urllib.request.urlopen(req, timeout=900)
