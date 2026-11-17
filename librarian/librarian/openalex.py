"""Direct OpenAlex REST calls, for what `orx` does not expose:
work details (ids, arXiv location, OA PDF) and `referenced_works`.

stdlib urllib only. No email goes in the User-Agent; set OPENALEX_API_KEY in
librarian/.env for a daily budget of your own (free at openalex.org/settings/api).
"""

import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request

from . import config

BASE = "https://api.openalex.org"
UA = "paper-librarian/0.1"
FIELDS = ("id,doi,title,publication_year,cited_by_count,ids,best_oa_location,"
          "locations,referenced_works,abstract_inverted_index,authorships,"
          "primary_location,type")
_last_call = [0.0]


class OpenAlexError(RuntimeError):
    pass


class RateLimited(OpenAlexError):
    """429: the daily budget is spent. retry_s comes from OpenAlex's own
    Retry-After / X-RateLimit-Reset headers."""
    def __init__(self, message, retry_s):
        super().__init__(message)
        self.retry_s = retry_s


def _get(path, params=None):
    params = dict(params or {})
    key = os.environ.get("OPENALEX_API_KEY")
    if key:
        params["api_key"] = key
    url = f"{BASE}{path}?{urllib.parse.urlencode(params, safe=':|,')}"
    for attempt in range(4):
        wait = 0.12 - (time.time() - _last_call[0])   # stay under ~10 req/s
        if wait > 0:
            time.sleep(wait)
        _last_call[0] = time.time()
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return None
            if e.code == 429:
                hint = e.headers.get("Retry-After") or e.headers.get("X-RateLimit-Reset") or ""
                retry_s = int(hint) if str(hint).isdigit() else 60
                if retry_s <= 30 and attempt < 3:      # a momentary per-second limit
                    time.sleep(retry_s + 1)
                    continue
                raise RateLimited(f"OpenAlex daily budget spent (HTTP 429); resets in "
                                  f"{retry_s // 3600}h{retry_s % 3600 // 60:02d}m", retry_s) from e
            if e.code in (500, 502, 503) and attempt < 3:
                time.sleep(2 ** attempt * 2)
                continue
            raise OpenAlexError(f"OpenAlex HTTP {e.code} for {path}") from e
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt < 3:
                time.sleep(2 ** attempt)
                continue
            raise OpenAlexError(f"OpenAlex unreachable: {e}") from e
    return None


def short_id(oa_id):
    return (oa_id or "").rsplit("/", 1)[-1]


def bare_doi(doi):
    doi = (doi or "").strip()
    for prefix in ("https://doi.org/", "http://doi.org/", "doi:"):
        if doi.lower().startswith(prefix):
            doi = doi[len(prefix):]
    return doi.lower()


def work(identifier):
    """One work by DOI or OpenAlex id (W…), as a simplified dict, or None."""
    ident = identifier.strip()
    if ident.lower().startswith("https://openalex.org/"):
        ident = short_id(ident)
    elif not ident.upper().startswith("W"):
        ident = "doi:" + bare_doi(ident)
    d = _get(f"/works/{ident}", {"select": FIELDS})
    return simplify(d) if d else None


def title_search(title, n=5):
    """OpenAlex's own title search, as candidates shaped like orx results."""
    q = re.sub(r"[^\w\s-]", " ", title)[:250].strip()
    if not q:
        return []
    d = _get("/works", {"filter": f"title.search:{q}", "per-page": n,
                        "select": "id,title,publication_year"})
    return [{"id": short_id(w["id"]), "title": w.get("title") or "",
             "year": str(w.get("publication_year") or "")}
            for w in (d or {}).get("results", [])]


def works_by_ids(oa_ids):
    """Batch lookup of OpenAlex ids (50 per request). Returns {short_id: dict}."""
    out = {}
    ids = [short_id(i) for i in oa_ids if i]
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        d = _get("/works", {"filter": "openalex:" + "|".join(batch),
                            "select": FIELDS, "per-page": 50})
        for w in (d or {}).get("results", []):
            s = simplify(w)
            out[s["openalex_id"]] = s
    return out


def abstract_from_index(inv):
    if not inv:
        return ""
    words = {}
    for word, positions in inv.items():
        for p in positions:
            words[p] = word
    return " ".join(words[i] for i in sorted(words))


def _arxiv_id(w):
    for loc in w.get("locations") or []:
        for key in ("landing_page_url", "pdf_url"):
            url = (loc or {}).get(key) or ""
            if "arxiv.org/" in url:
                tail = url.split("arxiv.org/", 1)[1]
                for pre in ("abs/", "pdf/"):
                    if tail.startswith(pre):
                        tail = tail[len(pre):]
                tail = tail.removesuffix(".pdf")
                base = tail.split("v")[0] if tail[:4].isdigit() else tail
                return base.strip("/")
    return ""


def simplify(w):
    authors = [((a.get("author") or {}).get("display_name") or "")
               for a in (w.get("authorships") or [])]
    venue = (((w.get("primary_location") or {}).get("source") or {})
             .get("display_name") or "")
    oa = w.get("best_oa_location") or {}
    return {
        "openalex_id": short_id(w.get("id")),
        "doi": bare_doi(w.get("doi")),
        "title": w.get("title") or "",
        "year": str(w.get("publication_year") or ""),
        "authors": ", ".join(a for a in authors if a),
        "venue": venue,
        "cited_by": w.get("cited_by_count"),
        "abstract": abstract_from_index(w.get("abstract_inverted_index")),
        "oa_pdf_url": oa.get("pdf_url") or "",
        "arxiv_id": _arxiv_id(w),
        "referenced_works": [short_id(r) for r in (w.get("referenced_works") or [])],
        "type": w.get("type") or "",
    }
