# CLAUDE.md

Guidance for Claude Code (and humans) working on this repository.

## What this is

A local, single-user web app for managing a research reading library: papers
in one sqlite file, user tags, per-paper markdown notes, FTS5 search, an inline
PDF viewer, a LaTeX "Draft" pane, and a per-paper chat. Alongside it runs
**the Librarian** (`librarian/`), an optional background agent that catalogs
papers against the user's research brief (`librarian/brief.md`), tracks
citations, and answers library-wide questions.

Internally the Draft pane is still called `thesis` (`thesis.py`,
`static/thesis.js`, `/api/thesis/*`, `#thesis/...` URLs). The user-facing
label is "Draft". Don't rename the internals piecemeal.

**Hard constraint: the web app (`app.py`, `db.py`, `chat.py`, `thesis.py`,
`librarian_link.py`, `import_bibtex.py`, `static/`) is Python standard library
only, with no frontend build step and no dependencies.** Do not introduce
Flask/FastAPI, npm packages, bundlers, or transpiled JS.

**The one exception is `librarian/`**, which has its own uv-managed venv
(sqlite-vec, fastembed) and runs as a **separate process**. The app reaches it
only over loopback HTTP via `librarian_link.py`, and must keep working,
unchanged, when `librarian/.venv` does not exist. Never import anything from
`librarian/` into the app.

## Commands

```bash
python3 app.py --open                 # serve on http://127.0.0.1:8765 (creates library.db)
python3 app.py --port 9000 --quiet --draft path/to/main.tex
python3 import_bibtex.py refs.bib     # bulk import; skips papers already present
cd librarian && uv sync               # install the Librarian (optional)
librarian/.venv/bin/python -m librarian status|find|ingest|gold|backfill
```

There are no automated tests. Verify changes by exercising the API directly:

```bash
curl -s 'http://127.0.0.1:8765/api/papers?q=attention&limit=3' | python3 -m json.tool
curl -s http://127.0.0.1:8765/api/bootstrap | python3 -m json.tool
curl -s http://127.0.0.1:8765/api/librarian/status | python3 -m json.tool
```

## Architecture

| file | role |
|---|---|
| [app.py](app.py) | `ThreadingHTTPServer` on `BaseHTTPRequestHandler`; JSON API, static file serving, PDF streaming with HTTP Range support |
| [db.py](db.py) | every SQL statement in the app: search, facets, writes, FTS maintenance |
| [schema.sql](schema.sql) | tables + the `papers_fts` FTS5 virtual table (idempotent; applied on every start) |
| [static/app.js](static/app.js) | the frontend: state object, render functions, event delegation |
| [chat.py](chat.py) | the only thing in the app that talks to Claude: drives the `claude` CLI as a subprocess |

`app.py` never writes SQL; `db.py` never touches HTTP; `chat.py` does neither.
Keep those boundaries.

### Threading and connections

`app.py` uses a `threading.local()` connection cache (`conn()`), so each worker
thread gets its own `sqlite3` connection. Do not share connections across threads
or hold module-level ones. `db.connect()` sets `foreign_keys = ON`; the schema
sets WAL mode.

### Full-text search

`papers_fts` is **not** trigger-maintained — tags and keywords live in separate
tables, so it is rebuilt from Python. Any write path that changes a paper's title,
authors, citation, venue, notes, tags, or keywords must call `db.reindex_paper(conn, pid)`
before committing. Tag renames/deletes must reindex every affected paper.
`db.rebuild_index(conn)` rebuilds the whole index.

`db.fts_query()` is the only thing that constructs a MATCH expression: it quotes
every term and appends `*` to bare words (prefix matching), so user input can never
break the FTS5 parser. Do not pass raw user text into MATCH.

### Filter semantics

In `db.search_papers()`: multiple **tags** and **keywords** are ANDed (implemented
as one aliased JOIN pair per value). Sorting is whitelisted through the `SORTS` dict — the
sort key is interpolated into SQL, so it must stay a lookup, never user text.
`sort=relevance` silently falls back to `popularity` when there is no query.

### Writable fields

`db.EDITABLE` is the allowlist for `PATCH /api/papers/<id>`. Adding a paper column
that users can edit means adding it there too.

Papers have no `topic` column: the reading map's topic groups became ordinary
tags. The `sources`/`paper_sources` tables still hold the mined bibliography
data behind `papers.popularity`, but nothing queries them — "cited by" is gone
from the sidebar, the filters, and the API. Don't reintroduce either as a
separate attribute.

`citation` holds the raw reference
string verbatim and is the source of truth; `title`/`authors`/`venue` are
heuristically parsed from it and may be imperfect.

### Path safety

Two independent containment checks exist and must be preserved: `safe_pdf_path()`
for anything under `papers/` (used by both `/pdf/<rel>` and `PATCH` of `pdf_path`),
and the `relative_to(STATIC_DIR)` check in `serve_static()`. Uploads go through
`PUT /api/upload?name=foo.pdf` with a **raw body** (not multipart, so the stdlib
parses it in one line) and never overwrite an existing file — they suffix `-1`, `-2`.

### Frontend

Single mutable `state` object in [static/app.js](static/app.js#L7); every filter
change funnels into `load()`, which builds a query string and re-renders. Rendering
is string templates + `innerHTML`, so **all interpolated values must go through
`esc()`** — except `markdown()` output, which is the app's own small renderer and
already escapes its input first. Events are delegated on container elements, not
bound per card. Detail state is mirrored into `location.hash` as `#paper/<id>/<tab>`,
where `<tab>` is `notes`, `pdf`, `split`, or `meta`.

`markdown()` lifts code spans and math (`$…$`, `$$…$$`, `\(…\)`, `\[…\]`) into
private-use-area placeholders **before** anything else runs, then substitutes the
rendered HTML back at the end. That ordering is load-bearing: it is what keeps
`esc()` from mangling a `<` inside `$a < b$` and stops `*` inside math from being
read as emphasis. `texToHTML()` escapes every literal character it emits, so math
is not an HTML injection hole — keep it that way if you extend it. Its symbol
table mirrors `SYMBOLS` in [thesis.py](thesis.py); extend both together so `\tau`
means the same thing in the Draft pane and in notes.

### The chat pane

`chat.py` reaches Claude by shelling out to the `claude` CLI in print mode, not
through the Anthropic API. That is deliberate: the CLI is already authenticated
against a Claude subscription, so there is no API key to manage and no second
bill, and `subprocess` keeps the stdlib-only rule intact. There is no API-key
code path — do not add one without being asked.

Two things in `chat.py` are load-bearing:

* The child runs with `cwd=papers/`, **never** the project root. From the root
  it auto-discovers this `CLAUDE.md` and concludes its job is to work on the
  webapp. Do not "fix" this with `--bare`: that flag reads `ANTHROPIC_API_KEY`
  only and never touches the OAuth credentials, defeating the subscription.
* The flag set (`--tools Read`, `--strict-mcp-config`,
  `--disable-slash-commands`, an explicit `--system-prompt`) is a token diet,
  not decoration. Without it the CLI loads every MCP server, skill and tool
  definition it can find: a one-word answer measured 31k tokens against ~3.8k
  with it. Adding tools or dropping `--strict-mcp-config` silently multiplies
  what every question costs.

`stream_turn()` is the whole Claude-facing surface, so swapping backends later
is a change to that one generator. Turns are persisted once, on completion —
a half-streamed answer is not written. One resumable CLI session per paper
lives in `chat_threads`, so follow-ups keep the PDF in context.

`POST /api/papers/<id>/chat` streams SSE and therefore cannot go through
`api_write()`, which ends in a single buffered `json_out`; it branches out of
`_mutate()` the same way `/api/upload` does. `protocol_version` is HTTP/1.1, so
the response sets `Connection: close` and omits `Content-Length`.

Chat is deliberately **not** in `papers_fts`: notes are the durable artifact and
only they should surface in library search. That is why the chat write paths do
not call `reindex_paper()`.

### Split view panes

The split is a *pair* picker, not two fixed columns: `pdf`, `notes` and `chat`
are all rendered, and exactly two are visible, because three readable columns do
not fit on one screen. The pair is in the URL as `#paper/<id>/split/<a>+<b>`; a
bare `.../split` still means `pdf+notes`, so old bookmarks keep working.

**All three panes stay in the DOM and visibility is toggled with CSS.** Swapping
a pane must never rebuild `#detail-body` — that would reload the PDF iframe,
lose its scroll position and drop the caret out of the notes. `applyPanes()`
toggles `hidden` and rewrites `grid-template-columns`; it does not re-render.
Each pane names its own grid column so hiding one does not shift the others.
Widths are remembered per pair, since 55% is right for a PDF and wrong for notes
beside a chat log.

## The Librarian (`librarian/`)

`app.py main()` starts it (`librarian_link.start()`) and stops it on any exit;
it serves `127.0.0.1:8766`. `/api/librarian/*` in `app.py` is an
**allowlisted** proxy to it. The daemon's `/tool/*` endpoints are for its MCP
server only and must not become reachable from the browser. `app.py` notifies
it (fire-and-forget) after `POST /api/papers` and after a `PATCH` that changes
`pdf_path`.

Module map: `daemon` (HTTP API, starts threads), `worker` (job queue, papers/
watcher, startup `reconcile`), `catalog` (resolve → text → triage → deep note),
`refs` (citations, ghosts), `embed` (sqlite-vec + FTS hybrid search), `agent`
(library-wide chat), `tools` (read-only query tools), `mcp` (stdio MCP server
over `tools`), `context` (parses `brief.md`), `llm` (`claude -p` wrapper),
`openalex` / `orx` (lookup; `orx` is optional with OpenAlex/arXiv fallbacks),
`store` (every SQL statement against `librarian.db`).

Load-bearing rules:

* **It never writes the user's notes and never applies tags.** It suggests
  tags; the user accepts them in the UI. Its own output lives in
  `librarian/librarian.db` (rebuildable) and `librarian/notes/`. Its only
  writes to `library.db` are linking a dropped PDF (`pdf_path`) and filing new
  paper rows (unmatched PDFs, frequently cited ghosts), both through the
  app's `db.py` (`config.app_db()`).
* **`papers_fts` stays the user's text only.** The Librarian's summaries are
  searchable only through its semantic search (the "≈ meaning" toggle).
  `db.fts_rank_any()` exists for its hybrid search; keep it read-only.
* **The brief drives the schemas.** Claim ids, gap ids, areas and tags come
  from `brief.md` via `context.py`, and become JSON-schema enums for
  `claude --json-schema`. An empty list must use `context.enum_array()`
  (an empty `enum` is invalid schema). Section names, not numbers, are what
  `context.section()` matches.
* **No tokens are spent on a placeholder brief.** While `brief.md` contains
  `<!-- TEMPLATE -->` the worker stays paused (`worker.Worker.paused`).
* **Every `claude -p` call passes `--setting-sources ""`** and runs with an
  empty `cwd` (`librarian/work/`; `papers/` for the app's chat). Otherwise the
  CLI walks up to parent CLAUDE.md files and user settings, costing ~15x the
  input tokens on a trivial call.
* **The backfill is opt-in.** Startup reconciliation only queues papers added
  after the Librarian was first started (plus new PDFs, and a re-triage of
  relevance-0/1 papers when `brief.md` changes). `librarian backfill` or the
  Activity view's button queues the rest.

## Data and safety notes

- `library.db` is the user's irreplaceable data (notes, tags). It uses WAL, so
  a plain file copy is not a valid backup; use `sqlite3.Connection.backup()`
  (see README).
- Nothing personal is tracked: `library.db`, `papers/`, `librarian/brief.md`,
  `librarian/notes/`, `librarian/LIBRARY.md`, `librarian/*.db` and `.env` are
  gitignored. Keep it that way.
- The API has **no authentication**. The default bind is `127.0.0.1`; do not
  change that default or suggest `--host 0.0.0.0` without auth in front.
