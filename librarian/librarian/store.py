"""Every SQL statement against librarian.db.

librarian.db is derived data: deleting it loses nothing that can't be rebuilt
(at the cost of tokens). The user's irreplaceable data stays in library.db,
which the Librarian only touches through the app's db.py (config.app_db()).
"""

import json
import sqlite3
import threading

from . import config

SCHEMA = """
PRAGMA journal_mode = WAL;

CREATE TABLE IF NOT EXISTS meta (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);

-- One row per library paper: everything the Librarian knows about it.
CREATE TABLE IF NOT EXISTS paper_state (
  paper_id         INTEGER PRIMARY KEY,
  doi              TEXT NOT NULL DEFAULT '',
  openalex_id      TEXT NOT NULL DEFAULT '',
  arxiv_id         TEXT NOT NULL DEFAULT '',
  resolve_state    TEXT NOT NULL DEFAULT 'pending', -- pending|resolved|uncertain|unresolved
  match_score      REAL,
  oa_title         TEXT NOT NULL DEFAULT '',
  oa_year          TEXT NOT NULL DEFAULT '',
  oa_authors       TEXT NOT NULL DEFAULT '',
  oa_venue         TEXT NOT NULL DEFAULT '',
  abstract         TEXT NOT NULL DEFAULT '',
  global_citations INTEGER,
  oa_pdf_url       TEXT NOT NULL DEFAULT '',
  text_source      TEXT NOT NULL DEFAULT '',  -- pdf|arxiv|oa_pdf|abstract|citation
  pdf_sha          TEXT NOT NULL DEFAULT '',
  relevance        INTEGER,
  confidence       REAL,
  claims           TEXT NOT NULL DEFAULT '[]',  -- key-claim ids from brief.md
  areas            TEXT NOT NULL DEFAULT '[]',
  gaps             TEXT NOT NULL DEFAULT '[]',
  suggested_tags   TEXT NOT NULL DEFAULT '[]',
  contradicts      TEXT NOT NULL DEFAULT '',
  summary          TEXT NOT NULL DEFAULT '',
  why              TEXT NOT NULL DEFAULT '',
  tier             TEXT NOT NULL DEFAULT '',  -- skim|deep|wanted
  triage_model     TEXT NOT NULL DEFAULT '',
  context_hash     TEXT NOT NULL DEFAULT '',
  note_path        TEXT NOT NULL DEFAULT '',
  note_model       TEXT NOT NULL DEFAULT '',
  refs_state       TEXT NOT NULL DEFAULT '',  -- ''|done|none
  embedded_hash    TEXT NOT NULL DEFAULT '',
  cataloged_at     TEXT,
  error            TEXT NOT NULL DEFAULT '',
  updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS jobs (
  id          INTEGER PRIMARY KEY,
  paper_id    INTEGER,
  kind        TEXT NOT NULL,           -- catalog|refs|embed|ghosts|digest|file
  arg         TEXT NOT NULL DEFAULT '',
  priority    INTEGER NOT NULL DEFAULT 5,
  state       TEXT NOT NULL DEFAULT 'queued',  -- queued|running|done|failed
  attempts    INTEGER NOT NULL DEFAULT 0,
  error       TEXT NOT NULL DEFAULT '',
  enqueued_at TEXT NOT NULL DEFAULT (datetime('now')),
  run_after   TEXT NOT NULL DEFAULT (datetime('now')),
  started_at  TEXT,
  finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_jobs_pick ON jobs(state, priority, id);
CREATE UNIQUE INDEX IF NOT EXISTS idx_jobs_live
  ON jobs(kind, IFNULL(paper_id, -1), arg) WHERE state IN ('queued', 'running');

-- References made by library papers.
CREATE TABLE IF NOT EXISTS refs (
  citing_id        INTEGER NOT NULL,
  ord              INTEGER NOT NULL,
  source           TEXT NOT NULL,       -- openalex|parsed
  raw              TEXT NOT NULL DEFAULT '',
  title            TEXT NOT NULL DEFAULT '',
  title_norm       TEXT NOT NULL DEFAULT '',
  year             TEXT NOT NULL DEFAULT '',
  doi              TEXT NOT NULL DEFAULT '',
  openalex_id      TEXT NOT NULL DEFAULT '',
  matched_paper_id INTEGER,
  match_score      REAL,
  ghost_id         INTEGER,
  PRIMARY KEY (citing_id, source, ord)
);
CREATE INDEX IF NOT EXISTS idx_refs_matched ON refs(matched_paper_id);
CREATE INDEX IF NOT EXISTS idx_refs_ghost ON refs(ghost_id);

-- Papers cited by the library but not in it.
CREATE TABLE IF NOT EXISTS ghosts (
  id               INTEGER PRIMARY KEY,
  key              TEXT NOT NULL UNIQUE,  -- doi:… | oa:W… | t:<normalised title>
  title            TEXT NOT NULL DEFAULT '',
  year             TEXT NOT NULL DEFAULT '',
  authors          TEXT NOT NULL DEFAULT '',
  venue            TEXT NOT NULL DEFAULT '',
  doi              TEXT NOT NULL DEFAULT '',
  openalex_id      TEXT NOT NULL DEFAULT '',
  abstract         TEXT NOT NULL DEFAULT '',
  global_citations INTEGER,
  cite_count       INTEGER NOT NULL DEFAULT 0,
  relevance        INTEGER,
  summary          TEXT NOT NULL DEFAULT '',
  why              TEXT NOT NULL DEFAULT '',
  state            TEXT NOT NULL DEFAULT 'new',  -- new|resolved|triaged|added|dismissed
  updated_at       TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Papers the Librarian wants a PDF for (listed in the UI's Wanted PDFs view).
CREATE TABLE IF NOT EXISTS requests (
  paper_id       INTEGER PRIMARY KEY,
  state          TEXT NOT NULL DEFAULT 'pending', -- pending|asked|received|declined|snoozed
  priority       INTEGER NOT NULL DEFAULT 5,
  why            TEXT NOT NULL DEFAULT '',
  asked_at       TEXT,
  snooze_until   TEXT,
  updated_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

-- PDFs seen in papers/ (by content hash), and what became of them.
CREATE TABLE IF NOT EXISTS pdf_files (
  sha       TEXT PRIMARY KEY,
  name      TEXT NOT NULL,
  paper_id  INTEGER,
  state     TEXT NOT NULL DEFAULT 'new',  -- new|linked|filed|failed
  detail    TEXT NOT NULL DEFAULT '',
  seen_at   TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Text units for semantic search; vectors live in vec_chunks (sqlite-vec).
CREATE TABLE IF NOT EXISTS chunks (
  id       INTEGER PRIMARY KEY,
  paper_id INTEGER,          -- NULL for ghost chunks
  ghost_id INTEGER,
  kind     TEXT NOT NULL,    -- summary|note|user_note|abstract|body|ghost
  page     TEXT NOT NULL DEFAULT '',
  text     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_chunks_paper ON chunks(paper_id);
CREATE INDEX IF NOT EXISTS idx_chunks_ghost ON chunks(ghost_id);

-- Things worth telling the user (filed a new paper, found a contradiction).
-- The web UI's Activity view and status chip read these.
CREATE TABLE IF NOT EXISTS events (
  id         INTEGER PRIMARY KEY,
  kind       TEXT NOT NULL,      -- filed|linked|contradiction|error|pdf_unmatched
  paper_id   INTEGER,
  text       TEXT NOT NULL,
  notified   INTEGER NOT NULL DEFAULT 0,
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

-- Library-wide conversations (the web "Ask the Librarian" view).
CREATE TABLE IF NOT EXISTS conversations (
  channel    TEXT PRIMARY KEY,   -- web
  session_id TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS messages (
  id         INTEGER PRIMARY KEY,
  channel    TEXT NOT NULL,
  role       TEXT NOT NULL,      -- user|assistant
  content    TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_messages_channel ON messages(channel, id);
"""

_local = threading.local()


def connect(vec=False):
    """A connection to librarian.db. vec=True also loads sqlite-vec."""
    conn = sqlite3.connect(config.DB_PATH, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    if vec:
        import sqlite_vec
        conn.enable_load_extension(True)
        sqlite_vec.load(conn)
        conn.enable_load_extension(False)
    return conn


def conn(vec=False):
    """Per-thread cached connection (same pattern as app.py)."""
    key = "vec" if vec else "plain"
    c = getattr(_local, key, None)
    if c is None:
        c = connect(vec)
        setattr(_local, key, c)
    return c


# ------------------------------------------------------------------ meta

def get_meta(c, key, default=""):
    row = c.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_meta(c, key, value):
    c.execute("INSERT INTO meta (key, value) VALUES (?,?) ON CONFLICT(key)"
              " DO UPDATE SET value = excluded.value", (key, str(value)))
    c.commit()


# ------------------------------------------------------------ paper_state

JSON_FIELDS = ("claims", "areas", "gaps", "suggested_tags")


def get_state(c, paper_id):
    row = c.execute("SELECT * FROM paper_state WHERE paper_id = ?", (paper_id,)).fetchone()
    if not row:
        return None
    d = dict(row)
    for k in JSON_FIELDS:
        d[k] = json.loads(d[k] or "[]")
    return d


def update_state(c, paper_id, **fields):
    c.execute("INSERT OR IGNORE INTO paper_state (paper_id) VALUES (?)", (paper_id,))
    if fields:
        for k in JSON_FIELDS:
            if k in fields and not isinstance(fields[k], str):
                fields[k] = json.dumps(fields[k])
        sets = ", ".join(f"{k} = ?" for k in fields)
        c.execute(f"UPDATE paper_state SET {sets}, updated_at = datetime('now')"
                  " WHERE paper_id = ?", list(fields.values()) + [paper_id])
    c.commit()


def all_states(c):
    return {r["paper_id"]: r for r in c.execute("SELECT * FROM paper_state")}


# ------------------------------------------------------------------ jobs

def enqueue(c, kind, paper_id=None, arg="", priority=5, delay_s=0):
    """Add a job unless an identical one is already queued or running."""
    try:
        c.execute("INSERT INTO jobs (kind, paper_id, arg, priority, run_after)"
                  " VALUES (?,?,?,?, datetime('now', ?))",
                  (kind, paper_id, arg, priority, f"+{int(delay_s)} seconds"))
        c.commit()
        return True
    except sqlite3.IntegrityError:
        # Already live: keep the more urgent priority.
        c.execute("UPDATE jobs SET priority = MIN(priority, ?) WHERE kind = ?"
                  " AND IFNULL(paper_id,-1) = IFNULL(?,-1) AND arg = ? AND state = 'queued'",
                  (priority, kind, paper_id, arg))
        c.commit()
        return False


def next_job(c):
    row = c.execute(
        "SELECT * FROM jobs WHERE state = 'queued' AND run_after <= datetime('now')"
        " ORDER BY priority, id LIMIT 1").fetchone()
    if not row:
        return None
    c.execute("UPDATE jobs SET state = 'running', started_at = datetime('now'),"
              " attempts = attempts + 1 WHERE id = ?", (row["id"],))
    c.commit()
    return dict(row)


def finish_job(c, job_id, error=""):
    c.execute("UPDATE jobs SET state = ?, error = ?, finished_at = datetime('now')"
              " WHERE id = ?", ("failed" if error else "done", error[:2000], job_id))
    c.commit()


def retry_job(c, job_id, delay_s, error):
    c.execute("UPDATE jobs SET state = 'queued', error = ?,"
              " run_after = datetime('now', ?) WHERE id = ?",
              (error[:2000], f"+{int(delay_s)} seconds", job_id))
    c.commit()


def reset_running(c):
    """Jobs left 'running' by a crash or Ctrl-C go back to the queue."""
    c.execute("UPDATE jobs SET state = 'queued' WHERE state = 'running'")
    c.commit()


def queue_stats(c):
    rows = c.execute("SELECT kind, COUNT(*) AS n FROM jobs WHERE state = 'queued'"
                     " GROUP BY kind").fetchall()
    running = c.execute("SELECT kind, paper_id FROM jobs WHERE state = 'running'"
                        " LIMIT 1").fetchone()
    failed = c.execute("SELECT COUNT(*) FROM jobs WHERE state = 'failed'").fetchone()[0]
    return {"queued": {r["kind"]: r["n"] for r in rows},
            "running": dict(running) if running else None,
            "failed": failed}


# ------------------------------------------------------------ pdf files

def pdf_seen(c, sha):
    row = c.execute("SELECT * FROM pdf_files WHERE sha = ?", (sha,)).fetchone()
    return dict(row) if row else None


def record_pdf(c, sha, name, paper_id=None, state="new", detail=""):
    c.execute("INSERT INTO pdf_files (sha, name, paper_id, state, detail) VALUES (?,?,?,?,?)"
              " ON CONFLICT(sha) DO UPDATE SET name = excluded.name,"
              " paper_id = excluded.paper_id, state = excluded.state, detail = excluded.detail",
              (sha, name, paper_id, state, detail))
    c.commit()


# -------------------------------------------------------------- requests

def upsert_request(c, paper_id, why, priority):
    c.execute("INSERT INTO requests (paper_id, why, priority) VALUES (?,?,?)"
              " ON CONFLICT(paper_id) DO UPDATE SET why = excluded.why,"
              " priority = MIN(priority, excluded.priority), updated_at = datetime('now')"
              " WHERE state IN ('pending', 'snoozed')",
              (paper_id, why, priority))
    c.commit()


def set_request(c, paper_id, **fields):
    if not fields:
        return
    sets = ", ".join(f"{k} = ?" for k in fields)
    c.execute(f"UPDATE requests SET {sets}, updated_at = datetime('now') WHERE paper_id = ?",
              list(fields.values()) + [paper_id])
    c.commit()


def due_requests(c, limit):
    return [dict(r) for r in c.execute(
        "SELECT * FROM requests WHERE state = 'pending'"
        " OR (state = 'snoozed' AND snooze_until <= datetime('now'))"
        " ORDER BY priority, updated_at LIMIT ?", (limit,))]


# ---------------------------------------------------------------- events

def add_event(c, kind, text, paper_id=None):
    c.execute("INSERT INTO events (kind, paper_id, text) VALUES (?,?,?)",
              (kind, paper_id, text))
    c.commit()


def pending_events(c, limit=20):
    return [dict(r) for r in c.execute(
        "SELECT * FROM events WHERE notified = 0 ORDER BY id LIMIT ?", (limit,))]


def mark_notified(c, ids):
    c.executemany("UPDATE events SET notified = 1 WHERE id = ?", [(i,) for i in ids])
    c.commit()


def recent_events(c, limit=20):
    return [dict(r) for r in c.execute(
        "SELECT * FROM events ORDER BY id DESC LIMIT ?", (limit,))]


# -------------------------------------------------------------- messages

def add_message(c, channel, role, content):
    cur = c.execute("INSERT INTO messages (channel, role, content) VALUES (?,?,?)",
                    (channel, role, content))
    c.commit()
    return cur.lastrowid


def history(c, channel, limit=200):
    rows = c.execute("SELECT * FROM (SELECT * FROM messages WHERE channel = ?"
                     " ORDER BY id DESC LIMIT ?) ORDER BY id", (channel, limit))
    return [dict(r) for r in rows]


def get_session(c, channel):
    row = c.execute("SELECT session_id FROM conversations WHERE channel = ?",
                    (channel,)).fetchone()
    return row["session_id"] if row else ""


def set_session(c, channel, session_id):
    c.execute("INSERT INTO conversations (channel, session_id) VALUES (?,?)"
              " ON CONFLICT(channel) DO UPDATE SET session_id = excluded.session_id",
              (channel, session_id))
    c.commit()


def clear_channel(c, channel):
    c.execute("DELETE FROM messages WHERE channel = ?", (channel,))
    c.execute("DELETE FROM conversations WHERE channel = ?", (channel,))
    c.commit()
