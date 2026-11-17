-- Paper Librarian — schema
-- One sqlite file holds papers, your tags, and your notes.

PRAGMA journal_mode = WAL;
PRAGMA foreign_keys = ON;

CREATE TABLE IF NOT EXISTS papers (
  id          INTEGER PRIMARY KEY,
  citation    TEXT NOT NULL,            -- the raw reference string, kept verbatim
  title       TEXT NOT NULL DEFAULT '',
  authors     TEXT NOT NULL DEFAULT '',
  year        TEXT NOT NULL DEFAULT '',
  venue       TEXT NOT NULL DEFAULT '',
  popularity  INTEGER NOT NULL DEFAULT 0, -- how many of your source papers cite it
  in_draft    INTEGER NOT NULL DEFAULT 0,
  summarized  INTEGER NOT NULL DEFAULT 0,
  read_status TEXT NOT NULL DEFAULT 'unread', -- unread | reading | read
  pdf_path    TEXT NOT NULL DEFAULT '', -- relative to the papers/ directory
  notes       TEXT NOT NULL DEFAULT '',
  created_at  TEXT NOT NULL DEFAULT (datetime('now')),
  updated_at  TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_papers_popularity ON papers(popularity DESC);
CREATE INDEX IF NOT EXISTS idx_papers_year       ON papers(year);

-- Your tags. Distinct from keywords: tags are yours, keywords are derived.
CREATE TABLE IF NOT EXISTS tags (
  id    INTEGER PRIMARY KEY,
  name  TEXT NOT NULL UNIQUE COLLATE NOCASE,
  color TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS paper_tags (
  paper_id INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  tag_id   INTEGER NOT NULL REFERENCES tags(id)   ON DELETE CASCADE,
  PRIMARY KEY (paper_id, tag_id)
);
CREATE INDEX IF NOT EXISTS idx_paper_tags_tag ON paper_tags(tag_id);

-- Keywords derived from the citation text (topic vocabulary + acronyms).
CREATE TABLE IF NOT EXISTS keywords (
  id   INTEGER PRIMARY KEY,
  name TEXT NOT NULL UNIQUE COLLATE NOCASE
);

CREATE TABLE IF NOT EXISTS paper_keywords (
  paper_id   INTEGER NOT NULL REFERENCES papers(id)   ON DELETE CASCADE,
  keyword_id INTEGER NOT NULL REFERENCES keywords(id) ON DELETE CASCADE,
  PRIMARY KEY (paper_id, keyword_id)
);
CREATE INDEX IF NOT EXISTS idx_paper_keywords_kw ON paper_keywords(keyword_id);

-- The 12 papers you actually own, whose bibliographies were mined.
-- Retained as the provenance behind papers.popularity, but no longer surfaced:
-- the app stopped exposing "cited by" as a facet, filter, or API field.
CREATE TABLE IF NOT EXISTS sources (
  id       INTEGER PRIMARY KEY,
  key      TEXT NOT NULL UNIQUE,     -- e.g. "Bouvier2019"
  label    TEXT NOT NULL DEFAULT '',
  pdf_path TEXT NOT NULL DEFAULT ''
);

CREATE TABLE IF NOT EXISTS paper_sources (
  paper_id  INTEGER NOT NULL REFERENCES papers(id)  ON DELETE CASCADE,
  source_id INTEGER NOT NULL REFERENCES sources(id) ON DELETE CASCADE,
  PRIMARY KEY (paper_id, source_id)
);
CREATE INDEX IF NOT EXISTS idx_paper_sources_src ON paper_sources(source_id);

-- Full-text index. Maintained explicitly from Python (see db.reindex_paper)
-- rather than by trigger, because tags/keywords live in other tables.
CREATE VIRTUAL TABLE IF NOT EXISTS papers_fts USING fts5(
  title, authors, citation, venue, notes, tags, keywords,
  tokenize = "unicode61 remove_diacritics 2"
);

-- Small key/value store for app settings (e.g. where the LaTeX draft lives).
CREATE TABLE IF NOT EXISTS settings (
  key   TEXT PRIMARY KEY,
  value TEXT NOT NULL DEFAULT ''
);

-- Maps a \cite{} key in the LaTeX draft to a paper in this library.
-- Rows with manual = 1 were chosen by hand and are never overwritten by the
-- automatic title matcher; ignored = 1 means "deliberately not in the library".
CREATE TABLE IF NOT EXISTS bib_links (
  bibkey   TEXT PRIMARY KEY,
  paper_id INTEGER REFERENCES papers(id) ON DELETE CASCADE,
  manual   INTEGER NOT NULL DEFAULT 0,
  ignored  INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_bib_links_paper ON bib_links(paper_id);

-- Per-paper conversation with Claude, shown in the split view's chat pane.
-- Deliberately not in papers_fts: chat is scratch thinking, notes are the
-- durable artifact, and only notes should surface in library search.
CREATE TABLE IF NOT EXISTS chat_messages (
  id         INTEGER PRIMARY KEY,
  paper_id   INTEGER NOT NULL REFERENCES papers(id) ON DELETE CASCADE,
  role       TEXT NOT NULL,              -- user | assistant
  content    TEXT NOT NULL DEFAULT '',
  model      TEXT NOT NULL DEFAULT '',
  created_at TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_chat_paper ON chat_messages(paper_id, id);

-- One resumable `claude -p` session per paper, so follow-up questions keep
-- the PDF and the earlier turns in context instead of re-reading from zero.
CREATE TABLE IF NOT EXISTS chat_threads (
  paper_id   INTEGER PRIMARY KEY REFERENCES papers(id) ON DELETE CASCADE,
  session_id TEXT NOT NULL DEFAULT '',
  model      TEXT NOT NULL DEFAULT ''
);
