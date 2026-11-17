"""Librarian command line.

  python -m librarian status
  python -m librarian ingest <paper_id> [<paper_id> ...] [--force] [--no-deep]
  python -m librarian find <text>              # look up paper ids by title
  python -m librarian gold                     # calibration check against gold_set.md
  python -m librarian backfill                 # queue every un-cataloged paper
  python -m librarian serve [--port 8766]      # the daemon (started by app.py)
"""

import argparse
import json
import sys

from . import config, store


def cmd_status(args):
    c = store.conn()
    lib = __import__("librarian.catalog", fromlist=["libdb"]).libdb()
    total = lib.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    rows = c.execute("SELECT tier, COUNT(*) n FROM paper_state WHERE cataloged_at"
                     " IS NOT NULL GROUP BY tier").fetchall()
    print(f"library papers: {total}")
    print("cataloged:", {r["tier"]: r["n"] for r in rows} or "none")
    print("queue:", json.dumps(store.queue_stats(c)))
    from . import orx, llm
    from . import context
    print("orx:", "ok" if orx.available() else "not installed (optional)", "| claude:",
          "ok" if llm.CLI else "MISSING (required)")
    if context.is_template():
        print("brief: librarian/brief.md is still the template; fill it in to start cataloging")


def cmd_find(args):
    lib = __import__("librarian.catalog", fromlist=["libdb"]).libdb()
    q = f"%{' '.join(args.text)}%"
    for r in lib.execute("SELECT id, year, title, pdf_path FROM papers WHERE title LIKE ?"
                         " OR citation LIKE ? LIMIT 20", (q, q)):
        print(f"#{r['id']:<4} {r['year']:<5} {'[pdf] ' if r['pdf_path'] else ''}{r['title'][:100]}")


def cmd_ingest(args):
    from . import catalog
    for pid in args.ids:
        print(f"#{pid}:")
        try:
            print("  ", json.dumps(catalog.catalog(pid, force=args.force,
                                                     deep=not args.no_deep)))
        except Exception as e:  # report and continue with the next id
            print(f"   error: {type(e).__name__}: {e}")


def cmd_gold(args):
    """Calibration check: the triage model vs the user's own ratings in gold_set.md."""
    import re
    from . import catalog
    path = config.LIB_DIR / "gold_set.md"
    if not path.exists():
        print("no librarian/gold_set.md; copy gold_set.template.md and rate some papers first")
        return
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        m = re.match(r"^\|\s*(\d+)\s*\|([^|]*)\|\s*([0-3]?)\s*\|", line)
        if m:
            rows.append((int(m.group(1)), m.group(2).strip(), m.group(3)))
    rated = [r for r in rows if r[2] != ""]
    if len(rated) < len(rows):
        print(f"{len(rows) - len(rated)} paper(s) in gold_set.md have no rating yet; "
              f"rate them all first so the check is blind.")
        if not args.partial:
            return
    out, agree, misses = [], 0, []
    for pid, label, mine in rated:
        paper = catalog.get_paper(pid)
        st = catalog.resolve(paper)
        text, source = catalog.get_text(paper, st)
        data, info = catalog.triage(paper, st, text, source)
        h, y = data["relevance"], int(mine)
        ok = abs(h - y) <= 1
        agree += ok
        if y == 3 and h <= 1:
            misses.append(pid)
        print(f"#{pid:<4} you {y}  model {h}  {'ok ' if ok else 'OFF'} ({source}) {label[:50]}")
        out.append(f"| {pid} | {label} | {y} | {h} | {'' if ok else 'off by ' + str(abs(h - y))}"
                   f" | {source} | {data['confidence']:.2f} | {data['why'].replace('|', '/')} |")
    passed = agree >= len(rated) - 2 and not misses
    report = ["# Gold-set report", "",
              f"Agreement within ±1: **{agree}/{len(rated)}**. "
              f"Rated-3 papers the model put at 0–1: **{misses or 'none'}**. "
              f"Result: **{'PASS' if passed else 'FAIL'}**.", "",
              "| id | Paper | You | Model | | Text | Conf. | Model's reason |",
              "|---|---|---|---|---|---|---|---|", *out]
    (config.LIB_DIR / "gold_report.md").write_text("\n".join(report), encoding="utf-8")
    print(f"\n{agree}/{len(rated)} within ±1, misses {misses or 'none'}: "
          f"{'PASS' if passed else 'FAIL'}  (see gold_report.md)")


def cmd_backfill(args):
    from . import worker
    c = store.conn()
    store.set_meta(c, "backfill", "1")
    print(worker.reconcile())
    print("Queued. The daemon (started by app.py) works through it in the background.")


def cmd_serve(args):
    from . import daemon
    daemon.main(port=args.port)


def main(argv=None):
    config.ensure_dirs()
    ap = argparse.ArgumentParser(prog="librarian")
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status").set_defaults(fn=cmd_status)
    p = sub.add_parser("find"); p.add_argument("text", nargs="+"); p.set_defaults(fn=cmd_find)
    p = sub.add_parser("ingest")
    p.add_argument("ids", nargs="+", type=int)
    p.add_argument("--force", action="store_true")
    p.add_argument("--no-deep", action="store_true")
    p.set_defaults(fn=cmd_ingest)
    p = sub.add_parser("gold")
    p.add_argument("--partial", action="store_true", help="run even if some are unrated")
    p.set_defaults(fn=cmd_gold)
    sub.add_parser("backfill").set_defaults(fn=cmd_backfill)
    p = sub.add_parser("serve")
    p.add_argument("--port", type=int, default=config.DAEMON_PORT)
    p.set_defaults(fn=cmd_serve)
    args = ap.parse_args(argv)
    args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
