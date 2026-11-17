"""PDF intake: files that appear in papers/.

A PDF uploaded through the web app already knows its paper. A PDF copied straight into papers/ does not, so:
  1. a paper whose pdf_path already names the file   -> that paper
  2. DOI on the first pages, or a confident title match to a row with no PDF
                                                      -> link it (pdf_path)
  3. no match                                         -> file a NEW paper row
     from OpenAlex metadata, and tell the user.
All writes to library.db go through the app's db.py.
"""

import re

from . import catalog, config, extract, openalex, orx, refs, store

_DOI = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>,;)]+)", re.I)


def _abbrev(name):
    parts = name.split()
    if len(parts) < 2:
        return name
    initials = " ".join(p[0] + "." for p in parts[:-1] if p[:1].isalpha())
    return f"{initials} {parts[-1]}"


def citation_from(w):
    """A citation string in the library's existing style:
    A. Author, B. Author, "Title," Venue, Year."""
    authors = [a.strip() for a in (w.get("authors") or "").split(",") if a.strip()]
    names = ", ".join(_abbrev(a) for a in authors[:8]) + (", et al." if len(authors) > 8 else "")
    bits = [names + ","] if names else []
    bits.append(f'"{w.get("title") or "Untitled"},"')
    if w.get("venue"):
        bits.append(w["venue"] + ",")
    if w.get("year"):
        bits.append(str(w["year"]) + ".")
    s = " ".join(bits)
    if w.get("doi"):
        s += f" doi:{w['doi']}"
    return s


def _title_windows(page):
    """Runs of 1-3 consecutive lines near the top of page 1: one of them is
    almost always the title, whatever junk (PII, journal banner) surrounds it."""
    lines = [re.sub(r"\s+", " ", l).strip() for l in page.splitlines()[:45]]
    lines = [l for l in lines if l]
    out = []
    for i in range(len(lines)):
        for n in (1, 2, 3):
            t = " ".join(lines[i:i + n])
            if 15 <= len(t) <= 260:
                out.append(t)
    return out


META_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "first_author", "year"],
    "properties": {"title": {"type": "string"}, "first_author": {"type": "string"},
                   "year": {"type": "string"}},
}


def _llm_title(page):
    from . import llm
    data, _ = llm.structured(
        "Give the title, first author and year of the paper whose first page "
        "is below. Use only what the page shows.\n\n" + page[:5000],
        META_SCHEMA, "You read the first page of an academic paper and extract its "
        "bibliographic metadata exactly as printed.", config.TRIAGE_MODEL, timeout=180)
    return data["title"].strip()


def identify(path, index=None):
    """Work out what paper a PDF is. Returns (library_paper_id, score, work, title):
    a library match if one is confident, else OpenAlex metadata if found."""
    pages = extract.pdf_pages(path)
    head = "\n".join(pages[:2]) if pages else ""
    index = index or refs.LibraryIndex()
    for m in _DOI.finditer(head):
        w = openalex.work(m.group(1).rstrip("."))
        if w and w["title"]:
            pid, s = index.match({"title": w["title"], "doi": w["doi"],
                                  "openalex_id": w["openalex_id"], "year": w["year"]})
            return pid, s, w, w["title"]
    # Library first: free, and exactly the case of an existing row with no PDF.
    best_pid, best_s, best_t = None, 0.0, ""
    for t in _title_windows(pages[0]) if pages else []:
        pid, s = index.match({"title": t})
        if pid and s > best_s:
            best_pid, best_s, best_t = pid, s, t
    if best_pid and best_s >= config.PDF_LINK_MIN:
        return best_pid, best_s, None, best_t
    title = extract.pdf_title(path)
    if re.match(r"^\s*(pii|doi)\b", title or "", re.I):
        title = ""
    if not title and pages:
        try:
            title = _llm_title(pages[0])
        except Exception:
            title = extract.first_page_title(pages)
    if title:
        pid, s = index.match({"title": title})
        if pid and s >= config.PDF_LINK_MIN:
            return pid, s, None, title
        try:
            for cand in orx.search(title, limit=5):
                if catalog.similarity(cand.get("title", ""), title) >= 0.85:
                    w = openalex.work(cand["id"])
                    if w:
                        pid, s = index.match({"title": w["title"], "doi": w["doi"],
                                              "openalex_id": w["openalex_id"],
                                              "year": w["year"]})
                        return pid, s, w, title
        except orx.OrxError:
            pass
    return None, 0.0, None, title


def _linked_paper(name):
    row = catalog.libdb().execute("SELECT id FROM papers WHERE pdf_path = ?", (name,)).fetchone()
    return row["id"] if row else None


def intake(name, log=print):
    """Handle one PDF that is in papers/. Returns (paper_id, action)."""
    path = config.PAPERS_DIR / name
    if not path.exists() or not extract.looks_like_pdf(path):
        return None, "not a pdf"
    sha = extract.sha256_file(path)
    c = store.conn()
    db = config.app_db()
    lib = catalog.libdb()

    pid = _linked_paper(name)
    if pid:
        store.record_pdf(c, sha, name, pid, "linked")
        return pid, "already linked"

    seen = store.pdf_seen(c, sha)
    if seen and seen["paper_id"]:
        return seen["paper_id"], "seen"

    pid, score, w, title = identify(path)
    if pid and score >= config.PDF_LINK_MIN:
        paper = db.get_paper(lib, pid)
        if paper["pdf_path"] and paper["pdf_path"] != name:
            store.record_pdf(c, sha, name, pid, "failed",
                             f"paper #{pid} already has {paper['pdf_path']}")
            store.add_event(c, "pdf_unmatched", f"{name} looks like #{pid} "
                            f"({paper['title'][:80]}), which already has a PDF; left unlinked.", pid)
            return pid, "duplicate"
        db.update_paper(lib, pid, {"pdf_path": name})
        store.record_pdf(c, sha, name, pid, "linked", f"title match {score:.2f}")
        store.add_event(c, "linked", f"Linked {name} to #{pid}: {paper['title'][:100]}", pid)
        log(f"  linked {name} -> #{pid} ({score:.2f})")
        return pid, "linked"

    # A PDF with no row becomes a new paper.
    if w:
        fields = {"citation": citation_from(w), "title": w["title"],
                  "authors": ", ".join(_abbrev(a.strip()) for a in w["authors"].split(",")
                                       if a.strip())[:500],
                  "year": w["year"], "venue": w["venue"]}
    else:
        stem = re.sub(r"[_\-]+", " ", path.stem)
        fields = {"citation": title or stem, "title": title or stem}
    paper = db.create_paper(lib, fields)
    db.update_paper(lib, paper["id"], {"pdf_path": name})
    if w:
        catalog._apply_work(c, paper["id"], w, 1.0)
    store.record_pdf(c, sha, name, paper["id"], "filed")
    how = "from OpenAlex metadata" if w else "from the PDF's own title (no OpenAlex match)"
    store.add_event(c, "filed", f"Filed new paper #{paper['id']} {how}: "
                    f"{fields['title'][:120]} ({name})", paper["id"])
    log(f"  filed {name} as new paper #{paper['id']}")
    return paper["id"], "filed"


def scan(enqueue):
    """Startup reconciliation over papers/: every PDF not yet handled gets a
    job. `enqueue(kind, paper_id, arg, priority)` is the queue's add function."""
    c = store.conn()
    lib = catalog.libdb()
    linked = {r["pdf_path"]: r["id"] for r in
              lib.execute("SELECT id, pdf_path FROM papers WHERE pdf_path <> ''")}
    n = 0
    for path in sorted(config.PAPERS_DIR.glob("*.pdf")):
        if path.name.startswith("."):
            continue
        if path.name in linked:
            continue
        sha = extract.sha256_file(path)
        seen = store.pdf_seen(c, sha)
        if seen and seen["state"] in ("linked", "filed", "failed"):
            continue
        enqueue("file", None, path.name, 1)
        n += 1
    return n
