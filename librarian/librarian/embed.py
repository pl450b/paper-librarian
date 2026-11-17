"""Semantic memory: local embeddings in sqlite-vec, and a
hybrid search that fuses them with the library's own FTS5 index.

Model: BAAI/bge-small-en-v1.5 (384-d) through fastembed (ONNX, CPU). It is
loaded once per process and kept; the daemon owns it, and other processes
(the MCP server) ask the daemon to search rather than loading their own.
"""

import hashlib
import re
import threading

from . import catalog, config, extract, store

MODEL_NAME = "BAAI/bge-small-en-v1.5"
DIM = 384
QUERY_PREFIX = "Represent this sentence for searching relevant passages: "
BODY_CHARS, BODY_OVERLAP = 1600, 200

_model = None
_model_lock = threading.Lock()


def model():
    global _model
    with _model_lock:
        if _model is None:
            from fastembed import TextEmbedding
            _model = TextEmbedding(MODEL_NAME, cache_dir=str(config.MODEL_CACHE))
    return _model


def ensure_vec_table(c):
    c.execute(f"CREATE VIRTUAL TABLE IF NOT EXISTS vec_chunks USING vec0("
              f"embedding float[{DIM}] distance_metric=cosine)")


def _vectors(texts):
    import sqlite_vec
    return [sqlite_vec.serialize_float32(list(map(float, v)))
            for v in model().embed(texts, batch_size=32)]


# ------------------------------------------------------------ units

def _note_sections(note_path):
    path = config.LIB_DIR / note_path if note_path else None
    if not path or not path.exists():
        return []
    text = path.read_text(encoding="utf-8")
    text = text.split("---", 2)[-1] if text.startswith("---") else text
    out = []
    for block in re.split(r"\n(?=## )", text):
        block = block.strip()
        if len(block) > 40:
            out.append(block[:3000])
    return out


def _body_chunks(text):
    """~1600-char chunks with overlap, each tagged with the page it starts on."""
    body, _ = extract.split_references(text)
    out, page = [], ""
    pos = 0
    while pos < len(body):
        chunk = body[pos:pos + BODY_CHARS]
        marks = re.findall(r"\[\[page (\d+)\]\]", body[:pos + 1])
        page = marks[-1] if marks else ""
        clean = re.sub(r"\[\[page \d+\]\]", " ", chunk)
        clean = re.sub(r"\s+", " ", clean).strip()
        if len(clean) > 200:
            out.append((page, clean))
        pos += BODY_CHARS - BODY_OVERLAP
    return out


def units_for_paper(paper_id):
    paper = catalog.get_paper(paper_id)
    st = store.get_state(store.conn(), paper_id) or {}
    if not paper:
        return []
    title = st.get("oa_title") or paper["title"]
    units = []
    if st.get("summary"):
        units.append(("summary", "", f"{title}. {st['summary']} {st.get('why', '')}"))
    elif title:
        units.append(("summary", "", f"{title}. {paper['citation'][:400]}"))
    if st.get("abstract"):
        units.append(("abstract", "", f"{title}. {st['abstract'][:3000]}"))
    for sec in _note_sections(st.get("note_path")):
        units.append(("note", "", f"{title}. {sec}"))
    if paper["notes"].strip():
        for i in range(0, min(len(paper["notes"]), 12000), 2000):
            units.append(("user_note", "", f"{title}. {paper['notes'][i:i + 2000]}"))
    if st.get("tier") == "deep":
        text = extract.cached_text(paper_id)
        for page, chunk in _body_chunks(text)[:200]:
            units.append(("body", page, chunk))
    return units


def embed_paper(paper_id, force=False):
    c = store.conn(vec=True)
    ensure_vec_table(c)
    units = units_for_paper(paper_id)
    h = hashlib.sha256("\x00".join(f"{k}|{p}|{t}" for k, p, t in units)
                       .encode("utf-8")).hexdigest()[:16]
    st = store.get_state(c, paper_id) or {}
    if not force and st.get("embedded_hash") == h:
        return 0
    old = [r[0] for r in c.execute("SELECT id FROM chunks WHERE paper_id = ?", (paper_id,))]
    for cid in old:
        c.execute("DELETE FROM vec_chunks WHERE rowid = ?", (cid,))
    c.execute("DELETE FROM chunks WHERE paper_id = ?", (paper_id,))
    vecs = _vectors([t for _, _, t in units]) if units else []
    for (kind, page, text), v in zip(units, vecs):
        cur = c.execute("INSERT INTO chunks (paper_id, kind, page, text) VALUES (?,?,?,?)",
                        (paper_id, kind, page, text))
        c.execute("INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)", (cur.lastrowid, v))
    c.commit()
    store.update_state(c, paper_id, embedded_hash=h)
    return len(units)


def embed_ghosts():
    """Embed ghosts that have an abstract and no chunk yet."""
    c = store.conn(vec=True)
    ensure_vec_table(c)
    rows = c.execute("SELECT id, title, abstract, summary FROM ghosts WHERE abstract <> ''"
                     " AND id NOT IN (SELECT ghost_id FROM chunks WHERE ghost_id IS NOT NULL)"
                     ).fetchall()
    if not rows:
        return 0
    texts = [f"{r['title']}. {r['summary'] or ''} {r['abstract'][:3000]}" for r in rows]
    for r, text, v in zip(rows, texts, _vectors(texts)):
        cur = c.execute("INSERT INTO chunks (ghost_id, kind, text) VALUES (?, 'ghost', ?)",
                        (r["id"], text))
        c.execute("INSERT INTO vec_chunks (rowid, embedding) VALUES (?, ?)", (cur.lastrowid, v))
    c.commit()
    return len(rows)


# ------------------------------------------------------------ search

def search(query, k=10, kinds=None, min_relevance=None, include_ghosts=False):
    """Hybrid search. Returns ranked dicts:
    {paper_id|ghost_id, title, score, kind, page, snippet, relevance, tier}."""
    c = store.conn(vec=True)
    ensure_vec_table(c)
    qv = _vectors([QUERY_PREFIX + query])[0]
    hits = c.execute(
        "SELECT v.rowid AS id, v.distance, ch.paper_id, ch.ghost_id, ch.kind, ch.page, ch.text"
        " FROM vec_chunks v JOIN chunks ch ON ch.id = v.rowid"
        " WHERE v.embedding MATCH ? AND k = ?", (qv, 200)).fetchall()

    # Best chunk per paper, then reciprocal-rank fusion with FTS5.
    best, order = {}, []
    for h in sorted(hits, key=lambda r: r["distance"]):
        if kinds and h["kind"] not in kinds:
            continue
        key = ("p", h["paper_id"]) if h["paper_id"] is not None else ("g", h["ghost_id"])
        if key[0] == "g" and not include_ghosts:
            continue
        if key not in best:
            best[key] = h
            order.append(key)
    scores = {key: 1.0 / (60 + i) for i, key in enumerate(order)}
    db = config.app_db()
    for i, pid in enumerate(db.fts_rank_any(catalog.libdb(), query, 50)):
        scores[("p", pid)] = scores.get(("p", pid), 0) + 1.0 / (60 + i)

    states = {r["paper_id"]: r for r in c.execute(
        "SELECT paper_id, relevance, tier, oa_title, summary FROM paper_state")}
    out = []
    for key, score in sorted(scores.items(), key=lambda kv: -kv[1]):
        if key[0] == "p":
            st = states.get(key[1])
            rel = st["relevance"] if st else None
            if min_relevance is not None and (rel is None or rel < min_relevance):
                continue
            paper = catalog.get_paper(key[1])
            if not paper:
                continue
            h = best.get(key)
            out.append({"paper_id": key[1],
                        "title": (st["oa_title"] if st and st["oa_title"] else paper["title"]),
                        "year": paper["year"], "score": round(score, 5),
                        "relevance": rel, "tier": st["tier"] if st else "",
                        "kind": h["kind"] if h else "fts", "page": h["page"] if h else "",
                        "snippet": (h["text"][:500] if h else
                                    ((st["summary"] if st else "") or paper["citation"])[:500])})
        else:
            g = c.execute("SELECT * FROM ghosts WHERE id = ?", (key[1],)).fetchone()
            if not g:
                continue
            out.append({"ghost_id": key[1], "title": g["title"], "year": g["year"],
                        "score": round(score, 5), "relevance": g["relevance"],
                        "tier": "ghost", "cite_count": g["cite_count"], "kind": "ghost",
                        "page": "", "snippet": best[key]["text"][:500]})
        if len(out) >= k:
            break
    return out
