#!/usr/bin/env python3
"""Bulk-add papers to the library from one or more BibTeX files.

    python3 import_bibtex.py refs.bib [more.bib ...] [--tag "To read"] [--dry-run]

Safe to re-run: an entry whose title (or DOI) already matches a paper in the
library is skipped, so nothing is duplicated and no note or tag is touched.
Works on a library that does not exist yet (it is created). Standard library
only, like the rest of the app.

If the Librarian is installed, papers imported while it is running are
cataloged automatically. Papers imported before it first started count as
"pre-existing": queue them with the Activity view's Start backfill button.
"""

import argparse
import re
import sys
from pathlib import Path

import db
import thesis as tex


def _author_list(raw):
    """BibTeX 'Last, First and First Last' -> ['F. Last', 'F. Last']."""
    out = []
    for name in re.split(r"\s+and\s+", raw or ""):
        name = name.strip().strip("{}")
        if not name or name.lower() == "others":
            continue
        if "," in name:
            last, first = [x.strip() for x in name.split(",", 1)]
        else:
            parts = name.split()
            last, first = parts[-1], " ".join(parts[:-1])
        initials = " ".join(p[0] + "." for p in re.split(r"[\s\-]+", first) if p[:1].isalpha())
        out.append(f"{initials} {last}".strip())
    return out


def citation(entry, authors):
    """A. Author, B. Author, "Title," Venue, Year. doi:..."""
    names = ", ".join(authors[:8]) + (", et al." if len(authors) > 8 else "")
    bits = [names + ","] if names else []
    bits.append(f'"{entry["title"] or "Untitled"},"')
    if entry["journal"]:
        bits.append(entry["journal"] + ",")
    if entry["year"]:
        bits.append(entry["year"] + ".")
    s = " ".join(bits)
    if entry["doi"]:
        s += f" doi:{entry['doi']}"
    return s


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("bib", nargs="+", type=Path, help=".bib file(s)")
    ap.add_argument("--tag", action="append", default=[],
                    help="tag every imported paper with this (repeatable)")
    ap.add_argument("--dry-run", action="store_true", help="report, write nothing")
    args = ap.parse_args(argv)

    missing = [p for p in args.bib if not p.is_file()]
    if missing:
        sys.exit(f"not found: {', '.join(map(str, missing))}")
    entries, dupes = tex.parse_bib(args.bib)

    conn = db.connect()
    db.init(conn)
    have_titles = {db.norm_title(r["title"]) for r in conn.execute("SELECT title FROM papers")
                   if r["title"]}
    have_dois = {m.group(1).lower() for (cit,) in conn.execute("SELECT citation FROM papers")
                 for m in [re.search(r"\b(10\.\d{4,9}/\S+)", cit or "")] if m}

    added = skipped = 0
    for key, e in entries.items():
        title = re.sub(r"\s+", " ", e["title"]).strip()
        doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", e["doi"].strip(), flags=re.I)
        if not title:
            print(f"  skip {key}: no title")
            skipped += 1
            continue
        if db.norm_title(title) in have_titles or (doi and doi.lower() in have_dois):
            skipped += 1
            continue
        authors = _author_list(e["author"])
        fields = {"citation": citation({**e, "title": title, "doi": doi}, authors),
                  "title": title, "authors": ", ".join(authors)[:500],
                  "year": e["year"][:4], "venue": e["journal"][:300]}
        if not args.dry_run:
            paper = db.create_paper(conn, fields)
            for t in args.tag:
                db.add_tag(conn, paper["id"], t)
        have_titles.add(db.norm_title(title))
        if doi:
            have_dois.add(doi.lower())
        added += 1

    verb = "would add" if args.dry_run else "added"
    print(f"{verb} {added} paper(s); skipped {skipped} already in the library or untitled"
          + (f"; {len(dupes)} duplicate bib key(s) ignored" if dupes else ""))


if __name__ == "__main__":
    main()
