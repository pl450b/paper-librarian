"""Database layer for the reading library.

Only the stdlib. The whole library is one sqlite file (library.db) sitting
next to the papers/ directory, so it moves/syncs/backs up as a unit.
"""

import re
import sqlite3
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DB_PATH = ROOT / "library.db"
SCHEMA_PATH = ROOT / "schema.sql"
PAPERS_DIR = ROOT / "papers"

SORTS = {
    "popularity": "p.popularity DESC, p.year DESC",
    "year_desc": "p.year DESC, p.popularity DESC",
    "year_asc": "p.year ASC, p.popularity DESC",
    "title": "p.title COLLATE NOCASE ASC",
    "updated": "p.updated_at DESC",
    "relevance": "rank ASC",
}


# Tables added after the original schema. They are created on connect so an
# older library.db picks them up. Every statement here must stay idempotent.
MIGRATIONS = """
CREATE TABLE IF NOT EXISTS chat_messages (
  id         INTEGER PRIMARY KEY,
  paper_id   INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  role       TEXT NOT NULL,
  content    TEXT NOT NULL DEFAULT '',
  model      TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_paper ON chat_messages(paper_id, id);
CREATE TABLE IF NOT EXISTS chat_threads (
  paper_id   INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL DEFAULT '',
  model      TEXT NOT NULL DEFAULT ''
);
"""


def connect():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    conn.executescript(MIGRATIONS)
    return conn


def init(conn):
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    conn.commit()


# --------------------------------------------------------------------------
# full-text index
# --------------------------------------------------------------------------

def reindex_paper(conn, paper_id):
    """Rebuild the FTS row for one paper. Call after any write."""
    row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    conn.execute("DELETE FROM papers_fts WHERE rowid = ?", (paper_id,))
    if row is None:
        return
    tags = " ".join(_names(conn, "tags", "paper_tags", "tag_id", paper_id))
    keywords = " ".join(_names(conn, "keywords", "paper_keywords", "keyword_id", paper_id))
    conn.execute(
        "INSERT INTO papers_fts (rowid, title, authors, citation, venue, notes, tags, keywords)"
        " VALUES (?,?,?,?,?,?,?,?)",
        (paper_id, row["title"], row["authors"], row["citation"], row["venue"],
         row["notes"], tags, keywords),
    )


def _names(conn, table, join_table, fk, paper_id):
    sql = (f"SELECT t.name FROM {table} t JOIN {join_table} j ON j.{fk} = t.id"
           f" WHERE j.paper_id = ? ORDER BY t.name")
    return [r["name"] for r in conn.execute(sql, (paper_id,))]


def rebuild_index(conn):
    conn.execute("DELETE FROM papers_fts")
    for row in conn.execute("SELECT id FROM papers").fetchall():
        reindex_paper(conn, row["id"])
    conn.commit()


def fts_query(raw):
    """Turn a human search box string into a safe FTS5 MATCH expression.

    Bare words become prefix matches so typing "transform" finds
    "transformer"; "quoted phrases" stay exact. Everything is quoted, so
    stray punctuation can never blow up the parser.
    """
    terms = []
    for phrase, word in re.findall(r'"([^"]+)"|(\S+)', raw):
        if phrase:
            terms.append('"%s"' % phrase.replace('"', ""))
        else:
            cleaned = re.sub(r'["*(){}\[\]:^-]', " ", word).strip()
            if cleaned:
                terms.append('"%s"*' % cleaned)
    return " AND ".join(terms)


# --------------------------------------------------------------------------
# queries
# --------------------------------------------------------------------------

def fts_rank_any(conn, q, limit=50):
    """Paper ids ranked by BM25 for ANY of the query's words (OR, not AND).

    For the Librarian's hybrid search, whose queries are natural-language
    questions: AND across every word of a sentence matches almost nothing.
    Terms go through the same quoting as fts_query, so input is still safe.
    """
    stop = {"the", "and", "for", "with", "that", "this", "from", "what", "which",
            "how", "does", "are", "was", "were", "have", "has", "any", "about",
            "into", "than", "then", "there", "their", "they", "them", "papers",
            "paper", "show", "find", "using", "use", "can", "between"}
    words = [w for w in re.findall(r"[A-Za-z0-9][A-Za-z0-9\-]+", q or "")
             if len(w) > 2 and w.lower() not in stop]
    if not words:
        return []
    expr = " OR ".join('"%s"*' % re.sub(r'["*(){}\[\]:^-]', " ", w).strip()
                       for w in words[:24])
    rows = conn.execute(
        "SELECT rowid AS id FROM papers_fts WHERE papers_fts MATCH ?"
        " ORDER BY bm25(papers_fts) LIMIT ?", (expr, int(limit))).fetchall()
    return [r["id"] for r in rows]


def search_papers(conn, q="", tags=(), keywords=(),
                  min_pop=0, has_pdf=None, has_notes=None, read_status="",
                  flag="", sort="popularity", limit=100, offset=0, only_ids=None):
    """Filtered + ranked paper list. Returns (rows, total_count)."""
    joins, where, args = [], [], []

    q = (q or "").strip()
    if q:
        expr = fts_query(q)
        if expr:
            joins.append("JOIN papers_fts f ON f.rowid = p.id")
            where.append("papers_fts MATCH ?")
            args.append(expr)

    # Each selected tag is an AND: papers must carry all of them.
    for i, name in enumerate(tags):
        alias = f"pt{i}"
        joins.append(
            f"JOIN paper_tags {alias} ON {alias}.paper_id = p.id"
            f" JOIN tags tg{i} ON tg{i}.id = {alias}.tag_id AND tg{i}.name = ? COLLATE NOCASE")
        args.append(name)

    for i, name in enumerate(keywords):
        alias = f"pk{i}"
        joins.append(
            f"JOIN paper_keywords {alias} ON {alias}.paper_id = p.id"
            f" JOIN keywords kw{i} ON kw{i}.id = {alias}.keyword_id AND kw{i}.name = ? COLLATE NOCASE")
        args.append(name)

    if only_ids is not None:
        # e.g. "read in full by the Librarian" -- ids come from the daemon.
        ids = [int(i) for i in only_ids][:5000] or [-1]
        where.append(f"p.id IN ({','.join('?' * len(ids))})")
        args.extend(ids)
    if min_pop:
        where.append("p.popularity >= ?")
        args.append(int(min_pop))
    if has_pdf is True:
        where.append("p.pdf_path <> ''")
    elif has_pdf is False:
        where.append("p.pdf_path = ''")
    if has_notes is True:
        where.append("TRIM(p.notes) <> ''")
    elif has_notes is False:
        where.append("TRIM(p.notes) = ''")
    if read_status:
        where.append("p.read_status = ?")
        args.append(read_status)
    if flag == "draft":
        where.append("p.in_draft = 1")
    elif flag == "summarized":
        where.append("p.summarized = 1")

    join_sql = " ".join(joins)
    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    total = conn.execute(
        f"SELECT COUNT(DISTINCT p.id) AS c FROM papers p {join_sql}{where_sql}", args
    ).fetchone()["c"]

    if sort == "relevance" and not q:
        sort = "popularity"
    order = SORTS.get(sort, SORTS["popularity"])

    rows = conn.execute(
        f"SELECT p.* FROM papers p {join_sql}{where_sql}"
        f" GROUP BY p.id ORDER BY {order} LIMIT ? OFFSET ?",
        args + [int(limit), int(offset)],
    ).fetchall()

    return [paper_dict(conn, r) for r in rows], total


def paper_dict(conn, row):
    d = dict(row)
    d["tags"] = _names(conn, "tags", "paper_tags", "tag_id", row["id"])
    d["keywords"] = _names(conn, "keywords", "paper_keywords", "keyword_id", row["id"])
    d["in_draft"] = bool(d["in_draft"])
    d["summarized"] = bool(d["summarized"])
    d["has_notes"] = bool(d["notes"].strip())
    return d


def get_paper(conn, paper_id):
    row = conn.execute("SELECT * FROM papers WHERE id = ?", (paper_id,)).fetchone()
    return paper_dict(conn, row) if row else None


def facets(conn):
    """Sidebar counts: tags and keywords."""
    tag_rows = conn.execute(
        "SELECT t.id, t.name, t.color, COUNT(pt.paper_id) AS count FROM tags t"
        " LEFT JOIN paper_tags pt ON pt.tag_id = t.id"
        " GROUP BY t.id ORDER BY count DESC, t.name COLLATE NOCASE").fetchall()
    keyword_rows = conn.execute(
        "SELECT k.name, COUNT(pk.paper_id) AS count FROM keywords k"
        " JOIN paper_keywords pk ON pk.keyword_id = k.id"
        " GROUP BY k.id HAVING count > 1 ORDER BY count DESC, k.name LIMIT 60").fetchall()

    stats = conn.execute(
        "SELECT COUNT(*) AS total,"
        " SUM(popularity >= 2) AS multi,"
        " SUM(in_draft) AS draft,"
        " SUM(TRIM(notes) <> '') AS noted,"
        " SUM(pdf_path <> '') AS pdfs FROM papers").fetchone()

    return {
        "tags": [dict(r) for r in tag_rows],
        "keywords": [dict(r) for r in keyword_rows],
        "stats": {k: (stats[k] or 0) for k in stats.keys()},
    }


# --------------------------------------------------------------------------
# writes
# --------------------------------------------------------------------------

EDITABLE = ("title", "authors", "year", "venue", "citation",
            "notes", "pdf_path", "read_status", "popularity",
            "in_draft", "summarized")


def update_paper(conn, paper_id, fields):
    sets, args = [], []
    for key in EDITABLE:
        if key in fields:
            value = fields[key]
            if key in ("in_draft", "summarized"):
                value = 1 if value else 0
            elif key == "popularity":
                value = int(value or 0)
            sets.append(f"{key} = ?")
            args.append(value)
    if not sets:
        return get_paper(conn, paper_id)
    sets.append("updated_at = datetime('now')")
    conn.execute(f"UPDATE papers SET {', '.join(sets)} WHERE id = ?", args + [paper_id])
    reindex_paper(conn, paper_id)
    conn.commit()
    return get_paper(conn, paper_id)


def create_paper(conn, fields):
    cur = conn.execute(
        "INSERT INTO papers (citation, title, authors, year, venue) VALUES (?,?,?,?,?)",
        (fields.get("citation") or fields.get("title", "Untitled"),
         fields.get("title", ""), fields.get("authors", ""), fields.get("year", ""),
         fields.get("venue", "")))
    paper_id = cur.lastrowid
    reindex_paper(conn, paper_id)
    conn.commit()
    return get_paper(conn, paper_id)


def delete_paper(conn, paper_id):
    conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
    conn.execute("DELETE FROM papers_fts WHERE rowid = ?", (paper_id,))
    conn.commit()


def add_tag(conn, paper_id, name):
    name = name.strip()
    if not name:
        return
    conn.execute("INSERT OR IGNORE INTO tags (name) VALUES (?)", (name,))
    tag = conn.execute("SELECT id FROM tags WHERE name = ? COLLATE NOCASE", (name,)).fetchone()
    conn.execute("INSERT OR IGNORE INTO paper_tags (paper_id, tag_id) VALUES (?,?)",
                 (paper_id, tag["id"]))
    conn.execute("UPDATE papers SET updated_at = datetime('now') WHERE id = ?", (paper_id,))
    reindex_paper(conn, paper_id)
    conn.commit()


def remove_tag(conn, paper_id, name):
    conn.execute(
        "DELETE FROM paper_tags WHERE paper_id = ? AND tag_id ="
        " (SELECT id FROM tags WHERE name = ? COLLATE NOCASE)", (paper_id, name.strip()))
    reindex_paper(conn, paper_id)
    conn.commit()


def rename_tag(conn, tag_id, name=None, color=None):
    if name:
        conn.execute("UPDATE tags SET name = ? WHERE id = ?", (name.strip(), tag_id))
    if color is not None:
        conn.execute("UPDATE tags SET color = ? WHERE id = ?", (color, tag_id))
    for r in conn.execute("SELECT paper_id FROM paper_tags WHERE tag_id = ?", (tag_id,)).fetchall():
        reindex_paper(conn, r["paper_id"])
    conn.commit()


def delete_tag(conn, tag_id):
    papers = [r["paper_id"] for r in
              conn.execute("SELECT paper_id FROM paper_tags WHERE tag_id = ?", (tag_id,))]
    conn.execute("DELETE FROM tags WHERE id = ?", (tag_id,))
    for pid in papers:
        reindex_paper(conn, pid)
    conn.commit()


# --------------------------------------------------------------------------
# settings
# --------------------------------------------------------------------------

def get_setting(conn, key, default=""):
    row = conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
    return row["value"] if row else default


def set_setting(conn, key, value):
    conn.execute("INSERT INTO settings (key, value) VALUES (?,?)"
                 " ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                 (key, str(value)))
    conn.commit()


# --------------------------------------------------------------------------
# thesis <-> library links
# --------------------------------------------------------------------------

def norm_title(text):
    """Fold a title down to comparable words: lowercase, letters and digits."""
    text = re.sub(r"[{}\\$]", "", (text or "").lower())
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text).split())


def _surname(author):
    """First author's surname, from either 'Last, First' or 'First Last'."""
    first = re.split(r"\band\b|,(?![^,]*\.)|;", (author or "").strip())[0]
    if "," in (author or "") and re.match(r"^[^,]+,\s*[A-Z]", author.strip()):
        first = author.split(",")[0]
    words = re.sub(r"[^A-Za-z \-]", " ", first).split()
    return words[-1].lower() if words else ""


def get_bib_links(conn):
    return {r["bibkey"]: dict(r) for r in conn.execute("SELECT * FROM bib_links")}


def set_bib_link(conn, bibkey, paper_id, manual=1, ignored=0):
    conn.execute(
        "INSERT INTO bib_links (bibkey, paper_id, manual, ignored) VALUES (?,?,?,?)"
        " ON CONFLICT(bibkey) DO UPDATE SET paper_id = excluded.paper_id,"
        " manual = excluded.manual, ignored = excluded.ignored",
        (bibkey, paper_id, 1 if manual else 0, 1 if ignored else 0))
    conn.commit()


def resolve_bib_links(conn, entries):
    """Match bib entries to papers by title, then by author+year.

    Rows the user set by hand (manual = 1) and keys marked ignored are left
    exactly as they are; everything else is re-derived on each call so the
    links follow the .bib file as it changes.
    """
    existing = get_bib_links(conn)
    papers = conn.execute(
        "SELECT id, title, authors, year FROM papers").fetchall()

    by_title, by_author_year = {}, {}
    for row in papers:
        key = norm_title(row["title"])
        if key:
            by_title.setdefault(key, []).append(row)
        ay = (_surname(row["authors"]), str(row["year"]).strip())
        if all(ay):
            by_author_year.setdefault(ay, []).append(row)

    titles = [(norm_title(r["title"]), r) for r in papers if r["title"]]
    resolved = {}

    for key, entry in entries.items():
        prior = existing.get(key)
        if prior and (prior["manual"] or prior["ignored"]):
            resolved[key] = {"paper_id": prior["paper_id"],
                             "manual": bool(prior["manual"]),
                             "ignored": bool(prior["ignored"]),
                             "how": "manual"}
            continue

        target = norm_title(entry.get("title"))
        match, how = None, ""
        if target:
            hits = by_title.get(target)
            if hits:
                match, how = hits[0], "title"
            else:
                near = [r for t, r in titles
                        if t and (target in t or t in target)
                        and abs(len(t) - len(target)) < 25]
                if len(near) >= 1:
                    match, how = near[0], "title~"
        if match is None:
            ay = (_surname(entry.get("author")), str(entry.get("year") or "").strip())
            hits = by_author_year.get(ay) if all(ay) else None
            if hits and len(hits) == 1:
                match, how = hits[0], "author+year"

        paper_id = match["id"] if match is not None else None
        resolved[key] = {"paper_id": paper_id, "manual": False,
                         "ignored": False, "how": how}
        if prior is None or prior["paper_id"] != paper_id:
            conn.execute(
                "INSERT INTO bib_links (bibkey, paper_id, manual, ignored)"
                " VALUES (?,?,0,0) ON CONFLICT(bibkey) DO UPDATE SET"
                " paper_id = excluded.paper_id WHERE bib_links.manual = 0"
                " AND bib_links.ignored = 0",
                (key, paper_id))

    # Drop auto rows whose key has disappeared from the .bib file.
    stale = [k for k, v in existing.items()
             if k not in entries and not v["manual"] and not v["ignored"]]
    for key in stale:
        conn.execute("DELETE FROM bib_links WHERE bibkey = ? AND manual = 0", (key,))

    conn.commit()
    return resolved


def sync_in_draft(conn, paper_ids):
    """Make papers.in_draft mirror what the thesis actually cites.

    updated_at is deliberately not touched: this is bookkeeping, not an edit,
    and it must not reshuffle the "Recently edited" sort. in_draft is not an
    FTS column either, so no reindex is needed.
    """
    ids = {int(p) for p in paper_ids if p}
    current = {r["id"] for r in conn.execute("SELECT id FROM papers WHERE in_draft = 1")}
    added, removed = ids - current, current - ids
    for pid in added:
        conn.execute("UPDATE papers SET in_draft = 1 WHERE id = ?", (pid,))
    for pid in removed:
        conn.execute("UPDATE papers SET in_draft = 0 WHERE id = ?", (pid,))
    if added or removed:
        conn.commit()
    return {"added": len(added), "removed": len(removed)}


def bibkeys_for_paper(conn, paper_id):
    return [r["bibkey"] for r in conn.execute(
        "SELECT bibkey FROM bib_links WHERE paper_id = ? ORDER BY bibkey", (paper_id,))]


# --------------------------------------------------------------------------
# per-paper chat with Claude
# --------------------------------------------------------------------------
# Chat is not an FTS column, so none of these call reindex_paper(). They also
# leave papers.updated_at alone: asking a question is not editing the paper,
# and it must not reshuffle the "Recently edited" sort.

def chat_history(conn, paper_id):
    return [dict(r) for r in conn.execute(
        "SELECT id, role, content, model, created_at FROM chat_messages"
        " WHERE paper_id = ? ORDER BY id", (paper_id,))]


def add_chat_message(conn, paper_id, role, content, model=""):
    cur = conn.execute(
        "INSERT INTO chat_messages (paper_id, role, content, model)"
        " VALUES (?,?,?,?)", (paper_id, role, content, model))
    conn.commit()
    return cur.lastrowid


def chat_thread(conn, paper_id):
    row = conn.execute("SELECT session_id, model FROM chat_threads"
                       " WHERE paper_id = ?", (paper_id,)).fetchone()
    return dict(row) if row else {"session_id": "", "model": ""}


def set_chat_thread(conn, paper_id, session_id, model=""):
    conn.execute(
        "INSERT INTO chat_threads (paper_id, session_id, model) VALUES (?,?,?)"
        " ON CONFLICT(paper_id) DO UPDATE SET"
        "   session_id = excluded.session_id, model = excluded.model",
        (paper_id, session_id, model))
    conn.commit()


def clear_chat(conn, paper_id):
    """Forget the whole conversation, including the resumable CLI session."""
    conn.execute("DELETE FROM chat_messages WHERE paper_id = ?", (paper_id,))
    conn.execute("DELETE FROM chat_threads WHERE paper_id = ?", (paper_id,))
    conn.commit()
