#!/usr/bin/env python3
"""Paper Librarian — a small stdlib-only reading-library web app.

    python3 app.py --open             # creates library.db on first run
    python3 import_bibtex.py refs.bib # optional: bulk-import a .bib file

No pip install, no build step. Binds to loopback only by default; the API is
unauthenticated, so keep it that way unless you add auth yourself.
"""

import argparse
import atexit
import hashlib
import json
import mimetypes
import os
import re
import signal
import tempfile
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs, quote, unquote

import chat
import db
import librarian_link as librarian
import thesis as tex

ROOT = Path(__file__).resolve().parent
STATIC_DIR = ROOT / "static"
MAX_UPLOAD = 200 * 1024 * 1024  # 200 MB

# Set by --draft; otherwise the path comes from the settings table, and
# failing that it is autodetected. The draft is never snapshotted -- every
# request re-reads whatever is on disk.
THESIS_OVERRIDE = None

_local = threading.local()
_render_lock = threading.Lock()
_render_cache = {"key": None, "data": None}


def conn():
    """One sqlite connection per worker thread."""
    if not hasattr(_local, "conn"):
        _local.conn = db.connect()
    return _local.conn


def safe_pdf_path(rel):
    """Resolve `rel` inside papers/ or reject it. Blocks ../ traversal."""
    rel = unquote(rel or "").lstrip("/")
    if not rel:
        return None
    target = (db.PAPERS_DIR / rel).resolve()
    try:
        target.relative_to(db.PAPERS_DIR.resolve())
    except ValueError:
        return None
    return target if target.is_file() else None


def same_bytes(target, size, digest):
    """True if `target` is already exactly the upload we just received."""
    if target.stat().st_size != size:
        return False           # cheap reject before reading the whole file
    h = hashlib.sha256()
    with open(target, "rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest() == digest


def thesis_doc(c):
    """The LaTeX draft the app is pointing at right now, or None."""
    path = THESIS_OVERRIDE or db.get_setting(c, "thesis_tex", "")
    if not path:
        found = tex.autodetect(ROOT)
        path = str(found) if found else ""
    return tex.Thesis(path) if path else None


def thesis_render(doc):
    """Render the draft, reusing the last result while the files are unchanged."""
    key = (str(doc.tex), doc.mtime())
    with _render_lock:
        if _render_cache["key"] == key:
            return _render_cache["data"]
    data = tex.build(doc)
    with _render_lock:
        _render_cache["key"], _render_cache["data"] = key, data
    return data


def thesis_payload(c, doc):
    """Rendered draft + the library link for every citation."""
    data = thesis_render(doc)
    links = db.resolve_bib_links(c, data["bib"])

    cites = []
    for cite in data["cites"]:
        link = links.get(cite["key"], {})
        pid = link.get("paper_id")
        paper = db.get_paper(c, pid) if pid else None
        entry = cite["entry"] or {}
        cites.append({
            "key": cite["key"],
            "number": cite["number"],
            "known": cite["known"],
            "title": entry.get("title", ""),
            "author": tex.bib_authors_short(entry.get("author", "")),
            "year": entry.get("year", ""),
            "journal": entry.get("journal", ""),
            "url": entry.get("url", "") or (
                "https://doi.org/" + entry["doi"] if entry.get("doi") else ""),
            "how": link.get("how", ""),
            "manual": link.get("manual", False),
            "ignored": link.get("ignored", False),
            "paper": {k: paper[k] for k in
                      ("id", "title", "authors", "year", "venue", "pdf_path",
                       "has_notes", "read_status", "tags", "popularity")}
                     if paper else None,
        })

    db.sync_in_draft(c, [x["paper"]["id"] for x in cites if x["paper"]])

    # Entries that sit in the .bib but are never \\cite'd in the draft.
    cited_keys = {x["key"] for x in cites}
    uncited = [{"key": k, "title": e.get("title", ""),
                "author": tex.bib_authors_short(e.get("author", "")),
                "year": e.get("year", ""),
                "paper_id": links.get(k, {}).get("paper_id")}
               for k, e in data["bib"].items() if k not in cited_keys]

    status = doc.status()
    return {
        "tex": status["tex"],
        "mtime": status["mtime"],
        "pdf": status["pdf"],
        "pdf_mtime": status["pdf_mtime"],
        "html": data["html"],
        "toc": data["toc"],
        "todos": data["todos"],
        "cites": cites,
        "sites": data["sites"],
        "uncited": uncited,
        "duplicates": data["duplicates"],
        "pages": tex.toc_pages(doc),
        "bib_files": [Path(b).name for b in data["bib_files"]],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "ReadingLibrary/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------------ util
    def log_message(self, fmt, *args):
        if not self.server.quiet:
            super().log_message(fmt, *args)

    def _send(self, status, body=b"", ctype="application/octet-stream", extra=None):
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def json_out(self, payload, status=HTTPStatus.OK):
        self._send(status, json.dumps(payload, ensure_ascii=False),
                   "application/json; charset=utf-8",
                   {"Cache-Control": "no-store"})

    def fail(self, status, message):
        self.json_out({"error": message}, status)

    def body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if not length:
            return {}
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    # --------------------------------------------------------------- routing
    def do_GET(self):
        url = urlparse(self.path)
        path, query = url.path, parse_qs(url.query)
        try:
            if path.startswith("/api/"):
                return self.api_get(path, query)
            if path.startswith("/pdf/"):
                return self.serve_pdf(path[len("/pdf/"):])
            return self.serve_static(path)
        except BrokenPipeError:
            pass
        except Exception as exc:  # keep the server alive on a bad request
            self.log_error("%s", exc)
            self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    do_HEAD = do_GET

    def do_POST(self):
        return self._mutate("POST")

    def do_PATCH(self):
        return self._mutate("PATCH")

    def do_PUT(self):
        return self._mutate("PUT")

    def do_DELETE(self):
        return self._mutate("DELETE")

    def _mutate(self, verb):
        url = urlparse(self.path)
        try:
            if url.path == "/api/upload" and verb == "PUT":
                return self.upload_pdf(parse_qs(url.query))
            if not url.path.startswith("/api/"):
                return self.fail(HTTPStatus.NOT_FOUND, "not found")
            payload = self.body_json()
            if payload is None:
                return self.fail(HTTPStatus.BAD_REQUEST, "invalid JSON body")
            # Chat streams its response, so it cannot go through api_write --
            # that path ends in a single buffered json_out.
            m = re.fullmatch(r"/api/papers/(\d+)/chat", url.path)
            if m and verb == "POST":
                return self.stream_chat(int(m.group(1)), payload)
            if url.path == "/api/librarian/chat" and verb == "POST":
                return self.librarian_chat(payload)
            if url.path.startswith("/api/librarian/") and verb == "POST":
                return self.librarian_proxy("POST", url.path, parse_qs(url.query), payload)
            return self.api_write(verb, url.path, payload)
        except BrokenPipeError:
            pass
        except Exception as exc:
            self.log_error("%s", exc)
            self.fail(HTTPStatus.INTERNAL_SERVER_ERROR, str(exc))

    # ------------------------------------------------------------- API: read
    def api_get(self, path, q):
        def one(key, default=""):
            return q.get(key, [default])[0]

        def many(key):
            return [v for v in q.get(key, []) if v]

        def tri(key):
            value = one(key)
            return True if value == "1" else False if value == "0" else None

        if path == "/api/bootstrap":
            return self.json_out(db.facets(conn()))

        m = re.fullmatch(r"/api/librarian/pdf/(\d+)", path)
        if m:
            # An open-access PDF the Librarian downloaded into its cache. The
            # path is built from an integer id, so it can't escape that folder.
            target = librarian.LIB_DIR / "cache" / "pdf" / f"{int(m.group(1))}.pdf"
            if not target.is_file():
                return self.fail(HTTPStatus.NOT_FOUND, "no cached pdf")
            return self.stream_file(target, "application/pdf")

        if path.startswith("/api/librarian/"):
            return self.librarian_proxy("GET", path, q, None)

        if path == "/api/papers":
            only_ids = None
            if one("lib_fulltext") == "1":
                status, body, _ = librarian.request("GET", "/fulltext")
                only_ids = list(json.loads(body)) if status == 200 else []
            papers, total = db.search_papers(
                conn(),
                q=one("q"), tags=many("tag"), keywords=many("keyword"),
                min_pop=int(one("min_pop", "0") or 0),
                has_pdf=tri("has_pdf"), has_notes=tri("has_notes"),
                read_status=one("read_status"), flag=one("flag"),
                sort=one("sort", "popularity"),
                limit=min(int(one("limit", "60") or 60), 1000),
                offset=int(one("offset", "0") or 0), only_ids=only_ids)
            return self.json_out({"papers": papers, "total": total})

        m = re.fullmatch(r"/api/papers/(\d+)", path)
        if m:
            paper = db.get_paper(conn(), int(m.group(1)))
            return self.json_out(paper) if paper else self.fail(
                HTTPStatus.NOT_FOUND, "no such paper")

        m = re.fullmatch(r"/api/papers/(\d+)/chat", path)
        if m:
            pid = int(m.group(1))
            return self.json_out({
                "messages": db.chat_history(conn(), pid),
                "model": db.chat_thread(conn(), pid)["model"] or chat.DEFAULT_MODEL,
                "backend": chat.available(),
            })

        if path == "/api/pdfs":
            return self.json_out({"files": self.list_pdfs()})

        if path.startswith("/api/thesis"):
            return self.thesis_get(path, q)

        return self.fail(HTTPStatus.NOT_FOUND, "unknown endpoint")

    # ----------------------------------------------------------- API: thesis
    def thesis_get(self, path, q):
        doc = thesis_doc(conn())
        if doc is None or not doc.exists():
            where = str(doc.tex) if doc else "(not configured)"
            if path == "/api/thesis/status":
                return self.json_out({"exists": False, "tex": where})
            return self.fail(HTTPStatus.NOT_FOUND,
                             f"no draft .tex found — looked for {where}. "
                             f"Start app.py with --draft /path/to/main.tex")

        if path == "/api/thesis":
            return self.json_out(thesis_payload(conn(), doc))

        if path == "/api/thesis/status":
            return self.json_out(doc.status())

        if path == "/api/thesis/pdf":
            if not doc.pdf.is_file():
                return self.fail(HTTPStatus.NOT_FOUND, "no compiled PDF next to the .tex")
            return self.stream_file(doc.pdf, "application/pdf")

        if path.startswith("/api/thesis/asset/"):
            target = doc.safe_path(unquote(path[len("/api/thesis/asset/"):]))
            if target is None:
                return self.fail(HTTPStatus.NOT_FOUND, "asset not found")
            ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
            return self.stream_file(target, ctype)

        m = re.fullmatch(r"/api/thesis/backlinks/(\d+)", path)
        if m:
            return self.json_out(self.backlinks(doc, int(m.group(1))))

        return self.fail(HTTPStatus.NOT_FOUND, "unknown endpoint")

    def backlinks(self, doc, paper_id):
        """Where in the draft this paper is cited (the Details-tab panel)."""
        c = conn()
        keys = db.bibkeys_for_paper(c, paper_id)
        data = thesis_render(doc)
        numbers = {cite["key"]: cite["number"] for cite in data["cites"]}
        sites = []
        for site in data["sites"]:
            if site["key"] in keys and not any(
                    s["anchor"] == site["anchor"] and s["key"] == site["key"]
                    for s in sites):
                sites.append(site)
        return {"paper_id": paper_id, "bibkeys": keys,
                "numbers": {k: numbers.get(k) for k in keys},
                "in_bib": [k for k in keys if k in data["bib"]],
                "sites": sites}

    def list_pdfs(self):
        attached = {r["pdf_path"] for r in
                    conn().execute("SELECT pdf_path FROM papers WHERE pdf_path <> ''")}
        files = []
        for p in sorted(db.PAPERS_DIR.glob("**/*.pdf")):
            rel = str(p.relative_to(db.PAPERS_DIR))
            files.append({"path": rel, "size": p.stat().st_size,
                          "attached": rel in attached})
        return files

    # ------------------------------------------------------------ API: write
    def api_write(self, verb, path, payload):
        c = conn()

        if path == "/api/papers" and verb == "POST":
            paper = db.create_paper(c, payload)
            librarian.notify("/enqueue", {"paper_id": paper["id"]})
            return self.json_out(paper, HTTPStatus.CREATED)

        m = re.fullmatch(r"/api/papers/(\d+)", path)
        if m:
            pid = int(m.group(1))
            if not db.get_paper(c, pid):
                return self.fail(HTTPStatus.NOT_FOUND, "no such paper")
            if verb == "PATCH":
                if "pdf_path" in payload and payload["pdf_path"]:
                    if safe_pdf_path(payload["pdf_path"]) is None:
                        return self.fail(HTTPStatus.BAD_REQUEST, "pdf not found in papers/")
                before = db.get_paper(c, pid)
                updated = db.update_paper(c, pid, payload)
                if "pdf_path" in payload and payload["pdf_path"] != before["pdf_path"]:
                    librarian.notify("/enqueue", {"paper_id": pid, "force": True})
                return self.json_out(updated)
            if verb == "DELETE":
                db.delete_paper(c, pid)
                return self.json_out({"ok": True})

        m = re.fullmatch(r"/api/papers/(\d+)/chat", path)
        if m and verb == "DELETE":
            db.clear_chat(c, int(m.group(1)))
            return self.json_out({"ok": True})

        m = re.fullmatch(r"/api/papers/(\d+)/tags", path)
        if m and verb == "POST":
            pid = int(m.group(1))
            for name in re.split(r"[,\n]", str(payload.get("name", ""))):
                db.add_tag(c, pid, name)
            return self.json_out(db.get_paper(c, pid))

        m = re.fullmatch(r"/api/papers/(\d+)/tags/(.+)", path)
        if m and verb == "DELETE":
            pid = int(m.group(1))
            db.remove_tag(c, pid, unquote(m.group(2)))
            return self.json_out(db.get_paper(c, pid))

        if path.startswith("/api/thesis/") and verb == "POST":
            return self.thesis_write(path, payload)

        m = re.fullmatch(r"/api/tags/(\d+)", path)
        if m:
            tid = int(m.group(1))
            if verb == "PATCH":
                db.rename_tag(c, tid, payload.get("name"), payload.get("color"))
                return self.json_out({"ok": True})
            if verb == "DELETE":
                db.delete_tag(c, tid)
                return self.json_out({"ok": True})

        return self.fail(HTTPStatus.NOT_FOUND, "unknown endpoint")

    def thesis_write(self, path, payload):
        c = conn()

        if path == "/api/thesis/path":
            candidate = tex.Thesis(payload.get("path", ""))
            if not candidate.exists():
                return self.fail(HTTPStatus.BAD_REQUEST, "no such .tex file")
            db.set_setting(c, "thesis_tex", str(candidate.tex))
            return self.json_out(candidate.status())

        if path == "/api/thesis/link":
            bibkey = str(payload.get("bibkey", "")).strip()
            if not bibkey:
                return self.fail(HTTPStatus.BAD_REQUEST, "bibkey required")
            paper_id = payload.get("paper_id")
            ignored = bool(payload.get("ignored"))
            if paper_id is not None and not db.get_paper(c, int(paper_id)):
                return self.fail(HTTPStatus.NOT_FOUND, "no such paper")
            db.set_bib_link(c, bibkey, int(paper_id) if paper_id else None,
                            manual=not ignored or paper_id is not None,
                            ignored=ignored)
            return self.json_out({"ok": True})

        if path == "/api/thesis/bibentry":
            return self.add_bib_entry(c, payload)

        return self.fail(HTTPStatus.NOT_FOUND, "unknown endpoint")

    def add_bib_entry(self, c, payload):
        """Append a library paper to references.bib and link it.

        The reverse of "add to library": a paper you already have notes on
        becomes a \\cite-able entry without retyping it. Appends only -- it
        never rewrites or reorders what is already in the file.
        """
        doc = thesis_doc(c)
        if doc is None or not doc.exists():
            return self.fail(HTTPStatus.NOT_FOUND, "no draft configured")
        paper = db.get_paper(c, int(payload.get("paper_id") or 0))
        if not paper:
            return self.fail(HTTPStatus.NOT_FOUND, "no such paper")

        bib_files = doc.bib_paths()
        if not bib_files:
            return self.fail(HTTPStatus.BAD_REQUEST,
                             "the draft declares no .bib file")
        target = bib_files[0]
        entries, _dupes = tex.parse_bib(bib_files)
        key = str(payload.get("key") or "").strip() or tex.suggest_key(paper, entries)
        if key in entries:
            return self.fail(HTTPStatus.CONFLICT,
                             f"{key} already exists in {target.name}")

        entry = tex.make_bibtex(paper, key)
        if payload.get("preview"):
            return self.json_out({"key": key, "entry": entry, "file": target.name,
                                  "preview": True})
        with open(target, "a", encoding="utf-8") as fh:
            fh.write("\n" + entry)
        db.set_bib_link(c, key, paper["id"], manual=1, ignored=0)
        return self.json_out({"key": key, "entry": entry, "file": target.name,
                              "cite": "\\cite{%s}" % key}, HTTPStatus.CREATED)

    # -------------------------------------------------------------- API: chat
    def stream_chat(self, pid, payload):
        """POST /api/papers/<id>/chat -- one turn, streamed back as SSE.

        Server-Sent Events rather than a buffered reply because a first turn
        reads a whole PDF and can take half a minute; the pane shows the
        answer as it arrives. protocol_version is HTTP/1.1, so a body of
        unknown length has to be delimited by closing the connection --
        hence Connection: close and no Content-Length.

        ThreadingHTTPServer gives this its own thread, so a long turn does
        not block the rest of the app. The turn is written to the database
        once, at the end: a half-finished answer is not worth persisting.
        """
        c = conn()
        paper = db.get_paper(c, pid)
        if not paper:
            return self.fail(HTTPStatus.NOT_FOUND, "no such paper")
        message = str(payload.get("message", "")).strip()
        if not message:
            return self.fail(HTTPStatus.BAD_REQUEST, "message required")
        model = chat.normalise_model(payload.get("model", ""))

        thread = db.chat_thread(c, pid)
        db.add_chat_message(c, pid, "user", message, model)

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        def emit(event):
            self.wfile.write(
                ("data: " + json.dumps(event, ensure_ascii=False) + "\n\n")
                .encode("utf-8"))
            self.wfile.flush()

        turn = chat.stream_turn(paper, message, model, thread["session_id"])
        try:
            for event in turn:
                if event["type"] == "session":
                    db.set_chat_thread(c, pid, event["id"], model)
                elif event["type"] == "done":
                    if event["text"].strip():
                        event["id"] = db.add_chat_message(
                            c, pid, "assistant", event["text"], model)
                emit(event)
        except (BrokenPipeError, ConnectionResetError):
            # Reader navigated away mid-answer. Closing the generator kills
            # the child process rather than leaving it running to no one.
            turn.close()
        finally:
            turn.close()

    # --------------------------------------------------------- API: librarian
    # GET/POST /api/librarian/<rest>  ->  daemon /<rest>. The allowlist keeps
    # the browser from reaching daemon endpoints it has no business calling
    # (/tool/* is for the MCP server only).
    LIBRARIAN_GET = ("status", "search", "ghosts", "top", "requests", "digest",
                     "chat/history", "fulltext")
    LIBRARIAN_POST = ("enqueue", "pause", "resume", "backfill", "chat/reset")

    def librarian_proxy(self, method, path, q, payload):
        rest = path[len("/api/librarian/"):]
        allowed = (rest in self.LIBRARIAN_GET or re.fullmatch(r"paper/\d+", rest)
                   if method == "GET" else
                   rest in self.LIBRARIAN_POST or re.fullmatch(r"(request|ghost)/\d+", rest))
        if not allowed:
            return self.fail(HTTPStatus.NOT_FOUND, "unknown librarian endpoint")
        if not librarian.installed():
            return self.json_out({"error": "librarian not installed", "offline": True},
                                 HTTPStatus.SERVICE_UNAVAILABLE)
        qs = "&".join(f"{quote(k)}={quote(v[0])}" for k, v in q.items())
        status, body, ctype = librarian.request(
            method, "/" + rest + (f"?{qs}" if qs else ""), payload,
            timeout=120 if rest in ("search", "backfill") else 30)
        self._send(status, body, ctype, {"Cache-Control": "no-store"})

    def librarian_chat(self, payload):
        """POST /api/librarian/chat -- relay the daemon's SSE stream as-is."""
        if not librarian.installed():
            return self.fail(HTTPStatus.SERVICE_UNAVAILABLE, "librarian not installed")
        try:
            upstream = librarian.stream("/chat", {"message": payload.get("message", "")})
        except Exception as exc:
            return self.fail(HTTPStatus.SERVICE_UNAVAILABLE, f"librarian unavailable: {exc}")
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        try:
            for line in upstream:
                self.wfile.write(line)
                if line == b"\n":
                    self.wfile.flush()
            self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            upstream.close()

    def upload_pdf(self, q):
        """Raw-body PDF upload: PUT /api/upload?name=foo.pdf

        Raw body rather than multipart so the stdlib can parse it in one line.
        """
        name = os.path.basename(unquote(q.get("name", [""])[0]))
        if not name.lower().endswith(".pdf"):
            return self.fail(HTTPStatus.BAD_REQUEST, "name must end in .pdf")
        length = int(self.headers.get("Content-Length") or 0)
        if not 0 < length <= MAX_UPLOAD:
            return self.fail(HTTPStatus.BAD_REQUEST, "empty or oversized upload")

        # Land the body in a temp file first, hashing it on the way in. The
        # bytes have to come off the socket either way, and the digest is what
        # lets a re-upload of a file already in papers/ -- picking the same pdf
        # twice, or browsing into papers/ from the file dialog -- attach the
        # original instead of leaving foo-1.pdf, foo-2.pdf of identical bytes.
        fd, tmp_path = tempfile.mkstemp(
            dir=db.PAPERS_DIR, prefix=".upload-", suffix=".part")
        tmp = Path(tmp_path)
        digest, remaining = hashlib.sha256(), length
        try:
            with os.fdopen(fd, "wb") as fh:
                while remaining > 0:
                    chunk = self.rfile.read(min(1 << 20, remaining))
                    if not chunk:
                        break
                    fh.write(chunk)
                    digest.update(chunk)
                    remaining -= len(chunk)
            written, digest = length - remaining, digest.hexdigest()

            target = db.PAPERS_DIR / name
            stem, suffix, n = target.stem, target.suffix, 1
            while target.exists():  # never clobber an existing pdf
                if same_bytes(target, written, digest):
                    tmp.unlink()
                    return self.json_out({"path": target.name}, HTTPStatus.OK)
                target = db.PAPERS_DIR / f"{stem}-{n}{suffix}"
                n += 1
            tmp.replace(target)
        except BaseException:
            tmp.unlink(missing_ok=True)
            raise
        return self.json_out({"path": target.name}, HTTPStatus.CREATED)

    # ------------------------------------------------------------- files
    def serve_pdf(self, rel):
        target = safe_pdf_path(rel)
        if target is None:
            return self.fail(HTTPStatus.NOT_FOUND, "pdf not found")
        return self.stream_file(target, "application/pdf")

    def stream_file(self, target, ctype):
        """Send a file with HTTP Range support, so PDF viewers can seek."""
        size = target.stat().st_size
        rng = self.headers.get("Range", "")
        start, end = 0, size - 1
        status = HTTPStatus.OK

        m = re.fullmatch(r"bytes=(\d*)-(\d*)", rng.strip())
        if m and (m.group(1) or m.group(2)):
            if m.group(1):
                start = int(m.group(1))
                end = int(m.group(2)) if m.group(2) else size - 1
            else:  # suffix range: last N bytes
                start = max(0, size - int(m.group(2)))
            end = min(end, size - 1)
            if start > end:
                return self._send(HTTPStatus.REQUESTED_RANGE_NOT_SATISFIABLE, b"",
                                  "text/plain", {"Content-Range": f"bytes */{size}"})
            status = HTTPStatus.PARTIAL_CONTENT

        length = end - start + 1
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(length))
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Content-Disposition",
                         f'inline; filename="{target.name}"')
        if status == HTTPStatus.PARTIAL_CONTENT:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        self.end_headers()
        if self.command == "HEAD":
            return
        with open(target, "rb") as fh:
            fh.seek(start)
            while length > 0:
                chunk = fh.read(min(1 << 16, length))
                if not chunk:
                    break
                self.wfile.write(chunk)
                length -= len(chunk)

    def serve_static(self, path):
        rel = "index.html" if path in ("/", "") else path.lstrip("/")
        target = (STATIC_DIR / unquote(rel)).resolve()
        try:
            target.relative_to(STATIC_DIR.resolve())
        except ValueError:
            return self.fail(HTTPStatus.NOT_FOUND, "not found")
        if not target.is_file():
            return self.fail(HTTPStatus.NOT_FOUND, "not found")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith(("text/", "application/javascript")):
            ctype += "; charset=utf-8"
        self._send(HTTPStatus.OK, target.read_bytes(), ctype,
                   {"Cache-Control": "no-cache"})


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8765)
    ap.add_argument("--open", action="store_true", help="open a browser tab")
    ap.add_argument("--quiet", action="store_true", help="silence request logging")
    ap.add_argument("--draft", "--thesis", dest="thesis", metavar="MAIN.TEX",
                    help="path to your LaTeX draft (default: remembered, else autodetected)")
    args = ap.parse_args()

    global THESIS_OVERRIDE
    if args.thesis:
        candidate = tex.Thesis(args.thesis)
        if not candidate.exists():
            raise SystemExit(f"no such draft file: {args.thesis}")
        THESIS_OVERRIDE = str(candidate.tex)

    if not db.DB_PATH.exists():
        print(f"creating an empty library at {db.DB_PATH.name}")
    db.PAPERS_DIR.mkdir(exist_ok=True)

    httpd = ThreadingHTTPServer((args.host, args.port), Handler)
    httpd.quiet = args.quiet
    url = f"http://{args.host}:{args.port}/"
    print(f"reading library on {url}   (ctrl-c to stop)")
    probe = db.connect()
    db.init(probe)          # idempotent: creates the schema on first run
    doc = thesis_doc(probe)
    probe.close()
    if doc and doc.exists():
        print(f"draft: {doc.tex}")
    else:
        print("draft: none (optional) — pass --draft /path/to/main.tex to enable the Draft tab")
    # Stop the Librarian however the app ends: Ctrl-C, SIGTERM, or a crash.
    # SIGINT is re-armed explicitly because a backgrounded process inherits
    # it ignored, which would leave the daemon running after the app dies.
    signal.signal(signal.SIGINT, signal.default_int_handler)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    atexit.register(librarian.stop)
    print(f"librarian: {librarian.start()}")
    if args.open:
        threading.Timer(0.5, webbrowser.open, [url]).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nbye")
    finally:
        librarian.stop()


if __name__ == "__main__":
    main()
