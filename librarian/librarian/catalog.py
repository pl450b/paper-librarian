"""The per-paper pipeline: resolve -> text -> triage -> tier -> deep note.
One public entry point: catalog(paper_id).
"""

import datetime
import difflib
import json
import re
import threading
import urllib.request

from . import config, context, extract, llm, openalex, orx, store

_lib = threading.local()


def libdb():
    """Thread-local connection to the user's library.db, via the app's db.py."""
    c = getattr(_lib, "conn", None)
    if c is None:
        db = config.app_db()
        c = db.connect()
        c.execute("PRAGMA busy_timeout = 30000")
        _lib.conn = c
    return c


def get_paper(paper_id):
    return config.app_db().get_paper(libdb(), paper_id)


def norm(title):
    return config.app_db().norm_title(title)


_STOP = set("a an the of for and in on with to from by via using based towards toward "
            "is are its their our we under over into at as or vs versus".split())


def _words(t):
    return [w for w in norm(t).split() if w not in _STOP and len(w) > 2]


def similarity(a, b):
    """Title similarity in [0, 1]: the better of character similarity and word
    containment (so 'X' vs 'X: a longer subtitle' still scores high). Containment
    needs 4+ significant words, so short nicknames can't match long titles."""
    na, nb = norm(a), norm(b)
    if not na or not nb:
        return 0.0
    ratio = difflib.SequenceMatcher(None, na, nb).ratio()
    wa, wb = set(_words(a)), set(_words(b))
    short, long_ = (wa, wb) if len(wa) <= len(wb) else (wb, wa)
    if len(short) >= 4 and len(short) / max(len(long_), 1) >= 0.6:
        ratio = max(ratio, 0.97 * len(short & long_) / len(short))
    return ratio


def render(template, **kw):
    for k, v in kw.items():
        template = template.replace("{" + k + "}", str(v if v is not None else ""))
    return template


def _prompt(name):
    return (config.PROMPTS_DIR / f"{name}.md").read_text(encoding="utf-8")


def system_prompt():
    return render(_prompt("system"), context=context.text(), gaps=context.gaps_block(),
                  claims=context.claims_block())


# ------------------------------------------------------------------ resolve

_DOI_IN_TEXT = re.compile(r"\b(10\.\d{4,9}/[^\s\"<>,;]+)", re.I)


def _apply_work(c, paper_id, w, score, state="resolved"):
    store.update_state(
        c, paper_id, resolve_state=state, match_score=round(score, 3),
        doi=w["doi"], openalex_id=w["openalex_id"], arxiv_id=w["arxiv_id"],
        oa_title=w["title"], oa_year=w["year"], oa_authors=w["authors"][:500],
        oa_venue=w["venue"], abstract=w["abstract"], global_citations=w["cited_by"],
        oa_pdf_url=w["oa_pdf_url"])


_QUOTED = re.compile(r"[\"“”‘’]{1,2}([^\"“”‘’]{12,300}?)[,.]?[\"“”‘’]{1,2}")
_BIBTEX_TITLE = re.compile(r"title\s*=\s*[{\"]+([^}\"]{12,300})", re.I)


def title_candidates(paper):
    """Every plausible title for a library row, best first. Row titles are
    parsed heuristically and are sometimes a venue or a nickname."""
    cands = []
    for rx in (_BIBTEX_TITLE, _QUOTED):
        m = rx.search(paper.get("citation") or "")
        if m:
            cands.append(m.group(1).strip().rstrip(",."))
    if paper.get("title"):
        cands.append(paper["title"])
    # Author-year style: "Surname, A. (2015). Title. Venue."
    m = re.search(r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)\.?\s*(.{12,300}?)[.?](?:\s|$)",
                  paper.get("citation") or "")
    if m:
        cands.append(m.group(1))
    out = []
    for t in cands:
        t = re.sub(r"\s+", " ", t).strip()
        t = re.sub(r",?\s*arXiv preprint.*$", "", t, flags=re.I)
        if len(t) >= 12 and t.lower() not in [o.lower() for o in out]:
            out.append(t)
    return out[:3]


def _pdf_identity(paper):
    """(doi, title) read from the paper's own PDF, for rows whose title is a
    nickname. Uses the same logic as PDF intake (files.identify)."""
    if not paper.get("pdf_path"):
        return "", ""
    pdf = config.PAPERS_DIR / paper["pdf_path"]
    if not pdf.exists():
        return "", ""
    pages = extract.pdf_pages(pdf)
    head = "\n".join(pages[:2]) if pages else ""
    m = _DOI_IN_TEXT.search(head)
    doi = m.group(1).rstrip(".)") if m else ""
    title = extract.pdf_title(pdf)
    if re.match(r"^\s*(pii|doi)\b", title or "", re.I):
        title = ""
    if not title and pages:
        from . import files
        try:
            title = files._llm_title(pages[0])
        except Exception:
            title = extract.first_page_title(pages)
    return doi, title


CITATION_SCHEMA = {
    "type": "object", "additionalProperties": False,
    "required": ["title", "year", "is_book"],
    "properties": {"title": {"type": "string"}, "year": {"type": "string"},
                   "is_book": {"type": "boolean"}},
}


def _llm_citation_title(citation):
    """Last resort: Haiku reads a citation string in any style and returns its
    title, repairing extraction damage (lost hyphens, merged words). The prompt
    is tiny, so this costs a few hundred tokens, not a triage's ~9k."""
    data, _ = llm.structured(
        "Citation:\n" + citation[:1200], CITATION_SCHEMA,
        "You extract the title of the cited work from one bibliographic citation, "
        "in any citation style. Repair obvious text-extraction damage in the title "
        "(e.g. 'HodgkinHuxley' -> 'Hodgkin-Huxley', broken accents), but do not "
        "otherwise change or complete it. If the citation names no title, return ''.",
        config.TRIAGE_MODEL, timeout=120)
    return data["title"].strip(), data["year"].strip()


def _score(cand_title, cand_year, title, year):
    s = similarity(cand_title, title)
    y1, y2 = str(cand_year or "")[:4], str(year or "")[:4]
    if y1.isdigit() and y2.isdigit() and abs(int(y1) - int(y2)) > 2:
        s -= 0.1           # preprint vs journal years differ by 1-2; more is suspicious
    return s


def resolve(paper, force=False):
    """Find the paper in OpenAlex. Tries, in order: a DOI in the citation;
    every title candidate (orx search, then OpenAlex title search); and for
    rows with a PDF, the DOI or title printed in the PDF itself. Ambiguous
    matches are kept as 'uncertain' and their metadata is not used."""
    c = store.conn()
    st = store.get_state(c, paper["id"]) or {}
    if not force and st.get("resolve_state") in ("resolved", "unresolved", "uncertain"):
        return st
    year = paper.get("year") or ""

    m = _DOI_IN_TEXT.search(paper["citation"] or "")
    if m:
        w = openalex.work(m.group(1).rstrip(".)"))
        if w:
            _apply_work(c, paper["id"], w, 1.0)
            return store.get_state(c, paper["id"])

    titles = title_candidates(paper)
    best, best_score, best_title = None, 0.0, ""

    def consider(cands, title):
        nonlocal best, best_score, best_title
        for cand in cands:
            sc = _score(cand.get("title", ""), cand.get("publicationDate") or cand.get("year"),
                        title, year)
            if sc > best_score:
                best, best_score, best_title = cand, sc, title

    for t in titles:
        try:
            consider(orx.search(t, limit=5), t)
        except orx.RateLimited:
            raise
        except orx.OrxError as e:
            store.update_state(c, paper["id"], error=f"orx: {e}")
        if best_score >= config.RESOLVE_MIN:
            break
        consider(openalex.title_search(t), t)
        if best_score >= config.RESOLVE_MIN:
            break

    if best_score < config.RESOLVE_MIN and paper.get("pdf_path"):
        doi, pdf_title = _pdf_identity(paper)
        if doi:
            w = openalex.work(doi)
            if w and (not pdf_title or similarity(w["title"], pdf_title) >= config.RESOLVE_UNCERTAIN):
                _apply_work(c, paper["id"], w, 1.0)
                return store.get_state(c, paper["id"])
        if pdf_title:
            # The PDF's own title is authoritative, so match against it, not the row.
            consider(orx.search(pdf_title, limit=5), pdf_title)
            if best_score < config.RESOLVE_MIN:
                consider(openalex.title_search(pdf_title), pdf_title)

    if best_score < config.RESOLVE_MIN and len(paper.get("citation") or "") > 30:
        try:
            t, y = _llm_citation_title(paper["citation"])
        except llm.UsageLimit:
            raise
        except llm.LLMError:
            t, y = "", ""
        if t and t.lower() not in [x.lower() for x in titles]:
            year = year or y
            consider(orx.search(t, limit=5), t)
            if best_score < config.RESOLVE_MIN:
                consider(openalex.title_search(t), t)

    if best and best_score >= config.RESOLVE_UNCERTAIN:
        w = openalex.work(best["id"])
        if w:
            if best_score >= config.RESOLVE_MIN:
                _apply_work(c, paper["id"], w, best_score)
            else:
                store.update_state(c, paper["id"], resolve_state="uncertain",
                                   match_score=round(best_score, 3),
                                   oa_title=w["title"], oa_year=w["year"])
            return store.get_state(c, paper["id"])
    store.update_state(c, paper["id"], resolve_state="unresolved",
                       match_score=round(best_score, 3))
    return store.get_state(c, paper["id"])


# --------------------------------------------------------------------- text

def download_oa_pdf(paper_id, url):
    """Fetch an open-access PDF into librarian/cache/pdf/. Returns the path or None."""
    path = config.PDF_CACHE / f"{paper_id}.pdf"
    if path.exists():
        return path
    if not url:
        return None
    req = urllib.request.Request(url, headers={"User-Agent": "paper-librarian/0.1"})
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            data = r.read(config.MAX_PDF_BYTES + 1)
    except Exception:
        return None
    if not data.startswith(b"%PDF-") or len(data) > config.MAX_PDF_BYTES:
        return None
    tmp = path.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(path)
    return path


def get_text(paper, st):
    """(text, source). Text has [[page N]] markers when it came from a PDF."""
    if paper.get("pdf_path"):
        pdf = config.PAPERS_DIR / paper["pdf_path"]
        if pdf.exists():
            pages = extract.pdf_pages(pdf)
            if pages:
                return extract.with_page_markers(pages), "pdf"
    oa = config.PDF_CACHE / f"{paper['id']}.pdf"
    if oa.exists():
        pages = extract.pdf_pages(oa)
        if pages:
            return extract.with_page_markers(pages), "oa_pdf"
    if st.get("arxiv_id"):
        t = orx.arxiv_fulltext(st["arxiv_id"])
        if t:
            return t, "arxiv"
    if st.get("abstract"):
        return st["abstract"], "abstract"
    return "", "citation"


# ------------------------------------------------------------------ triage

def triage_schema():
    gap_ids = [g for g, _ in context.gaps()]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["relevance", "confidence", "claims", "areas", "gaps",
                     "suggested_tags", "contradicts", "summary", "why"],
        "properties": {
            "relevance": {"type": "integer", "minimum": 0, "maximum": 3},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "claims": context.enum_array(context.claim_ids()),
            "areas": context.enum_array(context.areas()),
            "gaps": context.enum_array(gap_ids),
            "suggested_tags": context.enum_array(context.tags()),
            "contradicts": {"type": "string"},
            "summary": {"type": "string"},
            "why": {"type": "string"},
        },
    }


def _meta(paper, st):
    return dict(
        title=st.get("oa_title") or paper["title"] or "(untitled)",
        authors=(st.get("oa_authors") or paper["authors"])[:400],
        year=st.get("oa_year") or paper["year"],
        venue=st.get("oa_venue") or paper["venue"],
        citation=paper["citation"][:800])


def triage(paper, st, text, source, model=None):
    model = model or config.TRIAGE_MODEL
    have = {
        "pdf": "the abstract, introduction and conclusion, from the full PDF",
        "oa_pdf": "the abstract, introduction and conclusion, from an open-access PDF",
        "arxiv": "the abstract, introduction and conclusion, from the arXiv full text",
        "abstract": "the abstract only (from OpenAlex); the full text was not available",
        "citation": "the citation string only; no abstract or text was found",
    }[source]
    if source in ("pdf", "oa_pdf", "arxiv"):
        body = extract.triage_excerpt(text, abstract=st.get("abstract", ""))
    elif source == "abstract":
        body = "ABSTRACT\n" + text
    else:
        body = "(no abstract available)"
    prompt = render(_prompt("triage"), have=have, body=body, **_meta(paper, st))
    data, info = llm.structured(prompt, triage_schema(), system_prompt(), model)
    data["suggested_tags"] = [t for t in data.get("suggested_tags", [])
                              if t not in paper.get("tags", [])]
    return data, info


# --------------------------------------------------------------- deep note

def note_schema():
    gap_ids = [g for g, _ in context.gaps()]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["headline", "claim", "setup", "key_numbers", "relevance",
                     "supports", "contradicts", "tensions", "not_established", "cite_for",
                     "relevance_score", "claims", "suggested_tags"],
        "properties": {
            "headline": {"type": "string"},
            "claim": {"type": "string"},
            "setup": {
                "type": "object", "additionalProperties": False,
                "required": ["platform", "baseline", "metric", "evidence"],
                "properties": {
                    "platform": {"type": "string"},
                    "baseline": {"type": "string"},
                    "metric": {"type": "string"},
                    "evidence": {"enum": ["measured", "simulated", "estimated",
                                          "mixed", "not applicable"]},
                }},
            "key_numbers": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["value", "what", "page"],
                "properties": {"value": {"type": "string"}, "what": {"type": "string"},
                               "page": {"type": "string"}}}},
            "relevance": {"type": "string"},
            "supports": {"type": "string"},
            "contradicts": {"type": "string"},
            "tensions": {"type": "string"},
            "not_established": {"type": "string"},
            "cite_for": {"type": "array", "items": {
                "type": "object", "additionalProperties": False,
                "required": ["gap", "statement"],
                "properties": {"gap": ({"enum": gap_ids} if gap_ids else {"type": "string"}),
                               "statement": {"type": "string"}}},
                **({} if gap_ids else {"maxItems": 0})},
            "relevance_score": {"type": "integer", "minimum": 0, "maximum": 3},
            "claims": context.enum_array(context.claim_ids()),
            "suggested_tags": context.enum_array(context.tags()),
        },
    }


DEEP_TEXT_CAP = 160_000   # characters of body text (~40k tokens)


def deep_read(paper, st, text, source, model=None):
    model = model or config.DEEP_MODEL
    body, _refs = extract.split_references(text)
    if source == "arxiv":
        body = ("(This text has no page markers; cite section names instead of "
                "page numbers.)\n\n" + body)
    if len(body) > DEEP_TEXT_CAP:
        body = body[:DEEP_TEXT_CAP] + "\n\n[... text truncated ...]"
    prompt = render(_prompt("deep_note"), body=body,
                    triage_relevance=st.get("relevance"), triage_why=st.get("why", ""),
                    **_meta(paper, st))
    data, info = llm.structured(prompt, note_schema(), system_prompt(), model)
    data["suggested_tags"] = [t for t in data.get("suggested_tags", [])
                              if t not in paper.get("tags", [])]
    return data, info


def slug(title):
    s = re.sub(r"[^a-z0-9]+", "-", (title or "untitled").lower()).strip("-")
    return s[:60].rstrip("-") or "untitled"


def _page(page):
    page = re.sub(r"^\s*(?:pp?\.\s*)+", "", str(page or "")).strip()
    if not page:
        return ""
    return f" (pp. {page})" if re.search(r"[-–,]", page) else f" (p. {page})"


def _yaml_list(items):
    return "[" + ", ".join(json.dumps(i, ensure_ascii=False) for i in items) + "]"


def write_note(paper, st, tier, triage_data, note=None, info=None, pages=""):
    """Write notes/<id>-<slug>.md and return its path (relative to librarian/)."""
    meta = _meta(paper, st)
    gap_text = dict(context.gaps())
    if note:
        relevance, claims = note["relevance_score"], note["claims"]
        tags = note["suggested_tags"]
        gaps = [g["gap"] for g in note["cite_for"]]
    else:
        relevance, claims = triage_data["relevance"], triage_data["claims"]
        tags, gaps = triage_data["suggested_tags"], triage_data["gaps"]
    fm = [
        "---",
        f"paper_id: {paper['id']}",
        f"title: {json.dumps(meta['title'], ensure_ascii=False)}",
        f"authors: {json.dumps(meta['authors'][:200], ensure_ascii=False)}",
        f"year: {json.dumps(str(meta['year']))}",
        f"doi: {st.get('doi') or ''}",
        f"tier: {tier}",
        f"relevance: {relevance}",
        f"claims: {_yaml_list(claims)}",
        f"fills_gaps: {_yaml_list(gaps)}",
        f"suggested_tags: {_yaml_list(tags)}",
        f"text_source: {st.get('text_source') or ''}",
    ]
    if pages:
        fm.append(f"pages_read: {pages}")
    fm += [
        f"model: {(info or {}).get('model', '')}",
        f"context_hash: {context.digest()}",
        f"cataloged: {datetime.date.today().isoformat()}",
        "---", "",
        f"# {meta['title']}", "",
    ]
    out = fm
    if note:
        s = note["setup"]
        out += [f"**{note['headline']}**", "",
                "## Claim", note["claim"], "",
                "## Method & setup",
                f"- **Platform:** {s['platform']}",
                f"- **Baseline:** {s['baseline']}",
                f"- **Metric:** {s['metric']}",
                f"- **Evidence:** {s['evidence']}", "",
                "## Key numbers"]
        out += [f"- **{k['value']}**: {k['what']}" + _page(k["page"])
                for k in note["key_numbers"]] or ["- (none reported)"]
        out += ["", "## Relevance to the project", note["relevance"], ""]
        if note["contradicts"].strip():
            out += ["## ⚠ Contradicts the project", note["contradicts"], ""]
        if note["tensions"].strip():
            out += ["## Tensions with the project", note["tensions"], ""]
        if note["supports"].strip():
            out += ["## Supports", note["supports"], ""]
        out += ["## What it does not establish", note["not_established"], ""]
        if note["cite_for"]:
            out += ["## Cite for"]
            out += [f"- **{g['gap']}** ({gap_text.get(g['gap'], '')[:80]}): {g['statement']}"
                    for g in note["cite_for"]]
            out.append("")
    else:
        basis = {"abstract": "abstract only — not read in full",
                 "citation": "citation only — no abstract or text was available"
                 }.get(st.get("text_source"), "triaged from abstract, introduction "
                                               "and conclusion — not read in full")
        out += [f"*{basis}.*", "", triage_data["summary"], "",
                f"**Why it matters (or doesn't):** {triage_data['why']}", ""]
        if triage_data["contradicts"].strip():
            out += ["## ⚠ Contradicts the project", triage_data["contradicts"], ""]
        if tier == "wanted":
            out += ["*Relevant enough to read in full; the PDF has been requested.*", ""]
    name = f"{paper['id']}-{slug(meta['title'])}.md"
    path = config.NOTES_DIR / name
    for old in config.NOTES_DIR.glob(f"{paper['id']}-*.md"):
        if old.name != name:
            old.unlink()
    path.write_text("\n".join(out), encoding="utf-8")
    return f"notes/{name}"


# ----------------------------------------------------------------- catalog

def _request_priority(data):
    if data["contradicts"].strip() or data["relevance"] == 3:
        return 1
    return 3 if data["gaps"] else 5


def catalog(paper_id, force=False, deep=True, log=print):
    """Run the whole pipeline for one paper. Returns a short report dict."""
    config.ensure_dirs()
    c = store.conn()
    paper = get_paper(paper_id)
    if not paper:
        raise ValueError(f"no paper {paper_id}")
    ctx = context.digest()
    st = store.get_state(c, paper_id) or {}
    if not force and st.get("cataloged_at") and st.get("context_hash") == ctx:
        return {"paper_id": paper_id, "skipped": "already cataloged"}

    # A forced re-catalog retries matching for papers OpenAlex didn't match
    # (the resolver improves over time); a good match is kept as is.
    st = resolve(paper, force=force and st.get("resolve_state") != "resolved")
    text, source = get_text(paper, st)
    store.update_state(c, paper_id, text_source=source, error="")
    st = store.get_state(c, paper_id)
    if text and source != "abstract":
        extract.cache_text(paper_id, text)

    data, info = triage(paper, st, text, source)
    log(f"  triage #{paper_id}: relevance {data['relevance']} "
        f"(conf {data['confidence']:.2f}, {source}, {info['input_tokens']} tok in)")
    store.update_state(c, paper_id, relevance=data["relevance"],
                       confidence=data["confidence"], claims=data["claims"],
                       areas=data["areas"], gaps=data["gaps"],
                       suggested_tags=data["suggested_tags"],
                       contradicts=data["contradicts"], summary=data["summary"],
                       why=data["why"], triage_model=info["model"], context_hash=ctx)
    st = store.get_state(c, paper_id)

    note, ninfo, pages = None, info, ""
    tier = "skim"
    if data["relevance"] >= config.DEEP_MIN_RELEVANCE:
        if source not in ("pdf", "oa_pdf", "arxiv") and st.get("oa_pdf_url"):
            if download_oa_pdf(paper_id, st["oa_pdf_url"]):
                text, source = get_text(paper, st)
                store.update_state(c, paper_id, text_source=source)
                st = store.get_state(c, paper_id)
                extract.cache_text(paper_id, text)
        if source in ("pdf", "oa_pdf", "arxiv"):
            tier = "deep"
            if deep:
                note, ninfo = deep_read(paper, st, text, source)
                n_pages = text.count("[[page ")
                pages = f"1-{n_pages}" if n_pages else ""
                log(f"  deep   #{paper_id}: relevance {note['relevance_score']} "
                    f"({ninfo['input_tokens']} tok in, {ninfo['output_tokens']} out)")
                store.update_state(c, paper_id, relevance=note["relevance_score"],
                                   claims=note["claims"],
                                   gaps=[g["gap"] for g in note["cite_for"]],
                                   suggested_tags=note["suggested_tags"],
                                   contradicts=note["contradicts"],
                                   summary=note["headline"] + " " + data["summary"],
                                   note_model=ninfo["model"])
        else:
            tier = "wanted"
            # A score from the citation string alone is a guess: don't ask the
            # user to hunt for a PDF on that basis unless it looked core.
            if source != "citation" or data["relevance"] == 3:
                store.upsert_request(c, paper_id, data["why"], _request_priority(data))

    note_path = write_note(paper, st, tier, data, note, ninfo if note else info, pages)
    store.update_state(c, paper_id, tier=tier, note_path=note_path,
                       cataloged_at=datetime.datetime.now().isoformat(timespec="seconds"))
    return {"paper_id": paper_id, "tier": tier, "relevance":
            (note or {}).get("relevance_score", data["relevance"]),
            "source": source, "note": note_path}
