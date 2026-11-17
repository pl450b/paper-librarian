"""The Librarian's query tools, shared by the daemon's HTTP API and the MCP
server the chat agents use. Everything here is read-only.
"""

import json
import time

from . import catalog, config, context, embed, refs, store, thesis_doc


# ----------------------------------------------------------------- tools

def _paper_card(pid):
    paper = catalog.get_paper(pid)
    if not paper:
        return None
    st = store.get_state(store.conn(), pid) or {}
    return {"paper_id": pid, "title": st.get("oa_title") or paper["title"],
            "authors": (st.get("oa_authors") or paper["authors"])[:300],
            "year": paper["year"], "venue": paper["venue"], "doi": st.get("doi", ""),
            "has_pdf": bool(paper["pdf_path"]), "tier": st.get("tier", ""),
            "relevance": st.get("relevance"),
            "tags": paper["tags"], "read_status": paper["read_status"],
            "in_draft": bool(paper["in_draft"]),
            "why": st.get("why", ""), "claims": st.get("claims", []),
            "gaps": st.get("gaps", [])}


def search(query, k=8, min_relevance=None, include_ghosts=False):
    return embed.search(query, k=k, min_relevance=min_relevance,
                        include_ghosts=include_ghosts)


def get_note(paper_id):
    pid = int(paper_id)
    card = _paper_card(pid)
    if not card:
        return {"error": f"no paper #{paper_id}"}
    st = store.get_state(store.conn(), pid) or {}
    note = ""
    if st.get("note_path"):
        p = config.LIB_DIR / st["note_path"]
        if p.exists():
            note = p.read_text(encoding="utf-8")
    card["abstract"] = st.get("abstract", "")
    card["librarian_note"] = note
    card["your_notes"] = catalog.get_paper(pid)["notes"]
    card["cited_by_in_library"] = refs.cited_by(pid)
    return card


def related(paper_id, k=8):
    pid = int(paper_id)
    paper = catalog.get_paper(pid)
    if not paper:
        return {"error": f"no paper #{paper_id}"}
    st = store.get_state(store.conn(), pid) or {}
    q = (st.get("oa_title") or paper["title"]) + ". " + (st.get("abstract") or "")[:800]
    similar = [h for h in search(q, k=k + 1) if h.get("paper_id") != pid][:k]
    cites = [r for r in refs.references(pid) if r["matched_paper_id"]]
    return {"similar": similar,
            "cites_in_library": sorted({r["matched_paper_id"] for r in cites}),
            "cited_by_in_library": refs.cited_by(pid)}


def top_cited(scope="library", n=15):
    if scope == "ghosts":
        return [{"ghost_id": g["id"], "title": g["title"], "year": g["year"],
                 "doi": g["doi"], "cited_by_in_library": g["cite_count"],
                 "global_citations": g["global_citations"],
                 "relevance": g["relevance"], "why": g["why"]}
                for g in refs.top_ghosts(n)]
    out = []
    for r in refs.top_cited(n):
        card = _paper_card(r["paper_id"])
        if card:
            card["cited_by_in_library"] = r["n"]
            out.append(card)
    return out


def _by_json_field(field, value):
    c = store.conn()
    rows = [r["paper_id"] for r in c.execute(f"SELECT paper_id, {field} FROM paper_state")
            if value in json.loads(r[field] or "[]")]
    cards = [x for x in (_paper_card(p) for p in rows) if x]
    return sorted(cards, key=lambda x: -(x["relevance"] or 0))


def for_gap(gap_id):
    return {"gap": dict(context.gaps()).get(gap_id, ""),
            "papers": _by_json_field("gaps", gap_id)}


def for_claim(claim_id):
    return {"claim": dict(context.claims()).get(claim_id, ""),
            "papers": _by_json_field("claims", claim_id)}


def wanted():
    c = store.conn()
    rows = [dict(r) for r in c.execute(
        "SELECT * FROM requests WHERE state IN ('pending','asked','snoozed')"
        " ORDER BY priority, updated_at")]
    out = []
    for r in rows:
        card = _paper_card(r["paper_id"])
        if card:
            card["request_state"] = r["state"]
            out.append(card)
    return out


# ------------------------------------------------------------- progress

def _title(pid):
    st = store.get_state(store.conn(), pid) or {}
    p = catalog.get_paper(pid)
    return st.get("oa_title") or (p["title"] if p else f"#{pid}")


def status():
    """What the Librarian is doing: counts, queue, current job, pause state."""
    c = store.conn()
    lib = catalog.libdb()
    total = lib.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    tiers = {r["tier"]: r["n"] for r in c.execute(
        "SELECT tier, COUNT(*) n FROM paper_state WHERE cataloged_at IS NOT NULL GROUP BY tier")}
    rel = {str(r["relevance"]): r["n"] for r in c.execute(
        "SELECT relevance, COUNT(*) n FROM paper_state WHERE cataloged_at IS NOT NULL"
        " GROUP BY relevance")}
    q = store.queue_stats(c)
    running = c.execute("SELECT kind, paper_id, arg, started_at FROM jobs"
                        " WHERE state = 'running' LIMIT 1").fetchone()
    paused = ""
    if store.get_meta(c, "paused") == "1":
        paused = "paused by the user"
    elif context.is_template():
        paused = "waiting for librarian/brief.md to be filled in"
    else:
        until = store.get_meta(c, "paused_until")
        if until and float(until) > time.time():
            paused = (f"paused ({store.get_meta(c, 'paused_reason') or 'usage limit'}) until "
                      + time.strftime("%a %H:%M", time.localtime(float(until))))
    done_today = c.execute("SELECT COUNT(*) FROM jobs WHERE kind = 'catalog' AND state = 'done'"
                           " AND finished_at >= datetime('now', '-24 hours')").fetchone()[0]
    return {
        "papers_in_library": total,
        "cataloged": sum(tiers.values()),
        "by_tier": {"read_in_full": tiers.get("deep", 0), "skimmed": tiers.get("skim", 0),
                    "wanted_pdf": tiers.get("wanted", 0)},
        "by_relevance": rel,
        "queued_jobs": q["queued"],
        "queued_total": sum(q["queued"].values()),
        "now_working_on": ({"job": running["kind"], "paper_id": running["paper_id"],
                            "title": _title(running["paper_id"]) if running["paper_id"] else
                            running["arg"], "since": running["started_at"]} if running else None),
        "paused": paused,
        "backfill_on": store.get_meta(c, "backfill") == "1",
        "papers_cataloged_last_24h": done_today,
        "failed_jobs": q["failed"],
        "open_pdf_requests": c.execute("SELECT COUNT(*) FROM requests WHERE state IN"
                                       " ('pending','asked')").fetchone()[0],
        "ghost_papers_cited_2plus": c.execute("SELECT COUNT(*) FROM ghosts WHERE cite_count >= 2"
                                              " AND state NOT IN ('added','dismissed')").fetchone()[0],
        "recent_events": [{"kind": e["kind"], "paper_id": e["paper_id"], "text": e["text"][:300],
                           "at": e["created_at"]} for e in store.recent_events(c, 10)],
    }


def recent(n=15):
    """The most recently cataloged papers, newest first."""
    c = store.conn()
    rows = c.execute("SELECT paper_id, tier, relevance, text_source, cataloged_at, why,"
                     " oa_title FROM paper_state WHERE cataloged_at IS NOT NULL"
                     " ORDER BY cataloged_at DESC LIMIT ?", (int(n),)).fetchall()
    out = []
    for r in rows:
        p = catalog.get_paper(r["paper_id"])
        out.append({"paper_id": r["paper_id"], "title": r["oa_title"] or (p["title"] if p else ""),
                    "year": p["year"] if p else "", "tier": r["tier"],
                    "relevance": r["relevance"], "text_source": r["text_source"],
                    "cataloged_at": r["cataloged_at"], "why": r["why"]})
    return out


def queue(n=15):
    """Jobs waiting in the queue, in the order they will run."""
    c = store.conn()
    rows = c.execute("SELECT kind, paper_id, arg, priority, attempts, error, run_after FROM jobs"
                     " WHERE state = 'queued' ORDER BY priority, id LIMIT ?", (int(n),)).fetchall()
    return [{"job": r["kind"], "paper_id": r["paper_id"],
             "title": _title(r["paper_id"]) if r["paper_id"] else r["arg"],
             "priority": r["priority"], "retry": r["attempts"] > 0,
             "waiting_until": r["run_after"]} for r in rows]


# ------------------------------------------------------------- the draft

def draft_outline():
    return thesis_doc.outline()


def draft_section(section, include_subsections=True, offset=0):
    return thesis_doc.section(section, include_subsections, offset)


def search_draft(query, k=6):
    return thesis_doc.search(query, k)


def draft_cites(paper_id=None):
    return thesis_doc.cites(paper_id)


# ------------------------------------------------------------- registry

TOOLS = {
    "search": (search, "Hybrid semantic + keyword search over the library's papers, "
               "notes and (optionally) papers it cites but doesn't have.", {
                   "query": {"type": "string", "description": "natural-language query"},
                   "k": {"type": "integer", "description": "max results (default 8)"},
                   "min_relevance": {"type": "integer", "description": "0-3"},
                   "include_ghosts": {"type": "boolean"}}, ["query"]),
    "get_note": (get_note, "Everything known about one paper: metadata, abstract, "
                 "the Librarian's note, and the user's own notes.", {
                     "paper_id": {"type": "integer"}}, ["paper_id"]),
    "related": (related, "Papers similar to a paper, and its in-library citation links.", {
        "paper_id": {"type": "integer"}, "k": {"type": "integer"}}, ["paper_id"]),
    "top_cited": (top_cited, "Most-cited papers within the library (scope='library') or "
                  "most-cited papers the library lacks (scope='ghosts').", {
                      "scope": {"enum": ["library", "ghosts"]},
                      "n": {"type": "integer"}}, []),
    "for_gap": (for_gap, "Candidate papers for one of the brief's citation gaps (G1, G2, ...).", {
        "gap_id": {"type": "string"}}, ["gap_id"]),
    "for_claim": (for_claim, "Papers bearing on one of the brief's key claims, by claim id.", {
        "claim_id": {"type": "string"}}, ["claim_id"]),
    "wanted": (wanted, "Papers the Librarian has asked the user to find PDFs for.", {}, []),
    "status": (status, "The Librarian's own progress: how many papers are cataloged (by "
               "tier and relevance), the job queue, what it is working on now, whether "
               "it is paused, failures, and recent events. Use for any question about "
               "progress, the backfill, or what the Librarian is doing.", {}, []),
    "recent": (recent, "The most recently cataloged papers, newest first.", {
        "n": {"type": "integer", "description": "how many (default 15)"}}, []),
    "queue": (queue, "Jobs waiting in the queue, in the order they will run.", {
        "n": {"type": "integer", "description": "how many (default 15)"}}, []),
    "draft_outline": (draft_outline, "The user's LaTeX draft as it stands on disk: "
                      "section numbers, titles, word counts, TODO counts, citations per section.",
                      {}, []),
    "draft_section": (draft_section, "LaTeX source of one draft section, by number "
                      "('3.3') or title. Long sections are paged with offset.", {
                          "section": {"type": "string"},
                          "include_subsections": {"type": "boolean"},
                          "offset": {"type": "integer"}}, ["section"]),
    "search_draft": (search_draft, "Passages of the draft most related to a query "
                     "(semantic + keyword). Use to find what the draft already says.", {
                         "query": {"type": "string"},
                         "k": {"type": "integer", "description": "max passages (default 6)"}},
                     ["query"]),
    "draft_cites": (draft_cites, "What the draft cites. With paper_id: whether that paper "
                    "is in the .bib and which sections cite it. Without: every cited key, "
                    "its library paper id, where it is cited, and .bib entries never cited.", {
                        "paper_id": {"type": "integer"}}, []),
}


def call(name, args):
    if name not in TOOLS:
        return {"error": f"unknown tool {name}"}
    return TOOLS[name][0](**(args or {}))
