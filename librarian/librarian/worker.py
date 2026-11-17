"""The job queue's worker, the papers/ watcher, and startup reconciliation.
One worker thread runs one job at a time.
"""

import datetime
import shutil
import subprocess
import threading
import time
import traceback

from . import catalog, config, context, files, llm, openalex, orx, store

MAX_ATTEMPTS = 3
PRIORITY_USER, PRIORITY_NEW, PRIORITY_RETRIAGE, PRIORITY_BACKFILL = 0, 1, 8, 9


def enqueue(kind, paper_id=None, arg="", priority=5, delay_s=0):
    return store.enqueue(store.conn(), kind, paper_id, arg, priority, delay_s)


# ------------------------------------------------------------- handlers

def _h_file(job, log):
    pid, action = files.intake(job["arg"], log=log)
    if pid and action in ("linked", "filed"):
        enqueue("catalog", pid, "force", PRIORITY_USER)


def _h_catalog(job, log):
    rep = catalog.catalog(job["paper_id"], force=job["arg"] == "force", log=log)
    if rep.get("skipped"):
        return
    c = store.conn()
    st = store.get_state(c, job["paper_id"])
    req = c.execute("SELECT state FROM requests WHERE paper_id = ?",
                    (job["paper_id"],)).fetchone()
    if req and st.get("tier") == "deep" and req["state"] != "declined":
        # The wanted PDF arrived (upload, attach, or a drop into papers/): close it.
        store.set_request(c, job["paper_id"], state="received")
        headline = (st.get("summary") or "").split(". ")[0][:300]
        store.add_event(c, "received", f"Read #{job['paper_id']} "
                        f"({(st.get('oa_title') or '')[:100]}): {headline}", job["paper_id"])
    if st.get("contradicts", "").strip():
        store.add_event(c, "contradiction",
                        f"#{job['paper_id']} may contradict the project: {st['contradicts'][:300]}",
                        job["paper_id"])
    pri = min(job["priority"] + 1, PRIORITY_BACKFILL)
    enqueue("refs", job["paper_id"], "", pri)
    enqueue("embed", job["paper_id"], "", pri)
    enqueue("digest", None, "", PRIORITY_BACKFILL, delay_s=60)


def _h_refs(job, log):
    from . import refs
    rep = refs.build_refs(job["paper_id"])
    log(f"  refs   #{job['paper_id']}: {rep}")
    enqueue("ghosts", None, "", PRIORITY_BACKFILL, delay_s=120)


def _h_embed(job, log):
    from . import embed
    n = embed.embed_paper(job["paper_id"], force=job["arg"] == "force")
    log(f"  embed  #{job['paper_id']}: {n} chunks")


def _h_ghosts(job, log):
    from . import embed, refs
    refs.recount_ghosts()
    refs.resolve_ghosts(min_count=2, limit=40, log=log)
    refs.triage_ghosts(limit=15, log=log)
    for pid in refs.auto_add_ghosts(log=log):
        enqueue("catalog", pid, "", PRIORITY_NEW)
    embed.embed_ghosts()
    enqueue("digest", None, "", PRIORITY_BACKFILL, delay_s=30)


def _h_digest(job, log):
    from . import digest
    digest.write()


HANDLERS = {"file": _h_file, "catalog": _h_catalog, "refs": _h_refs, "embed": _h_embed,
            "ghosts": _h_ghosts, "digest": _h_digest}


# --------------------------------------------------------------- worker

class Worker(threading.Thread):
    def __init__(self, log=print):
        super().__init__(daemon=True, name="librarian-worker")
        self.log = log
        self.stop_event = threading.Event()
        self.current = None

    def paused(self):
        c = store.conn()
        if store.get_meta(c, "paused") == "1":
            return "paused by you"
        if context.is_template():
            return "waiting for librarian/brief.md to be filled in"
        until = store.get_meta(c, "paused_until")
        if until and float(until) > time.time():
            reason = store.get_meta(c, "paused_reason") or "usage limit"
            return f"{reason}: resumes " + datetime.datetime.fromtimestamp(
                float(until)).strftime("%a %H:%M")
        return ""

    def run(self):
        store.reset_running(store.conn())
        while not self.stop_event.is_set():
            if self.paused():
                self.stop_event.wait(15)
                continue
            job = store.next_job(store.conn())
            if not job:
                self.stop_event.wait(3)
                continue
            self.current = job
            try:
                HANDLERS[job["kind"]](job, self.log)
                store.finish_job(store.conn(), job["id"])
            except llm.UsageLimit as e:
                c = store.conn()
                store.set_meta(c, "paused_until", str(e.resume_at))
                store.set_meta(c, "paused_reason", "Claude usage limit")
                store.retry_job(c, job["id"], max(60, e.resume_at - time.time()), str(e))
                self.log(f"  usage limit hit; pausing queue ({e})")
            except (orx.RateLimited, openalex.RateLimited) as e:
                # Not a failure: retry without spending an attempt. A long wait
                # (a spent daily budget) pauses the whole queue, since nearly
                # every job needs OpenAlex; a short one just delays this job.
                c = store.conn()
                c.execute("UPDATE jobs SET attempts = MAX(attempts - 1, 0) WHERE id = ?",
                          (job["id"],))
                store.retry_job(c, job["id"], e.retry_s + 5, str(e))
                if e.retry_s > 300:
                    store.set_meta(c, "paused_until", str(time.time() + e.retry_s + 5))
                    store.set_meta(c, "paused_reason", "OpenAlex daily limit")
                    self.log(f"  {e}; pausing the queue until it resets")
            except Exception as e:
                c = store.conn()
                msg = f"{type(e).__name__}: {e}"
                if job["attempts"] < MAX_ATTEMPTS:
                    store.retry_job(c, job["id"], 60 * 2 ** job["attempts"], msg)
                else:
                    store.finish_job(c, job["id"], msg + "\n" + traceback.format_exc()[-1500:])
                    store.add_event(c, "error", f"{job['kind']} job for "
                                    f"#{job['paper_id'] or job['arg']} failed: {msg[:300]}",
                                    job["paper_id"])
                self.log(f"  job {job['kind']} #{job['paper_id']} failed: {msg}")
            finally:
                self.current = None


# -------------------------------------------------------------- watcher

class Watcher(threading.Thread):
    """inotifywait on papers/ (close_write, moved_to) plus a periodic rescan,
    which catches anything inotify missed."""

    def __init__(self, log=print, rescan_s=600):
        super().__init__(daemon=True, name="librarian-watcher")
        self.log, self.rescan_s = log, rescan_s
        self.proc = None
        self.stop_event = threading.Event()

    def run(self):
        threading.Thread(target=self._rescan_loop, daemon=True).start()
        exe = shutil.which("inotifywait")
        if not exe:
            self.log("  inotifywait not found; relying on periodic rescans")
            return
        self.proc = subprocess.Popen(
            [exe, "-m", "-q", "-e", "close_write", "-e", "moved_to", "--format", "%f",
             str(config.PAPERS_DIR)], stdout=subprocess.PIPE, text=True)
        for line in self.proc.stdout:
            name = line.strip()
            if name.lower().endswith(".pdf") and not name.startswith("."):
                self.log(f"  new file in papers/: {name}")
                # 30 s: an upload through the web app is linked by its PATCH
                # first, and intake then sees "already linked".
                enqueue("file", None, name, PRIORITY_USER, delay_s=30)

    def _rescan_loop(self):
        while not self.stop_event.wait(self.rescan_s):
            try:
                reconcile(self.log, quiet=True)
            except Exception as e:
                self.log(f"  rescan failed: {e}")

    def stop(self):
        self.stop_event.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()


# ---------------------------------------------------------- reconcile

def reconcile(log=print, quiet=False):
    """Catch up on everything that changed while the app was off.

    Does NOT queue the full backfill: papers that predate the Librarian are
    only queued after `librarian backfill` sets the 'backfill' flag.
    """
    c = store.conn()
    lib = catalog.libdb()
    if not store.get_meta(c, "installed_at"):
        store.set_meta(c, "installed_at", lib.execute(
            "SELECT datetime('now')").fetchone()[0])
    installed = store.get_meta(c, "installed_at")
    backfill = store.get_meta(c, "backfill") == "1"
    n_files = files.scan(lambda k, p, a, pr: enqueue(k, p, a, pr))

    states = store.all_states(c)
    n_new = n_back = n_retriage = n_embed = 0
    ctx = context.digest()
    for p in lib.execute("SELECT id, created_at, updated_at FROM papers"):
        st = states.get(p["id"])
        if not st or not st["cataloged_at"]:
            if p["created_at"] >= installed:
                n_new += enqueue("catalog", p["id"], "", PRIORITY_NEW)
            elif backfill:
                n_back += enqueue("catalog", p["id"], "", PRIORITY_BACKFILL)
            continue
        if st["context_hash"] != ctx and (st["relevance"] or 0) <= 1:
            n_retriage += enqueue("catalog", p["id"], "force", PRIORITY_RETRIAGE)
        elif p["updated_at"] > (st["updated_at"] or ""):
            # Notes/title edited since the last embed.
            n_embed += enqueue("embed", p["id"], "", PRIORITY_RETRIAGE)
    if not quiet or n_files or n_new or n_retriage:
        log(f"  reconcile: {n_files} new PDFs, {n_new} new papers, {n_back} backfill, "
            f"{n_retriage} re-triage (brief changed), {n_embed} re-embed")
    return {"files": n_files, "new": n_new, "backfill": n_back,
            "retriage": n_retriage, "embed": n_embed}
