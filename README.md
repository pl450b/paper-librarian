# Paper Librarian

A local reading library for research of any kind, with an AI librarian that
reads your papers against *your* project.

- **The library**: a single-user web app for a few hundred papers. It has
  your own tags, per-paper markdown notes with maths, full-text search, an
  inline PDF viewer, and an optional pane for your LaTeX draft. It uses the
  Python standard library only, so there is nothing to install.
- **Chat per paper**: ask Claude about the open paper. It reads the PDF and
  keeps the conversation alongside your notes.
- **The Librarian (optional)**: a background agent that catalogs every paper
  against a research brief you write. It:
  - triages each paper for relevance;
  - reads the relevant ones in full and writes a structured note;
  - flags papers that contradict your key claims;
  - maps who cites whom within your library, and which papers you're missing;
  - powers a semantic search ("≈ meaning") and a library-wide "Ask the
    Librarian" chat.

All model calls go through the [`claude` CLI](https://claude.com/claude-code)
on your own subscription. There is no API key in this repo.

---

## Quick start

```bash
git clone https://github.com/<you>/paper-librarian
cd paper-librarian
python3 app.py --open                 # http://127.0.0.1:8765 ; creates library.db
```

That gives you an empty library. To fill it:

* **From BibTeX**, which is the fastest way. Export your reference manager
  (Zotero, Mendeley, JabRef…) to `.bib`, then run:
  ```bash
  python3 import_bibtex.py my_refs.bib --tag "To read"
  ```
  It is safe to re-run, because papers already in the library are skipped.
* **By hand**, with the `+ Paper` button. Paste a citation; the raw citation
  string is the source of truth, and the title, authors and venue are parsed
  from it for you to correct.
* **PDFs.** Attach one from a paper's PDF tab, or drop files into `papers/`.
  With the Librarian running, a dropped PDF is matched to its paper
  automatically, or filed as a new one.

Requires **Python 3.9+** and a browser. Developed on Linux, and it should work
on macOS. Windows is untested.

## The chat pane

Each paper has a chat pane. **Prerequisite:** the `claude` CLI on your `PATH`,
already logged in. Without it the pane says so and nothing else is affected.

The chat runs `claude -p` with only the `Read` tool and the paper's PDF, and
streams the answer back. Conversations are stored in `library.db` next to
your notes. If the Librarian is installed, the chat can also search the rest
of the library and open other papers' notes.

## The Librarian

### Setup

```bash
cd librarian
uv sync                                   # venv with sqlite-vec + fastembed (local embeddings)
cp .env.example .env && chmod 600 .env    # optional: OpenAlex key, model choice
```

Also needed: `pdftotext` and `pdfinfo`, from poppler
(`apt install poppler-utils`, `brew install poppler`). `inotifywait`
(`inotify-tools`) is optional; without it, new PDFs are picked up by a
10-minute rescan. The [`orx`](https://openresearch.dev) CLI is optional too.
When it's absent, lookups go straight to OpenAlex and arXiv.

From then on, `python3 app.py` starts and stops the Librarian with the app.

### Write your brief

On first start the Librarian copies `librarian/brief.template.md` to
`librarian/brief.md`. **It will not catalog anything until you fill that file
in** and delete the `<!-- TEMPLATE -->` marker on its first line. The template
explains each section with a worked example:

| Section | What it does |
|---|---|
| `## Project` | One paragraph on what you're working on. It goes into every prompt. |
| `## Key claims` | `- **A**: …` bullets. Papers are tagged with the claims they bear on, and a direct contradiction is always flagged. |
| `## Relevance rubric` | What a 0, 1, 2 or 3 means *for you*. |
| `## Note conventions` | What every full note must record, and what it must not overstate. |
| `## Citation gaps` | Sentences in your draft that need a citation. The Librarian lists candidate papers for each. |
| `## Topic areas` / `## Tags` | Coarse groupings, and the tags it may *suggest*. You accept suggestions with one click. It never applies tags itself. |

Editing the brief later is expected. Papers judged 0–1 under an older version
are re-triaged automatically.

### What happens to each paper

1. **Resolve.** Find it on OpenAlex (by DOI, title, the PDF's own title, or
   as a last resort a model reading the citation). This gives the abstract,
   the identifiers and an open-access PDF link.
2. **Text.** Use your PDF, else an open-access PDF, else the arXiv full text,
   else the abstract.
3. **Triage.** A small model (Haiku by default) scores relevance 0–3 against
   the brief, with claims, gaps, tags and a summary.
4. **Deep read.** For relevance 2+ with full text, a larger model (Sonnet)
   reads the whole paper. It writes a note with the claim, the setup, key
   numbers with page refs, relevance, tensions and contradictions, what the
   paper does *not* establish, and what to cite it for. A relevant paper with
   no obtainable full text goes on the **Wanted PDFs** list.
5. **References.** Its citations are matched against your library. Papers
   your library keeps citing but doesn't have ("ghosts") build the
   **Reading queue**. Ghosts cited by 5+ of your papers with relevance 2+ are
   added automatically. Set the `auto_add_ghosts` meta key to `0` to stop that.
6. **Embed.** The notes, abstract, your own notes and the full text are
   embedded locally (bge-small, CPU) for semantic search.

Notes are plain markdown in `librarian/notes/`, shown in each paper's
**Librarian** tab.

**Papers added before the Librarian first ran are not cataloged
automatically.** Click **Start backfill…** in the Librarian → Activity view
(or run `python -m librarian backfill`) when you're ready. A backfill of a few
hundred papers takes hours and a meaningful amount of your Claude usage. The
queue pauses itself when you hit a usage limit and resumes afterwards. Before
a big backfill, it's worth running the optional calibration check in
`librarian/gold_set.template.md`.

### Command line

```bash
cd librarian
.venv/bin/python -m librarian status              # counts, queue, prerequisites
.venv/bin/python -m librarian find <title words>  # look up paper ids
.venv/bin/python -m librarian ingest 12 40 --force
.venv/bin/python -m librarian backfill
.venv/bin/python -m librarian gold                # calibration check
```

### Using it from Claude Code

`librarian/librarian/mcp.py` is a dependency-free MCP server exposing the
Librarian's read-only tools: `search`, `get_note`, `related`, `top_cited`,
`for_claim`, `for_gap`, `wanted`, `status`, and `draft_*`. To use your library
from any Claude Code session:

```bash
claude mcp add librarian -- /path/to/paper-librarian/librarian/.venv/bin/python \
  -m librarian.mcp   # with env PYTHONPATH=/path/to/paper-librarian/librarian
```

## The Draft tab

Point the app at a LaTeX draft (`python3 app.py --draft path/to/main.tex`;
the path is remembered afterwards). It renders the draft, surfaces
`\todo{}`-style notes, and links each `\cite{key}` to the matching paper in
your library. Each paper's Details tab then shows where your draft cites it.
The Librarian's chat can read the draft too. It is entirely optional.

## Architecture

| file | role |
|---|---|
| `app.py` | `ThreadingHTTPServer`: JSON API, static files, PDF streaming with Range support, `/api/librarian/*` proxy |
| `db.py` / `schema.sql` | every SQL statement in the app; papers, tags, notes, FTS5 index |
| `chat.py` | per-paper chat; drives the `claude` CLI |
| `thesis.py` | LaTeX → HTML for the Draft tab |
| `librarian_link.py` | starts/stops the Librarian daemon; degrades to a no-op if it isn't installed |
| `import_bibtex.py` | bulk import |
| `static/` | the whole frontend: vanilla JS, no build step |
| `librarian/librarian/` | the agent: `daemon` (HTTP on :8766), `worker` (job queue), `catalog` (per-paper pipeline), `refs`, `embed`, `agent` (library chat), `mcp`, `tools` |

The web app must keep working, unchanged, with no `librarian/.venv`. The
Librarian writes to `library.db` only through the app's `db.py`. It never
edits your notes and never applies tags. Its own state, in
`librarian/librarian.db`, is derived and can be rebuilt, at the cost of tokens.

## Your data

Nothing personal is tracked by git. `library.db`, `papers/`,
`librarian/brief.md`, `librarian/notes/`, `librarian/librarian.db` and
`librarian/.env` are all gitignored.

**`library.db` is the only copy of your notes and tags. Back it up.** It runs
in WAL mode, so a plain `cp` isn't a safe backup. Use sqlite's backup API:

```bash
python3 -c "
import sqlite3, datetime
src = sqlite3.connect('library.db')
dst = sqlite3.connect(f'library.backup-{datetime.date.today()}.db')
src.backup(dst); dst.close(); src.close(); print('ok')"
```

Published PDFs are usually copyrighted. Don't commit them to a public fork.

## What leaves your machine

- Paper titles, DOIs and arXiv ids go to OpenAlex, arXiv and (if installed)
  `orx` to look papers up.
- Paper text, your brief, and (in chat) your notes go to Claude through
  your `claude` CLI login.
- Embeddings are computed locally. The model is downloaded once from Hugging
  Face.

## Security

The server binds to `127.0.0.1`, and **the API has no authentication**.
Anyone who can reach the port can read and edit everything, and can run
`claude` turns on your account. Don't use `--host 0.0.0.0` on a network you
don't trust. For remote access, use an SSH tunnel:
`ssh -L 8765:127.0.0.1:8765 host`.

## Licence

MIT. See [LICENSE](LICENSE).
