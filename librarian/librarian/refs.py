"""Citations: who in the library cites whom, and which
papers outside it ("ghosts") the library keeps citing.

Reference sources, in priority order:
  1. OpenAlex `referenced_works` (structured, no parsing; covers most papers)
  2. references parsed from the paper's own text (local PDF / OA PDF / arXiv)
Matching favours precision: a doubtful match is recorded with its score but
not counted as a citation of a library paper.
"""

import difflib
import re
from collections import defaultdict

from . import catalog, config, extract, llm, openalex, orx, store

STOP = set("a an the of for and in on with to from by via using based towards toward "
           "is are its their our we under over into at as or vs versus".split())


def words(title):
    return [w for w in catalog.norm(title).split() if w not in STOP and len(w) > 2]


class LibraryIndex:
    """Lookup structures over the library's papers, built once per run."""

    def __init__(self):
        lib = catalog.libdb()
        c = store.conn()
        states = {r["paper_id"]: r for r in c.execute(
            "SELECT paper_id, doi, openalex_id, oa_title, oa_year FROM paper_state"
            " WHERE resolve_state = 'resolved'")}
        self.by_doi, self.by_oa, self.titles = {}, {}, {}
        self.inverted = defaultdict(set)
        for p in lib.execute("SELECT id, title, year FROM papers"):
            st = states.get(p["id"])
            if st:
                if st["doi"]:
                    self.by_doi[st["doi"]] = p["id"]
                if st["openalex_id"]:
                    self.by_oa[st["openalex_id"]] = p["id"]
            for t in {p["title"], st["oa_title"] if st else ""}:
                if t:
                    self._add_title(p["id"], t, (st["oa_year"] if st else "") or p["year"])

    def _add_title(self, pid, title, year):
        key = (pid, catalog.norm(title))
        self.titles[key] = (year or "")[:4]
        for w in set(words(title)):
            self.inverted[w].add(key)

    def match(self, ref):
        """(paper_id, score) or (None, best_score)."""
        if ref.get("doi") and ref["doi"] in self.by_doi:
            return self.by_doi[ref["doi"]], 1.0
        if ref.get("openalex_id") and ref["openalex_id"] in self.by_oa:
            return self.by_oa[ref["openalex_id"]], 1.0
        t = catalog.norm(ref.get("title", ""))
        ws = set(words(ref.get("title", "")))
        if len(ws) < 2:
            return None, 0.0
        counts = defaultdict(int)
        for w in ws:
            for key in self.inverted.get(w, ()):
                counts[key] += 1
        need = 2 if len(ws) <= 4 else 3
        best, best_s = None, 0.0
        for key, n in counts.items():
            if n < need:
                continue
            s = difflib.SequenceMatcher(None, t, key[1]).ratio()
            y1, y2 = (ref.get("year") or "")[:4], self.titles[key]
            if y1.isdigit() and y2.isdigit() and abs(int(y1) - int(y2)) > 1:
                s -= 0.1
            if s > best_s:
                best, best_s = key[0], s
        if best_s >= config.REF_MATCH_MIN:
            return best, best_s
        return None, best_s


# ----------------------------------------------------------- gathering

def _openalex_refs(st):
    if not st.get("openalex_id"):
        return []
    w = openalex.work(st["openalex_id"])
    if not w or not w["referenced_works"]:
        return []
    meta = openalex.works_by_ids(w["referenced_works"])
    out = []
    for i, oa_id in enumerate(w["referenced_works"]):
        m = meta.get(oa_id, {})
        out.append({"ord": i, "source": "openalex", "openalex_id": oa_id,
                    "doi": m.get("doi", ""), "title": m.get("title", ""),
                    "year": m.get("year", ""), "authors": m.get("authors", ""),
                    "venue": m.get("venue", ""), "raw": ""})
    return out


REFS_SCHEMA = {
    "type": "object", "additionalProperties": False, "required": ["refs"],
    "properties": {"refs": {"type": "array", "items": {
        "type": "object", "additionalProperties": False,
        "required": ["title", "year", "doi", "arxiv"],
        "properties": {"title": {"type": "string"}, "year": {"type": "string"},
                       "doi": {"type": "string"}, "arxiv": {"type": "string"}}}}},
}


def _parsed_refs(paper_id):
    text = extract.cached_text(paper_id)
    if not text:
        return []
    _, section = extract.split_references(text)
    if not section.strip():
        return []
    entries = extract.parse_references(section)
    out = []
    if len(entries) >= 5:
        for i, raw in enumerate(entries):
            f = extract.ref_fields(raw)
            out.append({"ord": i, "source": "parsed", "raw": raw[:1200],
                        "title": f["title"], "year": f["year"], "doi": f["doi"],
                        "openalex_id": "", "arxiv": f["arxiv"]})
        return out
    # Unrecognised layout: let Haiku split it, in slices of ~12k characters.
    system = ("You extract bibliographic references from messy PDF text. "
              "Output only what the text supports.")
    template = (config.PROMPTS_DIR / "refs_cleanup.md").read_text(encoding="utf-8")
    for start in range(0, min(len(section), 60_000), 12_000):
        chunk = section[start:start + 12_000]
        data, _ = llm.structured(template.replace("{section}", chunk), REFS_SCHEMA,
                                 system, config.TRIAGE_MODEL)
        for r in data["refs"]:
            out.append({"ord": len(out), "source": "parsed", "raw": "",
                        "title": r["title"], "year": r["year"],
                        "doi": openalex.bare_doi(r["doi"]), "openalex_id": "",
                        "arxiv": r["arxiv"]})
    return out


def ghost_key(ref):
    if ref.get("doi"):
        return "doi:" + ref["doi"]
    if ref.get("openalex_id"):
        return "oa:" + ref["openalex_id"]
    t = catalog.norm(ref.get("title", ""))
    return ("t:" + t) if len(t) > 15 else ""


def build_refs(paper_id, index=None):
    """(Re)build one paper's outgoing references. Returns counts."""
    c = store.conn()
    st = store.get_state(c, paper_id) or {}
    index = index or LibraryIndex()
    oa_refs = _openalex_refs(st)
    parsed = _parsed_refs(paper_id)
    # Parsed entries only add what OpenAlex is missing.
    if oa_refs:
        known = {catalog.norm(r["title"]) for r in oa_refs if r["title"]}
        known_doi = {r["doi"] for r in oa_refs if r["doi"]}
        parsed = [r for r in parsed if r["doi"] not in known_doi and not any(
            difflib.SequenceMatcher(None, catalog.norm(r["title"]), k).ratio() > 0.9
            for k in known if abs(len(k) - len(catalog.norm(r["title"]))) < 20)]
    c.execute("DELETE FROM refs WHERE citing_id = ?", (paper_id,))
    matched = ghosts = 0
    for ref in oa_refs + parsed:
        pid, score = index.match(ref)
        if pid == paper_id:
            continue
        gid = None
        if pid is None:
            key = ghost_key(ref)
            if key:
                c.execute("INSERT INTO ghosts (key, title, year, doi, openalex_id, authors, venue)"
                          " VALUES (?,?,?,?,?,?,?) ON CONFLICT(key) DO UPDATE SET"
                          " title = CASE WHEN ghosts.title = '' THEN excluded.title ELSE ghosts.title END,"
                          " doi = CASE WHEN ghosts.doi = '' THEN excluded.doi ELSE ghosts.doi END",
                          (key, ref["title"][:300], ref["year"], ref["doi"],
                           ref["openalex_id"], ref.get("authors", "")[:300],
                           ref.get("venue", "")[:200]))
                gid = c.execute("SELECT id FROM ghosts WHERE key = ?", (key,)).fetchone()[0]
                ghosts += 1
        else:
            matched += 1
        c.execute("INSERT OR REPLACE INTO refs (citing_id, ord, source, raw, title, title_norm,"
                  " year, doi, openalex_id, matched_paper_id, match_score, ghost_id)"
                  " VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                  (paper_id, ref["ord"], ref["source"], ref.get("raw", ""), ref["title"][:300],
                   catalog.norm(ref["title"]), ref["year"], ref["doi"], ref["openalex_id"],
                   pid, round(score, 3), gid))
    c.commit()
    store.update_state(c, paper_id, refs_state="done" if (oa_refs or parsed) else "none")
    recount_ghosts()
    return {"openalex": len(oa_refs), "parsed": len(parsed),
            "in_library": matched, "ghost": ghosts}


def recount_ghosts():
    c = store.conn()
    c.execute("UPDATE ghosts SET cite_count = (SELECT COUNT(DISTINCT citing_id)"
              " FROM refs WHERE refs.ghost_id = ghosts.id)")
    c.commit()


# ------------------------------------------------------------ ghosts

def resolve_ghosts(min_count=2, limit=50, log=print):
    """Fetch abstracts for ghosts cited by >= min_count library papers."""
    c = store.conn()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM ghosts WHERE state = 'new' AND cite_count >= ?"
        " ORDER BY cite_count DESC LIMIT ?", (min_count, limit))]
    by_oa = openalex.works_by_ids([r["openalex_id"] for r in rows if r["openalex_id"]])
    done = 0
    for g in rows:
        w = by_oa.get(g["openalex_id"]) if g["openalex_id"] else None
        if not w and g["doi"]:
            w = openalex.work(g["doi"])
        if not w and g["title"]:
            try:
                cands = orx.search(g["title"], limit=3)
            except orx.OrxError as e:
                log(f"  ghost {g['id']}: orx {e}")
                break
            for cand in cands:
                if catalog.similarity(cand.get("title", ""), g["title"]) >= config.RESOLVE_MIN:
                    w = openalex.work(cand["id"])
                    break
        if w:
            c.execute("UPDATE ghosts SET title = ?, year = ?, doi = ?, openalex_id = ?,"
                      " authors = ?, venue = ?, abstract = ?, global_citations = ?,"
                      " state = 'resolved', updated_at = datetime('now') WHERE id = ?",
                      (w["title"] or g["title"], w["year"], w["doi"], w["openalex_id"],
                       w["authors"][:300], w["venue"][:200], w["abstract"],
                       w["cited_by"], g["id"]))
        else:
            c.execute("UPDATE ghosts SET state = 'unresolved' WHERE id = ?", (g["id"],))
        c.commit()
        done += 1
    return done


def triage_ghosts(limit=25, log=print):
    """Haiku relevance for resolved ghosts, so the reading queue ranks by
    cite_count x relevance instead of raw counts."""
    c = store.conn()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM ghosts WHERE state = 'resolved' ORDER BY cite_count DESC LIMIT ?",
        (limit,))]
    for g in rows:
        fake = {"id": -g["id"], "title": g["title"], "authors": g["authors"],
                "year": g["year"], "venue": g["venue"],
                "citation": f"{g['authors']}. {g['title']}. {g['venue']} {g['year']}",
                "tags": []}
        source = "abstract" if g["abstract"] else "citation"
        data, _ = catalog.triage(fake, {"abstract": g["abstract"]}, g["abstract"], source)
        c.execute("UPDATE ghosts SET relevance = ?, summary = ?, why = ?, state = 'triaged',"
                  " updated_at = datetime('now') WHERE id = ?",
                  (data["relevance"], data["summary"], data["why"], g["id"]))
        c.commit()
        log(f"  ghost {g['id']} ({g['cite_count']}x): relevance {data['relevance']} {g['title'][:60]}")
    return len(rows)


AUTO_ADD_MIN_CITES = 5      # default; override with meta auto_add_min_cites
AUTO_ADD_MIN_RELEVANCE = 2


def auto_add_ghosts(limit=10, log=print):
    """File ghosts the library keeps citing (5+ papers by default, relevance 2+) as new
    library papers. Each is then cataloged like any
    new paper, so one without full text joins the PDF-request queue. Writes go
    through the app's db.py. Disable with meta auto_add_ghosts = 0."""
    from . import files
    c = store.conn()
    if store.get_meta(c, "auto_add_ghosts", "1") != "1":
        return []
    min_cites = int(store.get_meta(c, "auto_add_min_cites", str(AUTO_ADD_MIN_CITES)) or AUTO_ADD_MIN_CITES)
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM ghosts WHERE state = 'triaged' AND cite_count >= ? AND relevance >= ?"
        " ORDER BY cite_count DESC, relevance DESC LIMIT ?",
        (min_cites, AUTO_ADD_MIN_RELEVANCE, limit))]
    db = config.app_db()
    lib = catalog.libdb()
    index = LibraryIndex()
    added = []
    for g in rows:
        # Someone may have added it by hand since it became a ghost.
        pid, score = index.match({"title": g["title"], "doi": g["doi"],
                                  "openalex_id": g["openalex_id"], "year": g["year"]})
        if pid is None:
            authors = [a.strip() for a in g["authors"].split(",") if a.strip()]
            paper = db.create_paper(lib, {
                "citation": files.citation_from(g), "title": g["title"],
                "authors": ", ".join(files._abbrev(a) for a in authors)[:500],
                "year": g["year"], "venue": g["venue"]})
            pid = paper["id"]
            store.update_state(c, pid, resolve_state="resolved", match_score=1.0,
                               doi=g["doi"], openalex_id=g["openalex_id"],
                               oa_title=g["title"], oa_year=g["year"],
                               oa_authors=g["authors"][:500], oa_venue=g["venue"],
                               abstract=g["abstract"], global_citations=g["global_citations"])
            store.add_event(c, "filed", f"Added #{pid} from the reading queue: cited by "
                            f"{g['cite_count']} of your papers, relevance {g['relevance']}: "
                            f"{g['title'][:120]}", pid)
            log(f"  auto-added ghost g{g['id']} as #{pid} ({g['cite_count']}x): {g['title'][:60]}")
            added.append(pid)
        c.execute("UPDATE refs SET matched_paper_id = ?, match_score = 1.0, ghost_id = NULL"
                  " WHERE ghost_id = ?", (pid, g["id"]))
        c.execute("UPDATE ghosts SET state = 'added', cite_count = 0,"
                  " updated_at = datetime('now') WHERE id = ?", (g["id"],))
        c.commit()
    return added


# ----------------------------------------------------------- queries

def cited_by(paper_id):
    """Library papers that cite this one."""
    c = store.conn()
    return [r[0] for r in c.execute(
        "SELECT DISTINCT citing_id FROM refs WHERE matched_paper_id = ?", (paper_id,))]


def references(paper_id):
    c = store.conn()
    return [dict(r) for r in c.execute(
        "SELECT r.*, g.cite_count AS ghost_count, g.state AS ghost_state FROM refs r"
        " LEFT JOIN ghosts g ON g.id = r.ghost_id WHERE r.citing_id = ?"
        " ORDER BY r.source, r.ord", (paper_id,))]


def top_cited(n=25):
    c = store.conn()
    return [dict(r) for r in c.execute(
        "SELECT matched_paper_id AS paper_id, COUNT(DISTINCT citing_id) AS n FROM refs"
        " WHERE matched_paper_id IS NOT NULL GROUP BY matched_paper_id"
        " ORDER BY n DESC LIMIT ?", (n,))]


def top_ghosts(n=30, min_count=2):
    c = store.conn()
    return [dict(r) for r in c.execute(
        "SELECT *, cite_count * (COALESCE(relevance, 1) + 1) AS score FROM ghosts"
        " WHERE cite_count >= ? AND state NOT IN ('added', 'dismissed')"
        " ORDER BY score DESC, cite_count DESC LIMIT ?", (min_count, n))]
