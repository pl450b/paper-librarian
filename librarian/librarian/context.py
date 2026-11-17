"""brief.md: the research brief every judgement is made against.

The brief is plain markdown written by the user. A few `## ` sections have a
meaning to the Librarian and are parsed here (matched by heading name, case-
insensitive, so they can be in any order):

  ## Project            what the work is about        -> prompts, chat
  ## Key claims         `- **A**: ...` bullets         -> claim ids per paper
  ## Citation gaps      `- ...` bullets               -> gap ids G1, G2, ...
  ## Topic areas        `- slug: description` bullets -> area labels
  ## Tags               markdown table, first column  -> tags it may suggest

Every other section (rubric, note conventions, anything else) is passed to the
models verbatim as part of the brief.

Its hash is stored with each triage, so editing the brief can trigger a
re-triage of everything it may have judged wrongly (see worker.reconcile).
"""

import hashlib
import re
import shutil

from . import config

USER_ONLY_TAGS = {"in-draft"}
DEFAULT_AREAS = ["other"]


def ensure_brief():
    """Create brief.md from the shipped template on first run."""
    if not config.CONTEXT_PATH.exists() and config.TEMPLATE_PATH.exists():
        shutil.copyfile(config.TEMPLATE_PATH, config.CONTEXT_PATH)


def text():
    ensure_brief()
    if not config.CONTEXT_PATH.exists():
        return ""
    return config.CONTEXT_PATH.read_text(encoding="utf-8")


def digest():
    return hashlib.sha256(text().encode("utf-8")).hexdigest()[:16]


def is_template():
    """True while brief.md still contains the template's placeholder marker.
    The worker refuses to spend tokens judging papers against a placeholder."""
    return "<!-- TEMPLATE -->" in text()


def section(name, md=None):
    """Body of the `## <name>` section (heading match is case-insensitive and
    ignores a leading number like '## 3.')."""
    md = text() if md is None else md
    m = re.search(rf"^##\s+(?:\d+\.\s*)?{re.escape(name)}\b.*?$(.*?)(?=^##\s|\Z)",
                  md, re.M | re.S | re.I)
    return m.group(1).strip() if m else ""


def _bullets(body):
    """Top-level and nested `- ` bullets, continuation lines joined:
    [(indent, text)]."""
    items = []
    for line in body.splitlines():
        m = re.match(r"^(\s*)[-*] (.+)$", line)
        if m:
            items.append([len(m.group(1)), m.group(2).strip()])
        elif items and line.strip() and line.startswith(" "):
            items[-1][1] += " " + line.strip()
    return items


def claims():
    """Key claims as [(id, text)]. Bullets look like `- **A**: text` or
    `- A: text`; ids are short (letters/digits)."""
    out = []
    for _, item in _bullets(section("Key claims")):
        m = re.match(r"^\**\s*([A-Za-z0-9]{1,4})\s*\**\s*[:.)\-–]\s*\**\s*(.+)$", item)
        if m:
            out.append((m.group(1), re.sub(r"\s+", " ", m.group(2)).strip("* ")))
    return out


def claim_ids():
    return [cid for cid, _ in claims()]


def gaps():
    """Citation gaps as [(id, text)]. Lines ending in ':' are headers for the
    nested bullets below them and are folded in."""
    out, prefix = [], ""
    for indent, item in _bullets(section("Citation gaps")):
        if item.endswith(":"):
            prefix = item[:-1].strip() + ": "
            continue
        if indent == 0:
            prefix = ""
        out.append((f"G{len(out) + 1}", prefix + re.sub(r"\s+", " ", item)))
    return out


def areas():
    """Topic-area slugs from `- slug: description` bullets, plus 'other'."""
    names = []
    for _, item in _bullets(section("Topic areas")):
        m = re.match(r"^`?([A-Za-z0-9_\-]+)`?\s*(?:[:—–-]|$)", item)
        if m and m.group(1) not in names:
            names.append(m.group(1))
    return names + [a for a in DEFAULT_AREAS if a not in names]


def tags():
    """Tag names the Librarian may suggest (first column of the Tags table)."""
    names = []
    for line in section("Tags").splitlines():
        m = re.match(r"^\|\s*([^|]+?)\s*\|", line)
        if m and m.group(1).lower() != "tag" and not set(m.group(1)) <= set("-: "):
            names.append(m.group(1))
    return [n for n in names if n.lower() not in USER_ONLY_TAGS]


def gaps_block():
    return "\n".join(f"{gid}: {g}" for gid, g in gaps()) or "(none listed)"


def claims_block():
    return "\n".join(f"{cid}: {c}" for cid, c in claims()) or "(none listed)"


def summary():
    """The short version for chat agents: the Project and Key claims sections."""
    md = text()
    parts = []
    for name in ("Project", "Key claims"):
        body = section(name, md)
        if body:
            parts.append(f"## {name}\n\n{body}")
    return "\n\n".join(parts)


def enum_array(values):
    """A JSON-schema array whose items must come from `values`. An empty
    `enum` is invalid JSON schema, so an empty list yields an always-empty array."""
    if not values:
        return {"type": "array", "items": {"type": "string"}, "maxItems": 0}
    return {"type": "array", "items": {"enum": list(values)}}
