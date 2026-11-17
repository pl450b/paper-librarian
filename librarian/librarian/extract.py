"""Text extraction and sectioning. Deterministic, no LLM.

PDF text comes from poppler's `pdftotext` in its default reading-order mode
(not -layout: that interleaves journal sidebars and two-column text). Pages
are separated by form feeds, so page numbers survive into the notes.
"""

import hashlib
import re
import shutil
import subprocess
from pathlib import Path

from . import config

PDFTOTEXT = shutil.which("pdftotext")
PDFINFO = shutil.which("pdfinfo")


def sha256_file(path):
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def looks_like_pdf(path):
    try:
        with open(path, "rb") as fh:
            return fh.read(5) == b"%PDF-"
    except OSError:
        return False


def pdf_pages(path):
    """List of page texts. Empty list if extraction fails (e.g. a scan)."""
    if not PDFTOTEXT:
        raise RuntimeError("pdftotext (poppler) is not installed")
    try:
        p = subprocess.run([PDFTOTEXT, "-enc", "UTF-8", str(path), "-"],
                           capture_output=True, timeout=180)
    except subprocess.TimeoutExpired:
        return []
    if p.returncode != 0:
        return []
    text = p.stdout.decode("utf-8", errors="replace")
    pages = text.split("\f")
    if pages and not pages[-1].strip():
        pages.pop()
    # A scanned PDF yields pages with almost no text.
    if sum(len(pg.strip()) for pg in pages) < 500:
        return []
    return pages


def pdf_title(path):
    """The PDF's metadata title, if it looks like a real one."""
    if not PDFINFO:
        return ""
    try:
        p = subprocess.run([PDFINFO, str(path)], capture_output=True, timeout=30)
    except subprocess.TimeoutExpired:
        return ""
    for line in p.stdout.decode("utf-8", errors="replace").splitlines():
        if line.startswith("Title:"):
            t = line.split(":", 1)[1].strip()
            junk = ("untitled", "microsoft word", ".dvi", ".pdf", "doi:", "arxiv")
            if len(t) > 15 and not any(j in t.lower() for j in junk):
                return t
    return ""


def first_page_title(pages):
    """Heuristic title from the first page: the first substantial line(s)."""
    if not pages:
        return ""
    lines = [l.strip() for l in pages[0].splitlines() if l.strip()]
    skip = re.compile(r"(arxiv|doi|journal|proceedings|vol\.|volume|ieee|copyright|"
                      r"received|accepted|http|www\.|©|preprint|conference)", re.I)
    out = []
    for line in lines[:25]:
        line = re.sub(r"\s{2,}", " ", line)
        if skip.search(line) or len(line) < 12 or line.isdigit():
            if out:
                break
            continue
        out.append(line)
        if len(" ".join(out)) > 60 or len(out) == 2:
            break
    return " ".join(out)[:300]


def with_page_markers(pages):
    return "\n".join(f"\n[[page {i}]]\n{pg}" for i, pg in enumerate(pages, 1))


# ------------------------------------------------------------ sections

_HEAD = r"^\s*(?:[0-9IVX]+\.?\s*)?{name}\s*$"
_REF_HEAD = re.compile(r"^\s*(?:[0-9IVX]+\.?\s*)?(references|bibliography|"
                       r"literature cited|works cited)\s*:?\s*$", re.I | re.M)


def split_references(text):
    """(body, references_section). Uses the LAST references heading so that a
    'References' mention in a table of contents doesn't cut the paper short."""
    matches = list(_REF_HEAD.finditer(text))
    if not matches:
        return text, ""
    m = matches[-1]
    if m.start() < len(text) * 0.4:   # implausibly early: probably not the section
        return text, ""
    return text[:m.start()], text[m.end():]


def find_section(text, names, max_chars):
    for name in names:
        m = re.search(_HEAD.format(name=name), text, re.I | re.M)
        if m:
            return text[m.end():m.end() + max_chars].strip()
    return ""


def abstract_from_text(text):
    m = re.search(r"\babstract\b[\s.:—-]*", text[:12000], re.I)
    if not m:
        return ""
    chunk = text[m.end():m.end() + 3000]
    stop = re.search(r"\n\s*(?:[0-9I]+\.?\s*)?(introduction|index terms|keywords)\b",
                     chunk, re.I)
    return re.sub(r"\s+", " ", chunk[:stop.start()] if stop else chunk[:2000]).strip()


def triage_excerpt(text, abstract="", budget=14000):
    """Abstract + introduction + conclusion, capped, for a cheap relevance read.
    A clean abstract (from OpenAlex) is preferred over one parsed from the PDF."""
    body, _ = split_references(text)
    abstract = abstract or abstract_from_text(body)
    intro = find_section(body, ["introduction", "background"], 5000)
    concl = find_section(body, ["conclusions?", "concluding remarks",
                                "discussion and conclusions?", "conclusions? and future work",
                                "summary and conclusions?", "discussion"], 3500)
    if not intro:
        intro = re.sub(r"\s+", " ", body[:6000])
    if not concl:
        concl = body[-3500:]
    parts = []
    if abstract:
        parts.append("ABSTRACT\n" + abstract)
    parts.append("INTRODUCTION (excerpt)\n" + re.sub(r"[ \t]+", " ", intro))
    if concl:
        parts.append("CONCLUSION (excerpt)\n" + re.sub(r"[ \t]+", " ", concl))
    return "\n\n".join(parts)[:budget]


# ------------------------------------------------------------ references

_NUMBERED = re.compile(r"^\s*(?:\[(\d{1,3})\]|(\d{1,3})\.)\s+", re.M)


def parse_references(section):
    """Split a references section into entry strings. Returns [] if the
    structure isn't recognisable (the caller then asks Haiku)."""
    section = section.strip()
    if not section:
        return []
    marks = list(_NUMBERED.finditer(section))
    entries = []
    if len(marks) >= 5:
        for i, m in enumerate(marks):
            end = marks[i + 1].start() if i + 1 < len(marks) else len(section)
            entries.append(section[m.end():end])
    else:
        # Author–year style: a new entry starts at a line beginning with
        # "Surname, X." after a line that ended an entry (a period or year).
        cur = []
        for line in section.splitlines():
            s = line.strip()
            if not s:
                continue
            starts = re.match(r"^[A-Z][A-Za-z'’\-]+,\s+(?:[A-Z]\.|[A-Z][a-z]+)", s)
            if starts and cur and re.search(r"[.)]\s*$|\d{4}[a-z]?\.?\s*$", cur[-1]):
                entries.append(" ".join(cur))
                cur = []
            cur.append(s)
        if cur:
            entries.append(" ".join(cur))
        if len(entries) < 5:
            return []
    cleaned = []
    for e in entries:
        e = re.sub(r"\s+", " ", e).strip()
        e = re.sub(r"-\s(?=[a-z])", "", e)          # undo hyphenated line breaks
        if 20 <= len(e) <= 1200:
            cleaned.append(e)
    return cleaned


_QUOTED = re.compile(r"[\"“”]([^\"“”]{12,300})[\"“”]")
_DOI = re.compile(r"10\.\d{4,9}/[^\s\"<>,;]+", re.I)
_ARXIV = re.compile(r"arxiv[:\s/]*(?:abs/)?(\d{4}\.\d{4,5})", re.I)
_YEAR = re.compile(r"\b(19[5-9]\d|20[0-3]\d)\b")


def ref_fields(raw):
    """Best-effort title/year/doi/arxiv from one reference string."""
    doi = (_DOI.search(raw).group(0).rstrip(".)") if _DOI.search(raw) else "")
    arxiv = (_ARXIV.search(raw).group(1) if _ARXIV.search(raw) else "")
    years = _YEAR.findall(raw)
    year = years[-1] if years else ""
    q = _QUOTED.search(raw)
    if q:
        title = q.group(1).strip().rstrip(",.")
    else:
        # IEEE/ACM unquoted: "A. Author, B. Author. Title. Venue, 2020." or
        # author-year: "Author, A. (2020). Title. Venue."
        m = re.search(r"\(\s*(?:19|20)\d{2}[a-z]?\s*\)\.?\s*(.+?)[.?!](?:\s|$)", raw)
        if m:
            title = m.group(1)
        else:
            parts = [p.strip() for p in re.split(r"\.\s+(?=[A-Z])", raw) if p.strip()]
            # Skip leading author-ish chunks (short, lots of initials/commas).
            title = ""
            for p in parts[1:] if len(parts) > 1 else parts:
                if len(p) > 15 and not re.match(r"^(in|proc|vol|pp|ieee|acm)\b", p, re.I):
                    title = p
                    break
    return {"title": title.strip()[:300], "year": year, "doi": doi.lower(), "arxiv": arxiv}


def cache_text(paper_id, text):
    path = config.TEXT_CACHE / f"{paper_id}.txt"
    path.write_text(text, encoding="utf-8")
    return path


def cached_text(paper_id):
    path = config.TEXT_CACHE / f"{paper_id}.txt"
    return path.read_text(encoding="utf-8") if path.exists() else ""
