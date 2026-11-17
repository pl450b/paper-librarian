"""Read access to the user's LaTeX draft (e.g. main.tex), for the chat agent.

The brief (brief.md) is the author's hand-written digest; this module is the
draft as it stands on disk. It is re-read whenever the .tex/.bib files change,
never snapshotted, and never written. Optional: with no draft configured, the
draft tools just say so.

Reuses the app's stdlib thesis.py for comment stripping, \\input resolution and
.bib parsing, so the draft means the same thing here as in the Thesis tab.
"""

import re
import threading
from pathlib import Path

from . import catalog, config, store

LEVELS = ("chapter", "section", "subsection", "subsubsection")
HEADING = re.compile(r"\\(chapter|section|subsection|subsubsection)(\*?)\s*(?:\[[^\]]*\])?\s*(?=\{)")
CITE = re.compile(r"\\(?:[a-zA-Z]*cite[a-zA-Z]*)\*?\s*(?:\[[^\]]*\]\s*){0,2}\{([^}]*)\}")
TODO = re.compile(r"\\(todo|note|cne)\b")
SECTION_CAP = 14000

_lock = threading.Lock()
_cache = {"key": None, "doc": None}
_vec_cache = {}          # paragraph text -> unit vector


def _tex():
    config.app_db()                       # puts the app dir on sys.path
    import thesis as tex                  # the app's module
    return tex


def locate():
    """The draft's path: the app's setting, then the app's autodetect, then a
    main.tex in a sibling folder of the repo."""
    tex, db = _tex(), config.app_db()
    c = db.connect()
    try:
        path = db.get_setting(c, "thesis_tex", "")
    finally:
        c.close()
    if path and Path(path).is_file():
        return Path(path)
    found = tex.autodetect(config.APP_DIR)
    if found:
        return found
    for cand in sorted(config.APP_DIR.parent.glob("*/main.tex")):
        if re.search(r"\\begin\s*\{document\}", cand.read_text(errors="replace")):
            return cand
    return None


def _files_key(doc):
    files = doc.sources() + doc.bib_paths()
    return tuple((str(p), p.stat().st_mtime) for p in files if p.is_file())


def load():
    """Parsed draft, cached on the files' mtimes. None if there is no draft."""
    path = locate()
    if not path:
        return None
    tex = _tex()
    doc = tex.Thesis(str(path))
    key = (str(path), _files_key(doc))
    with _lock:
        if _cache["key"] == key:
            return _cache["doc"]
    raw = tex.resolve_inputs(doc, tex.strip_comments(tex._read(doc.tex)))
    m = re.search(r"\\begin\s*\{document\}", raw)
    end = re.search(r"\\end\s*\{document\}", raw)
    body = raw[m.end():end.start() if end else len(raw)] if m else raw
    bib, _dupes = tex.parse_bib(doc.bib_paths(raw))
    parsed = {"path": str(path), "sections": _sections(tex, body), "bib": bib,
              "mtime": max((t for _, t in key[1]), default=0)}
    with _lock:
        _cache["key"], _cache["doc"] = key, parsed
    return parsed


def _sections(tex, body):
    heads = []
    for m in HEADING.finditer(body):
        title, end = tex.read_group(body, m.end())
        heads.append((m.start(), end, LEVELS.index(m.group(1)), m.group(2) == "*",
                      re.sub(r"\s+", " ", re.sub(r"\\(?=\s)", "", title)).strip()))
    top = min((h[2] for h in heads), default=0)
    counters, out = [0] * len(LEVELS), []
    if heads and body[:heads[0][0]].strip():
        out.append({"number": "0", "title": "(front matter)", "level": top,
                    "text": body[:heads[0][0]].strip()})
    for i, (start, end, level, starred, title) in enumerate(heads):
        stop = heads[i + 1][0] if i + 1 < len(heads) else len(body)
        if not starred:
            counters[level] += 1
            for j in range(level + 1, len(LEVELS)):
                counters[j] = 0
        number = ".".join(str(n) for n in counters[top:level + 1]) if not starred else ""
        out.append({"number": number, "title": title, "level": level - top,
                    "text": body[end:stop].strip()})
    for s in out:
        s["words"] = len(re.findall(r"[A-Za-z]{2,}", re.sub(r"\\[A-Za-z]+", " ", s["text"])))
        s["todos"] = len(TODO.findall(s["text"]))
        s["cites"] = sorted({k.strip() for g in CITE.findall(s["text"])
                             for k in g.split(",") if k.strip()})
    return out


def _label(s):
    return f"{s['number']} {s['title']}".strip()


# ------------------------------------------------------------ bib -> library

def bib_links(d):
    """bibkey -> library paper id, read-only. The user's manual links and
    ignores from the Thesis tab win; then DOI; then title similarity >= 0.90."""
    db = config.app_db()
    c = db.connect()
    try:
        manual = db.get_bib_links(c)
        papers = c.execute("SELECT id, title FROM papers").fetchall()
    finally:
        c.close()
    lc = store.conn()
    dois = {r["doi"].lower(): r["paper_id"] for r in lc.execute(
        "SELECT paper_id, doi FROM paper_state WHERE doi IS NOT NULL AND doi != ''")}
    exact = {catalog.norm(r["title"]): r["id"] for r in papers if r["title"]}
    links = {}
    for key, entry in d["bib"].items():
        m = manual.get(key)
        if m and (m["manual"] or m["ignored"]):
            if m["paper_id"] and not m["ignored"]:
                links[key] = m["paper_id"]
            continue
        doi = (entry.get("doi") or "").lower().removeprefix("https://doi.org/")
        title = entry.get("title") or ""
        pid = dois.get(doi) if doi else None
        pid = pid or exact.get(catalog.norm(title))
        if not pid and title:
            best = max(((catalog.similarity(title, r["title"] or ""), r["id"])
                        for r in papers), default=(0, None))
            pid = best[1] if best[0] >= config.REF_MATCH_MIN else None
        if pid:
            links[key] = pid
    return links


# ------------------------------------------------------------ tools

def _missing():
    return {"error": "no LaTeX draft configured (set one in the app's Draft tab)"}


def outline():
    d = load()
    if not d:
        return _missing()
    return {"path": d["path"], "sections": [
        {"number": s["number"], "title": s["title"], "level": s["level"],
         "words": s["words"], "todos": s["todos"], "cites": len(s["cites"])}
        for s in d["sections"]],
        "total_words": sum(s["words"] for s in d["sections"])}


def _find(d, ref):
    ref = str(ref).strip()
    secs = d["sections"]
    hit = [s for s in secs if s["number"] == ref.rstrip(".")]
    if not hit:
        low = ref.lower()
        hit = ([s for s in secs if s["title"].lower() == low]
               or [s for s in secs if low in s["title"].lower()])
    return hit[0] if hit else None


def section(ref, include_subsections=True, offset=0):
    """LaTeX source of one section (comments stripped), by number or title."""
    d = load()
    if not d:
        return _missing()
    s = _find(d, ref)
    if not s:
        return {"error": f"no section matching {ref!r}",
                "sections": [_label(x) for x in d["sections"]]}
    parts = [s]
    if include_subsections:
        i = d["sections"].index(s)
        for x in d["sections"][i + 1:]:
            if x["level"] <= s["level"]:
                break
            parts.append(x)
    text = "\n\n".join((f"[[{_label(x)}]]\n" if x is not s else "") + x["text"] for x in parts)
    chunk = text[offset:offset + SECTION_CAP]
    out = {"section": _label(s), "text": chunk,
           "subsections": [_label(x) for x in parts[1:]]}
    if offset + SECTION_CAP < len(text):
        out["truncated"] = True
        out["next_offset"] = offset + SECTION_CAP
    return out


def _paragraphs(d):
    for s in d["sections"]:
        buf = ""
        for para in re.split(r"\n\s*\n", s["text"]):
            para = para.strip()
            if not para:
                continue
            buf = f"{buf}\n\n{para}" if buf else para
            if len(buf) >= 250:
                yield s, buf[:2000]
                buf = ""
        if buf:
            yield s, buf[:2000]


def _unit(vectors):
    import numpy as np
    v = np.asarray(list(vectors), dtype="float32")
    return v / (np.linalg.norm(v, axis=1, keepdims=True) + 1e-9)


def search(query, k=6):
    """Passages of the draft closest to the query: semantic (local embeddings)
    plus a keyword bonus, so exact terms like 'BRAM' still rank."""
    d = load()
    if not d:
        return _missing()
    from . import embed
    paras = list(_paragraphs(d))
    if not paras:
        return {"results": []}
    todo = [p for _, p in paras if p not in _vec_cache]
    if todo:
        for p, v in zip(todo, _unit(embed.model().embed(todo, batch_size=32))):
            _vec_cache[p] = v
    import numpy as np
    q = _unit(embed.model().query_embed([query]))[0]
    terms = [t.lower() for t in re.findall(r"[A-Za-z0-9\-]{3,}", query)]
    scored = []
    for s, p in paras:
        low = p.lower()
        bonus = 0.04 * sum(t in low for t in terms) / max(len(terms), 1)
        scored.append((float(np.dot(_vec_cache[p], q)) + bonus, s, p))
    scored.sort(key=lambda x: -x[0])
    return {"results": [{"section": _label(s), "score": round(sc, 3), "text": p}
                        for sc, s, p in scored[:k]]}


def cites(paper_id=None):
    """What the draft cites. With paper_id: whether and where that paper is
    cited. Without: every cited key, its library paper, and where it is used."""
    d = load()
    if not d:
        return _missing()
    links = bib_links(d)
    where = {}
    for s in d["sections"]:
        for key in s["cites"]:
            where.setdefault(key, []).append(_label(s))
    if paper_id is not None:
        keys = [k for k, pid in links.items() if pid == int(paper_id)]
        return {"paper_id": int(paper_id), "bib_keys": keys,
                "in_bib": bool(keys),
                "cited_in": sorted({w for k in keys for w in where.get(k, [])})}
    rows = []
    for key, secs in where.items():
        entry = d["bib"].get(key) or {}
        rows.append({"key": key, "paper_id": links.get(key),
                     "title": entry.get("title", "") or "(not in .bib)",
                     "sections": secs})
    unused = [k for k in d["bib"] if k not in where]
    return {"cited": sorted(rows, key=lambda r: r["key"]),
            "not_in_library": [r["key"] for r in rows if not r["paper_id"]],
            "bib_entries_never_cited": unused}
