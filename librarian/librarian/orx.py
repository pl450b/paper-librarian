"""Paper lookup, through the `orx` CLI (OpenResearch) when it is installed.

Used for two things:
  * search(title)       -> candidate papers (OpenAlex via `orx discover openalex`)
  * arxiv_fulltext(id)  -> full extracted text, reference list included

orx is OPTIONAL. Without it, search() uses OpenAlex's own title search and
arxiv_fulltext() downloads the arXiv PDF and extracts it with pdftotext.

Every orx call passes --no-telemetry. Only a title or an id leaves the machine.
Results are cached on disk so a re-run of the pipeline costs no lookups.
"""

import hashlib
import json
import os
import re
import shutil
import subprocess
import time
import urllib.request
from pathlib import Path

from . import config

ORX = shutil.which("orx") or str(Path.home() / ".cargo" / "bin" / "orx")
TIMEOUT = 120


class OrxError(RuntimeError):
    pass


class RateLimited(OrxError):
    """OpenAlex/alphaXiv said slow down; retry_s is its own hint."""
    def __init__(self, message, retry_s):
        super().__init__(message)
        self.retry_s = retry_s


def available():
    return Path(ORX).exists()


def _cache_path(kind, key):
    h = hashlib.sha256(key.encode("utf-8")).hexdigest()[:24]
    return config.ORX_CACHE / f"{kind}-{h}.json"


def _run_once(args):
    if not available():
        raise OrxError("orx CLI not found (expected ~/.cargo/bin/orx)")
    try:
        p = subprocess.run([ORX, "--no-telemetry", *args], capture_output=True,
                           text=True, timeout=TIMEOUT, cwd=str(config.WORK_DIR),
                           env={**os.environ})
    except subprocess.TimeoutExpired as e:
        raise OrxError(f"orx timed out: {' '.join(args[:2])}") from e
    if p.returncode != 0:
        msg = (p.stderr or p.stdout).strip()[:500] or f"orx exit {p.returncode}"
        if "429" in msg or "rate-limit" in msg.lower():
            m = re.search(r"retry in (\d+)\s*s", msg)
            raise RateLimited(msg, int(m.group(1)) if m else 60)
        raise OrxError(msg)
    return p.stdout


def _run(args, max_wait=90):
    """Run orx, sleeping through short rate-limit windows (up to max_wait s
    in total). Longer limits propagate so the queue can reschedule the job."""
    waited = 0
    while True:
        try:
            return _run_once(args)
        except RateLimited as e:
            if waited + e.retry_s > max_wait:
                raise
            time.sleep(e.retry_s + 1)
            waited += e.retry_s + 1


def search(query, limit=5, use_cache=True):
    """OpenAlex search through orx. Returns a list of dicts:
    {source, id, title, abstract, published, citations, ...}."""
    query = " ".join(query.split())[:300]
    if not available():
        from . import openalex
        return openalex.title_search(query, limit)
    path = _cache_path("search", f"{limit}:{query}")
    if use_cache and path.exists():
        return json.loads(path.read_text(encoding="utf-8"))
    out = _run(["discover", "openalex", query, "--limit", str(limit)])
    try:
        data = json.loads(out)
    except ValueError as e:
        raise OrxError(f"orx returned non-JSON: {out[:200]}") from e
    if isinstance(data, dict):
        data = data.get("results", [])
    path.write_text(json.dumps(data), encoding="utf-8")
    return data


def arxiv_fulltext(arxiv_id, use_cache=True):
    """Full text of an arXiv paper (alphaXiv extraction), or '' if unavailable."""
    path = config.TEXT_CACHE / f"arxiv-{arxiv_id.replace('/', '_')}.txt"
    if use_cache and path.exists():
        return path.read_text(encoding="utf-8")
    if not available():
        out = _arxiv_pdf_text(arxiv_id)
        if out:
            path.write_text(out, encoding="utf-8")
        return out
    try:
        out = _run(["paper", arxiv_id, "--full"])
    except OrxError:
        return ""
    lines = out.splitlines()
    if lines and lines[0].startswith("alphaXiv:"):
        out = "\n".join(lines[1:]).lstrip()
    if len(out) < 2000:        # an error page or a stub, not a paper
        return ""
    path.write_text(out, encoding="utf-8")
    return out


def _arxiv_pdf_text(arxiv_id):
    """Fallback without orx: fetch https://arxiv.org/pdf/<id> and extract it."""
    from . import extract
    url = f"https://arxiv.org/pdf/{arxiv_id}"
    req = urllib.request.Request(url, headers={"User-Agent": "paper-librarian/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(config.MAX_PDF_BYTES + 1)
    except Exception:
        return ""
    if not data.startswith(b"%PDF-") or len(data) > config.MAX_PDF_BYTES:
        return ""
    tmp = config.PDF_CACHE / f"arxiv-{arxiv_id.replace('/', '_')}.pdf"
    tmp.write_bytes(data)
    try:
        pages = extract.pdf_pages(tmp)
    finally:
        tmp.unlink(missing_ok=True)
    text = "\n\n".join(pages)
    return text if len(text) >= 2000 else ""
