"""LIBRARY.md: the regenerated overview the chat agent loads first.

Kept small: it goes into the first turn of every library-wide chat.
"""

import datetime
import json

from . import catalog, config, context, refs, store

def _title(pid, states):
    st = states.get(pid)
    if st and st["oa_title"]:
        return st["oa_title"]
    p = catalog.get_paper(pid)
    return p["title"] if p else f"#{pid}"


def build():
    c = store.conn()
    states = store.all_states(c)
    lib = catalog.libdb()
    total = lib.execute("SELECT COUNT(*) FROM papers").fetchone()[0]
    cat = [s for s in states.values() if s["cataloged_at"]]
    tiers = {}
    for s in cat:
        tiers[s["tier"]] = tiers.get(s["tier"], 0) + 1
    L = [f"# Library digest", "",
         f"Generated {datetime.datetime.now():%Y-%m-%d %H:%M} by the Librarian. Do not edit.",
         "", f"{total} papers; {len(cat)} cataloged "
         f"({tiers.get('deep', 0)} read in full, {tiers.get('skim', 0)} skimmed, "
         f"{tiers.get('wanted', 0)} wanted). Paper ids are `#id`.", ""]

    def line(s):
        pid = s["paper_id"]
        why = (s["why"] or "").replace("\n", " ")
        return f"- #{pid} [{s['relevance']}/{s['tier']}] {_title(pid, states)[:110]} — {why[:160]}"

    contra = [s for s in cat if (s["contradicts"] or "").strip()]
    if contra:
        L += ["## May contradict the project", *[line(s) for s in contra[:15]], ""]

    for cid, claim in context.claims():
        rows = sorted((s for s in cat if cid in json.loads(s["claims"] or "[]")),
                      key=lambda s: -(s["relevance"] or 0))
        if rows:
            L += [f"## Claim {cid}: {claim[:80]}", *[line(s) for s in rows[:15]], ""]

    gaps = context.gaps()
    gap_rows = []
    for gid, text in gaps:
        cands = sorted((s for s in cat if gid in json.loads(s["gaps"] or "[]")),
                       key=lambda s: -(s["relevance"] or 0))
        if cands:
            gap_rows.append(f"- **{gid}** {text[:90]}: " +
                            ", ".join(f"#{s['paper_id']}" for s in cands[:6]))
        else:
            gap_rows.append(f"- **{gid}** {text[:90]}: *no candidates yet*")
    if gap_rows:
        L += ["## Citation gaps", *gap_rows, ""]

    top = refs.top_cited(20)
    if top:
        L += ["## Most cited within the library"]
        L += [f"- #{r['paper_id']} cited by {r['n']}: {_title(r['paper_id'], states)[:110]}"
              for r in top]
        L.append("")

    ghosts = refs.top_ghosts(25)
    if ghosts:
        L += ["## Not in the library, but cited by it (ghosts)"]
        L += [f"- g{g['id']} cited by {g['cite_count']}, relevance "
              f"{g['relevance'] if g['relevance'] is not None else '?'}: "
              f"{g['title'][:110]} ({g['year']})" for g in ghosts]
        L.append("")

    wanted = store.due_requests(c, 25)
    asked = [dict(r) for r in c.execute("SELECT * FROM requests WHERE state = 'asked'")]
    if wanted or asked:
        L += ["## Wanted PDFs (requested)"]
        L += [f"- #{r['paper_id']} [{r['state']}] {_title(r['paper_id'], states)[:110]}"
              for r in asked + wanted]
        L.append("")

    unsure = [s for s in states.values() if s["resolve_state"] == "uncertain"]
    if unsure:
        L += ["## Uncertain metadata matches",
              *[f"- #{s['paper_id']} matched '{s['oa_title'][:80]}' at {s['match_score']}"
                for s in unsure[:20]], ""]
    return "\n".join(L)


def write():
    text = build()
    config.DIGEST_PATH.write_text(text, encoding="utf-8")
    return text
