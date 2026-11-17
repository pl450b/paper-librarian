"""The Librarian daemon: started by the web app's app.py, stopped with it.

Runs the worker and the papers/ watcher, and serves a small JSON API on 127.0.0.1 only (the API has no auth, like the app):

  GET  /health                  liveness
  GET  /status                  queue, pause state, counts, recent events
  GET  /paper/<id>              state + note + references + cited-by
  GET  /search?q=&k=&min_rel=&ghosts=1
  GET  /ghosts?n=   /top?n=   /requests   /digest
  GET  /chat/history?channel=web
  POST /enqueue   {paper_id, kind?, force?, priority?}
  POST /file      {name}                       a PDF that landed in papers/
  POST /pause  /resume
  POST /backfill                               queue every un-cataloged paper
  POST /request/<id>  {state}                  declined|snoozed|pending
  POST /ghost/<id>    {state}                  dismissed|added
  POST /tool/<name>  {args}                   used by the MCP server
  POST /chat      {message, channel?}          SSE stream of one chat turn
  POST /chat/reset {channel}
"""

import json
import os
import re
import signal
import sys
import threading
import time
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from . import config, context, refs, store, tools, worker

LOG_LOCK = threading.Lock()


def log(msg):
    with LOG_LOCK:
        print(f"[librarian {time.strftime('%H:%M:%S')}] {msg}", flush=True)


class State:
    worker = None
    watcher = None
    started = time.time()


def status():
    c = store.conn()
    lib = __import__("librarian.catalog", fromlist=["libdb"]).libdb()
    total = lib.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    tiers = {r["tier"]: r["n"] for r in c.execute(
        "SELECT tier, COUNT(*) n FROM paper_state WHERE cataloged_at IS NOT NULL GROUP BY tier")}
    q = store.queue_stats(c)
    running = State.worker.current if State.worker else None
    return {
        "ok": True, "papers": total, "cataloged": sum(tiers.values()), "tiers": tiers,
        "queued": sum(q["queued"].values()), "queue": q["queued"], "failed": q["failed"],
        "running": {"kind": running["kind"], "paper_id": running["paper_id"],
                    "arg": running["arg"]} if running else None,
        "paused": State.worker.paused() if State.worker else "",
        "backfill": store.get_meta(c, "backfill") == "1",
        "requests": c.execute("SELECT COUNT(*) FROM requests WHERE state IN"
                              " ('pending','asked')").fetchone()[0],
        "brief_missing": context.is_template(),
        "claims": dict(context.claims()),
        "events": store.recent_events(c, 8),
    }


def paper_detail(pid):
    c = store.conn()
    st = store.get_state(c, pid)
    if not st:
        return {"paper_id": pid, "cataloged": False}
    note = ""
    if st.get("note_path") and (config.LIB_DIR / st["note_path"]).exists():
        note = (config.LIB_DIR / st["note_path"]).read_text(encoding="utf-8")
    req = c.execute("SELECT * FROM requests WHERE paper_id = ?", (pid,)).fetchone()
    from . import catalog
    def card(i):
        p = catalog.get_paper(i)
        return {"id": i, "title": p["title"] if p else f"#{i}", "year": p["year"] if p else ""}
    return {"paper_id": pid, "cataloged": bool(st.get("cataloged_at")), "state": st,
            "note": note, "references": refs.references(pid),
            "cited_by": [card(i) for i in refs.cited_by(pid)],
            "request": dict(req) if req else None}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        pass

    def _json(self, payload, status=HTTPStatus.OK):
        body = json.dumps(payload, ensure_ascii=False, default=str).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        try:
            return json.loads(self.rfile.read(n).decode("utf-8"))
        except ValueError:
            return {}

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        try:
            if u.path == "/health":
                return self._json({"ok": True, "pid": os.getpid()})
            if u.path == "/status":
                return self._json(status())
            m = re.fullmatch(r"/paper/(\d+)", u.path)
            if m:
                return self._json(paper_detail(int(m.group(1))))
            if u.path == "/search":
                mr = q.get("min_rel")
                return self._json(tools.search(q.get("q", ""), k=int(q.get("k", 20)),
                                               min_relevance=int(mr) if mr else None,
                                               include_ghosts=q.get("ghosts") == "1"))
            if u.path == "/fulltext":
                # Papers the Librarian read in full from its own sources (not a
                # linked PDF in papers/): an open-access PDF it downloaded, or arXiv.
                rows = store.conn().execute(
                    "SELECT paper_id, text_source, arxiv_id FROM paper_state"
                    " WHERE text_source IN ('oa_pdf', 'arxiv') AND cataloged_at IS NOT NULL")
                return self._json({str(r["paper_id"]): {
                    "source": r["text_source"], "arxiv_id": r["arxiv_id"],
                    "pdf": (config.PDF_CACHE / f"{r['paper_id']}.pdf").exists()}
                    for r in rows})
            if u.path == "/ghosts":
                return self._json(refs.top_ghosts(int(q.get("n", 50)), int(q.get("min", 2))))
            if u.path == "/top":
                return self._json(tools.top_cited("library", int(q.get("n", 25))))
            if u.path == "/requests":
                return self._json(tools.wanted())
            if u.path == "/digest":
                return self._json({"text": config.DIGEST_PATH.read_text(encoding="utf-8")
                                   if config.DIGEST_PATH.exists() else ""})
            if u.path == "/chat/history":
                ch = q.get("channel", "web")
                return self._json({"messages": store.history(store.conn(), ch)})
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"},
                              HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_POST(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        c = store.conn()
        try:
            if u.path == "/chat":
                return self._chat(self._body())
            b = self._body()
            if u.path == "/enqueue":
                kind = b.get("kind", "catalog")
                worker.enqueue(kind, int(b["paper_id"]), "force" if b.get("force") else "",
                               int(b.get("priority", worker.PRIORITY_USER)))
                return self._json({"ok": True})
            if u.path == "/file":
                worker.enqueue("file", None, os.path.basename(b.get("name", "")),
                               worker.PRIORITY_USER, delay_s=2)
                return self._json({"ok": True})
            if u.path == "/pause":
                store.set_meta(c, "paused", "1")
                return self._json(status())
            if u.path == "/resume":
                store.set_meta(c, "paused", "0")
                store.set_meta(c, "paused_until", "")
                return self._json(status())
            if u.path == "/backfill":
                store.set_meta(c, "backfill", "1")
                return self._json(worker.reconcile(log))
            m = re.fullmatch(r"/request/(\d+)", u.path)
            if m:
                state = b.get("state", "")
                if state not in ("declined", "snoozed", "pending"):
                    return self._json({"error": "bad state"}, HTTPStatus.BAD_REQUEST)
                extra = {"snooze_until": time.strftime("%Y-%m-%d %H:%M:%S",
                                                       time.gmtime(time.time() + 7 * 86400))
                         } if state == "snoozed" else {}
                store.set_request(c, int(m.group(1)), state=state, **extra)
                return self._json({"ok": True})
            m = re.fullmatch(r"/ghost/(\d+)", u.path)
            if m:
                if b.get("state") not in ("dismissed", "added"):
                    return self._json({"error": "bad state"}, HTTPStatus.BAD_REQUEST)
                c.execute("UPDATE ghosts SET state = ? WHERE id = ?",
                          (b["state"], int(m.group(1))))
                c.commit()
                return self._json({"ok": True})
            m = re.fullmatch(r"/tool/(\w+)", u.path)
            if m:
                return self._json(tools.call(m.group(1), b))
            if u.path == "/chat/reset":
                store.clear_channel(c, b.get("channel", "web"))
                return self._json({"ok": True})
            return self._json({"error": "not found"}, HTTPStatus.NOT_FOUND)
        except Exception as e:
            return self._json({"error": f"{type(e).__name__}: {e}"},
                              HTTPStatus.INTERNAL_SERVER_ERROR)

    def _chat(self, b):
        from . import agent
        message = str(b.get("message", "")).strip()
        channel = "web"
        if not message:
            return self._json({"error": "message required"}, HTTPStatus.BAD_REQUEST)
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True
        turn = agent.stream_turn(channel, message)
        try:
            for ev in turn:
                self.wfile.write(("data: " + json.dumps(ev, ensure_ascii=False) + "\n\n")
                                 .encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            turn.close()


def main(port=config.DAEMON_PORT):
    config.ensure_dirs()
    try:
        httpd = ThreadingHTTPServer((config.DAEMON_HOST, port), Handler)
    except OSError as e:
        log(f"cannot bind {config.DAEMON_HOST}:{port} ({e}); is another librarian running?")
        sys.exit(3)
    httpd.daemon_threads = True
    log(f"serving on http://{config.DAEMON_HOST}:{port}")

    # Import everything heavy *before* any thread starts, so two threads never
    # race to initialise the same module.
    from . import agent, catalog, embed, refs as _refs  # noqa: F401

    try:
        worker.reconcile(log)
    except Exception as e:
        log(f"reconcile failed: {e}")
    State.worker = worker.Worker(log)
    State.worker.start()
    State.watcher = worker.Watcher(log)
    State.watcher.start()

    def shutdown(*_):
        log("shutting down")
        if State.watcher:
            State.watcher.stop()
        if State.worker:
            State.worker.stop_event.set()
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)
    httpd.serve_forever()
    log("stopped")
