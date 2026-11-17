"""Paths and settings shared by every part of the Librarian.

The Librarian lives in librarian/ next to the web app and runs from its own venv. It
reaches the web app's data only through the app's own db.py (imported from
APP_DIR), so every write to library.db still goes through one module.
"""

import os
import sys
from pathlib import Path

LIB_DIR = Path(__file__).resolve().parent.parent      # <repo>/librarian
APP_DIR = LIB_DIR.parent                               # <repo>
PAPERS_DIR = APP_DIR / "papers"

DB_PATH = LIB_DIR / "librarian.db"
NOTES_DIR = LIB_DIR / "notes"
CACHE_DIR = LIB_DIR / "cache"
TEXT_CACHE = CACHE_DIR / "text"
PDF_CACHE = CACHE_DIR / "pdf"
ORX_CACHE = CACHE_DIR / "orx"
MODEL_CACHE = CACHE_DIR / "models"
WORK_DIR = LIB_DIR / "work"          # empty cwd for `claude -p` children
PROMPTS_DIR = Path(__file__).resolve().parent / "prompts"
CONTEXT_PATH = LIB_DIR / "brief.md"                  # yours; gitignored
TEMPLATE_PATH = LIB_DIR / "brief.template.md"         # copied to brief.md on first run
DIGEST_PATH = LIB_DIR / "LIBRARY.md"
ENV_PATH = LIB_DIR / ".env"


def load_env():
    """Read librarian/.env (KEY=VALUE lines) into os.environ, without overriding."""
    if not ENV_PATH.exists():
        return
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


load_env()      # before anything below reads os.environ

DAEMON_HOST = "127.0.0.1"
DAEMON_PORT = int(os.environ.get("LIBRARIAN_PORT", "8766"))
APP_URL = os.environ.get("LIBRARIAN_APP_URL", "http://127.0.0.1:8765")

# `claude` CLI model aliases; override in librarian/.env.
TRIAGE_MODEL = os.environ.get("LIBRARIAN_TRIAGE_MODEL", "haiku")
DEEP_MODEL = os.environ.get("LIBRARIAN_DEEP_MODEL", "sonnet")

# Thresholds.
RESOLVE_MIN = 0.90       # orx/OpenAlex title match accepted
RESOLVE_UNCERTAIN = 0.80
PDF_LINK_MIN = 0.92      # auto-link a dropped PDF to an existing row
REF_MATCH_MIN = 0.90
DEEP_MIN_RELEVANCE = 2
MAX_PDF_BYTES = 60 * 1024 * 1024


def ensure_dirs():
    for d in (NOTES_DIR, TEXT_CACHE, PDF_CACHE, ORX_CACHE, MODEL_CACHE, WORK_DIR):
        d.mkdir(parents=True, exist_ok=True)


def app_db():
    """The web app's db module (stdlib-only), imported from the repo root."""
    if str(APP_DIR) not in sys.path:
        sys.path.insert(0, str(APP_DIR))
    import db  # noqa: E402  (the app's module, not ours)
    return db

