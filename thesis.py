"""Thesis rendering: main.tex -> HTML, references.bib -> entries.

This module knows about LaTeX and nothing else: no SQL, no HTTP. It never
snapshots the draft — every call re-reads the files from disk, so the app
always shows whatever is on disk right now.

The renderer is deliberately partial. It handles the subset of LaTeX a thesis
draft actually uses (sectioning, lists, figures, tables, equations, citations,
cross-references, and the author's own \\newcommand shorthands) and degrades to
readable plain text for anything else, rather than pretending to be TeX.
"""

import re
from pathlib import Path

MAX_INPUT_DEPTH = 8

# Sectioning commands, outermost first. The index is the heading level.
SECTIONS = ["part", "chapter", "section", "subsection", "subsubsection", "paragraph"]

# Environments whose body is mathematics rather than prose.
MATH_ENVS = {"equation", "equation*", "align", "align*", "gather", "gather*",
             "multline", "multline*", "eqnarray", "eqnarray*", "displaymath"}

# Commands that carry no meaning once the document is HTML.
DROP_COMMANDS = {"maketitle", "tableofcontents", "listoffigures", "listoftables",
                 "clearpage", "newpage", "pagebreak", "noindent", "centering",
                 "par", "vfill", "bigskip", "medskip", "smallskip", "hfill",
                 "raggedright", "onehalfspacing", "doublespacing", "singlespacing"}

# Writing-note macros. Rendered as visible callouts (as they are in the PDF)
# and collected for the sidebar, instead of being expanded like other macros.
NOTE_MACROS = {"todo": "todo", "note": "note", "cne": "cne"}

# A small symbol table so equations read as something closer to maths than to
# source. Not a typesetter -- just enough that the formula is recognisable.
SYMBOLS = {
    r"\alpha": "α", r"\beta": "β", r"\gamma": "γ", r"\delta": "δ",
    r"\epsilon": "ε", r"\varepsilon": "ε", r"\zeta": "ζ", r"\eta": "η",
    r"\theta": "θ", r"\vartheta": "ϑ", r"\iota": "ι", r"\kappa": "κ",
    r"\lambda": "λ", r"\mu": "μ", r"\nu": "ν", r"\xi": "ξ", r"\pi": "π",
    r"\rho": "ρ", r"\sigma": "σ", r"\tau": "τ", r"\upsilon": "υ",
    r"\phi": "φ", r"\varphi": "φ", r"\chi": "χ", r"\psi": "ψ", r"\omega": "ω",
    r"\Gamma": "Γ", r"\Delta": "Δ", r"\Theta": "Θ", r"\Lambda": "Λ",
    r"\Xi": "Ξ", r"\Pi": "Π", r"\Sigma": "Σ", r"\Phi": "Φ", r"\Psi": "Ψ",
    r"\Omega": "Ω",
    r"\sum": "∑", r"\prod": "∏", r"\int": "∫", r"\partial": "∂",
    r"\infty": "∞", r"\nabla": "∇", r"\cdot": "·", r"\cdots": "⋯",
    r"\ldots": "…", r"\dots": "…", r"\times": "×", r"\div": "÷",
    r"\pm": "±", r"\mp": "∓", r"\leq": "≤", r"\le": "≤", r"\geq": "≥",
    r"\ge": "≥", r"\neq": "≠", r"\ne": "≠", r"\approx": "≈",
    r"\equiv": "≡", r"\propto": "∝", r"\sim": "∼", r"\in": "∈",
    r"\notin": "∉", r"\subset": "⊂", r"\subseteq": "⊆", r"\cup": "∪",
    r"\cap": "∩", r"\forall": "∀", r"\exists": "∃", r"\rightarrow": "→",
    r"\to": "→", r"\leftarrow": "←", r"\Rightarrow": "⇒", r"\Leftarrow": "⇐",
    r"\leftrightarrow": "↔", r"\top": "⊤", r"\bot": "⊥", r"\angle": "∠",
    r"\sqrt": "√", r"\prime": "′", r"\circ": "∘", r"\star": "⋆",
    r"\ll": "≪", r"\gg": "≫", r"\quad": " ", r"\qquad": "  ",
}

# Spacing/size commands inside maths that we simply drop.
MATH_NOISE = re.compile(
    r"\\(?:left|right|bigl|bigr|Bigl|Bigr|biggl|biggr|Biggl|Biggr|"
    r"displaystyle|limits|nolimits|!|,|;|:|>)")

SUPERSCRIPTS = str.maketrans("0123456789+-()n=ijkT", "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁽⁾ⁿ⁼ⁱʲᵏᵀ")
SUBSCRIPTS = str.maketrans("0123456789+-()aeioruvxjmnpst",
                           "₀₁₂₃₄₅₆₇₈₉₊₋₍₎ₐₑᵢₒᵣᵤᵥₓⱼₘₙₚₛₜ")


def esc(s):
    """HTML-escape. Every text node passes through here before it is emitted."""
    return (str(s).replace("&", "&amp;").replace("<", "&lt;")
            .replace(">", "&gt;").replace('"', "&quot;").replace("'", "&#39;"))


# --------------------------------------------------------------------------
# locating the thesis
# --------------------------------------------------------------------------

class Thesis:
    """The set of files that make up one thesis, resolved from its .tex path."""

    def __init__(self, tex_path):
        self.tex = Path(tex_path).expanduser().resolve()
        self.dir = self.tex.parent
        self.pdf = self.tex.with_suffix(".pdf")
        self.toc = self.tex.with_suffix(".toc")

    def exists(self):
        return self.tex.is_file()

    def safe_path(self, rel, extensions=()):
        """Resolve `rel` inside the thesis directory or return None.

        Same containment discipline as app.safe_pdf_path: the resolved path
        must stay under self.dir, so \\input{../../etc/passwd} cannot escape.
        """
        rel = (rel or "").strip().lstrip("/")
        if not rel:
            return None
        base = (self.dir / rel)
        try:
            base.resolve().relative_to(self.dir)
        except ValueError:
            return None
        candidates = [base] + [base.with_name(base.name + e) for e in extensions]
        for cand in candidates:
            if cand.is_file():
                return cand
        return None

    def bib_paths(self, source=None):
        """Every .bib file the document pulls in, in declaration order."""
        text = source if source is not None else _read(self.tex)
        found = []
        for m in re.finditer(r"\\(?:addbibresource|bibliography)\s*\{([^}]*)\}", text):
            for name in m.group(1).split(","):
                path = self.safe_path(name.strip(), extensions=(".bib",))
                if path and path not in found:
                    found.append(path)
        return found

    def sources(self):
        """Every file whose change should invalidate the render."""
        raw = _read(self.tex)
        files = [self.tex]
        for name in re.findall(r"\\(?:input|include)\b\s*\{([^}]*)\}", raw):
            path = self.safe_path(name.strip(), extensions=(".tex",))
            if path and path not in files:
                files.append(path)
        return files + [b for b in self.bib_paths(raw) if b not in files]

    def mtime(self):
        """Newest mtime across the tex, its includes, and the bibliography."""
        return max((p.stat().st_mtime for p in self.sources() if p.is_file()),
                   default=0.0)

    def status(self):
        return {
            "tex": str(self.tex),
            "exists": self.exists(),
            "mtime": self.mtime(),
            "pdf": self.pdf.name if self.pdf.is_file() else "",
            "pdf_mtime": self.pdf.stat().st_mtime if self.pdf.is_file() else 0.0,
        }


def _read(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def autodetect(start):
    """Find a plausible thesis .tex near `start` (the app directory)."""
    start = Path(start).resolve()
    for folder in (start.parent, start, start.parent.parent):
        for name in ("main.tex", "thesis.tex"):
            candidate = folder / name
            if candidate.is_file():
                return candidate
        for candidate in sorted(folder.glob("*.tex")):
            if re.search(r"\\begin\s*\{document\}", _read(candidate)):
                return candidate
    return None


# --------------------------------------------------------------------------
# lexing helpers
# --------------------------------------------------------------------------

def strip_comments(text):
    """Remove TeX comments, honouring \\%."""
    out = []
    for line in text.split("\n"):
        kept, i = [], 0
        while i < len(line):
            ch = line[i]
            if ch == "\\" and i + 1 < len(line):
                kept.append(line[i:i + 2])
                i += 2
                continue
            if ch == "%":
                break
            kept.append(ch)
            i += 1
        out.append("".join(kept))
    return "\n".join(out)


def read_group(text, i):
    """Read a brace group starting at text[i] == '{'. Returns (inner, end)."""
    if i >= len(text) or text[i] != "{":
        return "", i
    depth, j = 0, i
    while j < len(text):
        if text[j] == "\\":
            j += 2
            continue
        if text[j] == "{":
            depth += 1
        elif text[j] == "}":
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    return text[i + 1:], len(text)


def read_optional(text, i):
    """Read an optional [..] argument at text[i]. Returns (inner, end)."""
    if i >= len(text) or text[i] != "[":
        return "", i
    depth, j = 0, i
    while j < len(text):
        if text[j] == "[":
            depth += 1
        elif text[j] == "]":
            depth -= 1
            if depth == 0:
                return text[i + 1:j], j + 1
        j += 1
    return "", i


def skip_space(text, i):
    while i < len(text) and text[i] in " \t\n":
        i += 1
    return i


def resolve_inputs(thesis, text, depth=0, seen=None):
    """Inline \\input / \\include recursively, so split chapters just work."""
    if depth >= MAX_INPUT_DEPTH:
        return text
    seen = seen if seen is not None else set()

    def swap(m):
        path = thesis.safe_path(m.group(1).strip(), extensions=(".tex",))
        if path is None or str(path) in seen:
            return ""
        seen.add(str(path))
        return resolve_inputs(thesis, strip_comments(_read(path)), depth + 1, seen)

    return re.sub(r"\\(?:input|include)\b\s*\{([^}]*)\}", swap, text)


def parse_macros(preamble):
    """Collect the author's own \\newcommand / \\renewcommand definitions."""
    macros = {}
    for m in re.finditer(r"\\(?:re)?newcommand\*?\s*\{?\s*\\([A-Za-z@]+)\s*\}?", preamble):
        i = m.end()
        i = skip_space(preamble, i)
        argc = 0
        opt, i = read_optional(preamble, i)
        if opt.strip().isdigit():
            argc = int(opt.strip())
        _default, i = read_optional(preamble, i)
        i = skip_space(preamble, i)
        if i < len(preamble) and preamble[i] == "{":
            body, _ = read_group(preamble, i)
            macros[m.group(1)] = (argc, body)
    return macros


def expand_macros(text, macros, depth=0):
    """Expand user macros with 0 or 1 arguments. Note macros are left alone."""
    if depth > 4 or not macros:
        return text
    out, i, changed = [], 0, False
    while i < len(text):
        ch = text[i]
        if ch != "\\":
            out.append(ch)
            i += 1
            continue
        m = re.match(r"\\([A-Za-z@]+)", text[i:])
        if not m:
            out.append(text[i:i + 2])
            i += 2
            continue
        name = m.group(1)
        if name in NOTE_MACROS or name not in macros:
            out.append(m.group(0))
            i += m.end()
            continue
        argc, body = macros[name]
        j = i + m.end()
        if argc == 0:
            # \macro followed by {} or a space: swallow the usual TeX spacing form
            if j < len(text) and text[j] == "{" and text[j:j + 2] == "{}":
                j += 2
            out.append(body)
            i = j
            changed = True
            continue
        j = skip_space(text, j)
        if j < len(text) and text[j] == "{":
            arg, j = read_group(text, j)
            out.append(body.replace("#1", arg))
            i = j
            changed = True
            continue
        out.append(m.group(0))
        i += m.end()
    result = "".join(out)
    return expand_macros(result, macros, depth + 1) if changed else result


# --------------------------------------------------------------------------
# bibliography
# --------------------------------------------------------------------------

def parse_bib(paths):
    """Parse .bib files into {key: entry}. Returns (entries, duplicate_keys)."""
    entries, duplicates = {}, []
    for path in paths:
        text = _read(path)
        for m in re.finditer(r"@(\w+)\s*\{\s*([^,\s]+)\s*,", text):
            kind, key = m.group(1).lower(), m.group(2).strip()
            body, _ = read_group(text, text.index("{", m.start()))
            body = body[body.index(",") + 1:] if "," in body else ""
            if key in entries:
                duplicates.append(key)
                continue  # biber keeps the first definition and skips the rest
            entries[key] = {
                "key": key,
                "type": kind,
                "title": _bib_clean(_bib_field(body, "title")),
                "author": _bib_clean(_bib_field(body, "author")),
                "year": _bib_clean(_bib_field(body, "year")),
                "journal": _bib_clean(_bib_field(body, "journal")
                                      or _bib_field(body, "booktitle")
                                      or _bib_field(body, "publisher")),
                "doi": _bib_clean(_bib_field(body, "doi")),
                "url": _bib_clean(_bib_field(body, "url")),
                "file": path.name,
            }
    return entries, duplicates


def _bib_field(body, name):
    m = re.search(r"(?:^|[,{\s])" + name + r"\s*=\s*", body, re.I)
    if not m:
        return ""
    i = m.end()
    if i < len(body) and body[i] == "{":
        inner, _ = read_group(body, i)
        return inner
    if i < len(body) and body[i] == '"':
        j = body.find('"', i + 1)
        return body[i + 1:j] if j > 0 else body[i + 1:]
    j = body.find(",", i)
    return body[i:j if j >= 0 else len(body)]


def _bib_clean(value):
    value = re.sub(r"[{}]", "", value or "")
    value = value.replace("\\&", "&").replace("--", "–")
    return re.sub(r"\s+", " ", value).strip()


def bib_authors_short(author):
    """'Kumar, Arvind and Rotter, Stefan' -> 'Kumar, Rotter'."""
    names = [a.strip() for a in re.split(r"\s+and\s+", author or "") if a.strip()]
    surnames = []
    for name in names:
        surnames.append(name.split(",")[0].strip() if "," in name
                        else name.split()[-1] if name.split() else name)
    if len(surnames) > 3:
        return ", ".join(surnames[:3]) + " et al."
    return ", ".join(surnames)


def format_reference(entry):
    """A compact one-line reference, in the spirit of the IEEE style."""
    bits = [b for b in (bib_authors_short(entry.get("author")),
                        entry.get("title"), entry.get("journal"),
                        entry.get("year")) if b]
    return ", ".join(bits)


def make_bibtex(paper, key):
    """Render a library paper as a BibTeX entry for appending to a .bib file."""
    def field(name, value):
        value = re.sub(r"[{}]", "", str(value or "")).strip()
        return f"  {name:<8}= {{{value}}},\n" if value else ""

    entry = "@article{" + key + ",\n"
    entry += field("title", paper.get("title"))
    entry += field("author", _authors_to_bibtex(paper.get("authors")))
    entry += field("journal", paper.get("venue"))
    entry += field("year", paper.get("year"))
    entry += field("note", paper.get("citation"))
    return entry.rstrip(",\n") + "\n}\n"


def _authors_to_bibtex(authors):
    """'A. Kumar, S. Rotter, and A. Aertsen' -> 'A. Kumar and S. Rotter and ...'"""
    text = re.sub(r",?\s+and\s+", ", ", authors or "")
    parts = [p.strip(" .,&") for p in text.split(",")]
    parts = [p for p in parts if p and not re.fullmatch(r"[A-Z]\.?( ?[A-Z]\.?)*", p)]
    return " and ".join(parts)


def suggest_key(paper, taken=()):
    """Build a bibkey like 'maass1997networks' that no other entry uses."""
    authors = paper.get("authors") or ""
    first = re.split(r",| and ", authors)[0].strip()
    surname = first.split()[-1] if first.split() else "ref"
    surname = re.sub(r"[^A-Za-z]", "", surname).lower() or "ref"
    year = re.sub(r"[^0-9]", "", str(paper.get("year") or ""))[:4]
    stop = {"the", "a", "an", "of", "on", "for", "and", "in", "with", "to"}
    words = [w for w in re.findall(r"[A-Za-z]+", paper.get("title") or "")
             if w.lower() not in stop]
    word = words[0].lower() if words else "paper"
    base = f"{surname}{year}{word}"
    key, n = base, 2
    while key in taken:
        key, n = f"{base}{n}", n + 1
    return key


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

class Renderer:
    """Turns the document body into HTML, in two passes.

    Pass one assigns numbers to headings and equations so that \\ref{} can be
    resolved; pass two emits the HTML. Citation numbers are handed out in
    order of first appearance, which is what biblatex's `sorting=none` does,
    so the numbers here match the numbers in the compiled PDF.
    """

    def __init__(self, thesis, bib):
        self.thesis = thesis
        self.bib = bib
        self.reset()

    def reset(self):
        self.labels = {}
        self.toc = []
        self.cites = {}
        self.cite_order = []
        self.cite_sites = []
        self.todos = []
        self.counters = [0] * len(SECTIONS)
        self.eq_counter = 0
        self.anchors = set()
        self.collect = True

    # ------------------------------------------------------------- numbering
    def bump(self, level):
        self.counters[level] += 1
        for deeper in range(level + 1, len(self.counters)):
            self.counters[deeper] = 0
        if level <= 1:
            self.eq_counter = 0
        parts = [str(self.counters[i]) for i in range(1, level + 1)]
        return ".".join(p for p in parts if p != "0") if level >= 1 else ""

    def anchor_for(self, label, title):
        base = label or re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-") or "sec"
        anchor, n = base, 2
        while anchor in self.anchors:
            anchor, n = f"{base}-{n}", n + 1
        self.anchors.add(anchor)
        return anchor

    # ----------------------------------------------------------------- entry
    def render(self, body):
        self.reset()
        self.walk(body)                 # pass 1: labels, numbers, anchors, todos
        labels, todos = self.labels, self.todos
        self.reset()
        self.labels = labels            # so \\ref{} can resolve forward references
        self.collect = False
        html = self.walk(body)          # pass 2: emit
        self.todos = todos
        return html

    # ------------------------------------------------------------- structure
    def walk(self, src):
        out = []
        pending_head = None
        for kind, payload in self.blocks(src):
            if kind == "text":
                text = payload
                if pending_head is not None:
                    label, text = self.take_label(text)
                    out.append(self.heading(*pending_head, label))
                    pending_head = None
                out.append(self.paragraphs(text))
            elif kind == "head":
                if pending_head is not None:
                    out.append(self.heading(*pending_head, ""))
                pending_head = payload
            else:
                if pending_head is not None:
                    out.append(self.heading(*pending_head, ""))
                    pending_head = None
                out.append(self.environment(*payload))
        if pending_head is not None:
            out.append(self.heading(*pending_head, ""))
        return "".join(out)

    def blocks(self, src):
        """Split source into text runs, headings, and environments."""
        pattern = re.compile(
            r"\\(" + "|".join(SECTIONS) + r")\*?\s*(?=[\[{])"
            r"|\\begin\s*\{([A-Za-z*]+)\}")
        i = 0
        while i < len(src):
            m = pattern.search(src, i)
            if not m:
                break
            if m.start() > i:
                yield ("text", src[i:m.start()])
            if m.group(1):
                j = m.end()
                _short, j = read_optional(src, j)
                title, j = read_group(src, j)
                level = SECTIONS.index(m.group(1))
                starred = src[m.start():m.end()].rstrip().endswith("*")
                yield ("head", (level, title, starred))
                i = j
            else:
                name = m.group(2)
                inner, j = self.read_environment(src, m.end(), name)
                yield ("env", (name, inner))
                i = j

    def read_environment(self, src, start, name):
        """Return (inner, index_after_end) for \\begin{name} ... \\end{name}."""
        opener = re.compile(r"\\begin\s*\{" + re.escape(name) + r"\}")
        closer = re.compile(r"\\end\s*\{" + re.escape(name) + r"\}")
        depth, i = 1, start
        while i < len(src):
            nxt_open = opener.search(src, i)
            nxt_close = closer.search(src, i)
            if not nxt_close:
                return src[start:], len(src)
            if nxt_open and nxt_open.start() < nxt_close.start():
                depth += 1
                i = nxt_open.end()
                continue
            depth -= 1
            if depth == 0:
                return src[start:nxt_close.start()], nxt_close.end()
            i = nxt_close.end()
        return src[start:], len(src)

    def take_label(self, text):
        """Pull a \\label{} off the front of a text run (it names the heading)."""
        m = re.match(r"\s*\\label\s*\{([^}]*)\}", text)
        return (m.group(1).strip(), text[m.end():]) if m else ("", text)

    # -------------------------------------------------------------- emitters
    def heading(self, level, title, starred, label):
        number = "" if starred else self.bump(level)
        text = self.inline(title)
        anchor = self.anchor_for(label, re.sub(r"<[^>]+>", "", text))
        if label:
            self.labels.setdefault(label, {"number": number, "anchor": anchor,
                                           "title": re.sub(r"<[^>]+>", "", text),
                                           "kind": "section"})
        self.toc.append({"anchor": anchor, "level": level, "number": number,
                         "title": re.sub(r"<[^>]+>", "", text)})
        tag = f"h{min(level + 1, 6)}"
        num = f'<span class="tex-num">{esc(number)}</span> ' if number else ""
        return (f'<{tag} id="{esc(anchor)}" class="tex-head tex-h{level}">'
                f'{num}{text}</{tag}>')

    def paragraphs(self, text):
        if not text.strip():
            return ""
        out = []
        for chunk in re.split(r"\n\s*\n", text):
            if not chunk.strip():
                continue
            html = self.inline(chunk)
            if html.strip():
                out.append(f"<p>{html}</p>")
        return "".join(out)

    def environment(self, name, inner):
        if name in MATH_ENVS:
            return self.math_block(name, inner)
        if name in ("itemize", "enumerate", "description"):
            return self.list_block(name, inner)
        if name == "figure" or name == "figure*":
            return self.figure_block(inner)
        if name in ("table", "table*"):
            return f'<div class="tex-table-wrap">{self.walk(inner)}</div>'
        if name == "tabular":
            return self.tabular_block(inner)
        if name in ("verbatim", "lstlisting", "minted"):
            return f'<pre class="tex-verbatim">{esc(inner.strip())}</pre>'
        if name in ("quote", "quotation", "abstract"):
            return f"<blockquote>{self.walk(inner)}</blockquote>"
        if name == "titlepage":
            return f'<div class="tex-titlepage">{self.walk(inner)}</div>'
        if name == "center":
            return f'<div class="tex-center">{self.walk(inner)}</div>'
        if name == "document":
            return self.walk(inner)
        return self.walk(inner)

    def math_block(self, name, inner):
        label = ""
        m = re.search(r"\\label\s*\{([^}]*)\}", inner)
        if m:
            label = m.group(1).strip()
            inner = inner[:m.start()] + inner[m.end():]
        number = ""
        if not name.endswith("*"):
            self.eq_counter += 1
            chapter = self.counters[1]
            number = f"{chapter}.{self.eq_counter}" if chapter else str(self.eq_counter)
            if label:
                self.labels.setdefault(label, {"number": number, "anchor": "eq-" + label,
                                               "title": "Equation " + number,
                                               "kind": "equation"})
        anchor = f' id="eq-{esc(label)}"' if label else ""
        tag = f'<span class="tex-eqno">({esc(number)})</span>' if number else ""
        return (f'<div class="tex-equation"{anchor}>'
                f'<div class="tex-math">{self.math(inner)}</div>{tag}</div>')

    def list_block(self, name, inner):
        tag = "ol" if name == "enumerate" else "ul"
        items = re.split(r"\\item\b", inner)
        head = items[0]
        body = []
        for item in items[1:]:
            _opt, j = read_optional(item, skip_space(item, 0))
            content = item[j:] if j else item
            body.append(f"<li>{self.walk(content)}</li>")
        lead = self.paragraphs(head) if head.strip() else ""
        return lead + (f"<{tag} class=\"tex-list\">{''.join(body)}</{tag}>"
                       if body else "")

    def figure_block(self, inner):
        parts = []
        for m in re.finditer(r"\\includegraphics\s*(?:\[[^\]]*\])?\s*\{([^}]*)\}", inner):
            rel = m.group(1).strip()
            found = self.thesis.safe_path(rel, extensions=(".png", ".jpg", ".jpeg",
                                                           ".gif", ".svg", ".pdf"))
            if found:
                url = "/api/thesis/asset/" + _url_quote(
                    str(found.relative_to(self.thesis.dir)))
                if found.suffix.lower() == ".pdf":
                    parts.append(f'<a class="tex-figure-pdf" href="{url}" '
                                 f'target="_blank" rel="noopener">{esc(rel)} (PDF figure)</a>')
                else:
                    parts.append(f'<img src="{url}" alt="{esc(rel)}" loading="lazy">')
            else:
                parts.append(f'<div class="tex-missing">missing figure: {esc(rel)}</div>')
        caption = ""
        m = re.search(r"\\caption\s*(?:\[[^\]]*\])?\s*\{", inner)
        if m:
            text, _ = read_group(inner, m.end() - 1)
            caption = f"<figcaption>{self.inline(text)}</figcaption>"
        nested = re.search(r"\\begin\s*\{tabular\}", inner)
        if nested:
            body, end = self.read_environment(inner, nested.end(), "tabular")
            parts.append(self.tabular_block(body))
        return f'<figure class="tex-figure">{"".join(parts)}{caption}</figure>'

    def tabular_block(self, inner):
        inner = re.sub(r"^\s*\{[^{}]*\}", "", inner.strip(), count=1)  # column spec
        inner = re.sub(r"\\(?:toprule|midrule|bottomrule|hline|"
                       r"cmidrule\s*(?:\([^)]*\))?\s*\{[^}]*\})", "", inner)
        rows = [r for r in re.split(r"\\\\", inner) if r.strip()]
        html = []
        for n, row in enumerate(rows):
            cells = [self.inline(c) for c in row.split("&")]
            tag = "th" if n == 0 else "td"
            html.append("<tr>" + "".join(f"<{tag}>{c}</{tag}>" for c in cells) + "</tr>")
        return (f'<div class="tex-table-scroll"><table class="tex-table">'
                f'{"".join(html)}</table></div>')

    # ---------------------------------------------------------------- inline
    def inline(self, src):
        out, i = [], 0
        while i < len(src):
            ch = src[i]

            if ch == "%":
                i += 1
                continue

            if ch == "$":
                if src.startswith("$$", i):
                    end = src.find("$$", i + 2)
                    end = len(src) if end < 0 else end
                    out.append(f'<span class="tex-math">{self.math(src[i + 2:end])}</span>')
                    i = end + 2
                    continue
                end = i + 1
                while end < len(src):
                    if src[end] == "\\":
                        end += 2
                        continue
                    if src[end] == "$":
                        break
                    end += 1
                out.append(f'<span class="tex-math">{self.math(src[i + 1:end])}</span>')
                i = end + 1
                continue

            if ch == "\\":
                consumed, html = self.command(src, i)
                if consumed:
                    out.append(html)
                    i += consumed
                    continue
                out.append(esc(src[i:i + 2]))
                i += 2
                continue

            if ch == "~":
                out.append("&nbsp;")
                i += 1
                continue

            if src.startswith("---", i):
                out.append("—")
                i += 3
                continue
            if src.startswith("--", i):
                out.append("–")
                i += 2
                continue
            if src.startswith("``", i):
                out.append("&ldquo;")
                i += 2
                continue
            if src.startswith("''", i):
                out.append("&rdquo;")
                i += 2
                continue

            out.append(esc(ch))
            i += 1
        return re.sub(r"[ \t]*\n[ \t]*", " ", "".join(out)).strip()

    def command(self, src, i):
        """Render one \\command at src[i]. Returns (chars_consumed, html)."""
        m = re.match(r"\\([A-Za-z@]+)\*?", src[i:])
        if not m:
            escaped = re.match(r"\\([&%$#_{}])", src[i:])
            if escaped:
                return 2, esc(escaped.group(1))
            spacing = re.match(r"\\([ ,;:!@])", src[i:])
            if spacing:
                return 2, ("" if spacing.group(1) in "!@" else " ")
            if src.startswith("\\\\", i):
                return 2, "<br>"
            if src.startswith("\\[", i):
                end = src.find("\\]", i)
                end = len(src) if end < 0 else end
                return (end + 2 - i,
                        f'<div class="tex-equation"><div class="tex-math">'
                        f'{self.math(src[i + 2:end])}</div></div>')
            return 0, ""

        name = m.group(1)
        j = i + m.end()

        def arg(index=j):
            k = skip_space(src, index)
            if k < len(src) and src[k] == "{":
                inner, end = read_group(src, k)
                return inner, end
            return "", index

        if name in DROP_COMMANDS:
            return j - i, ""

        if name in NOTE_MACROS:
            text, end = arg()
            kind = NOTE_MACROS[name]
            label = {"todo": "TODO", "note": "NOTE", "cne": "CITE?"}[kind]
            body = self.inline(text) if text else ""
            if self.collect:
                self.todos.append({"kind": kind,
                                   "text": re.sub(r"<[^>]+>", "", body) or label,
                                   "section": self.toc[-1]["anchor"] if self.toc else ""})
            return end - i, (f'<span class="tex-note tex-note-{kind}">'
                             f'<b>{label}</b>{(": " + body) if body else ""}</span>')

        if name in ("cite", "citep", "citet", "parencite", "textcite", "autocite"):
            _opt, k = read_optional(src, j)
            _opt2, k = read_optional(src, k)
            keys, end = arg(k)
            return end - i, self.citation(keys)

        if name == "label":
            key, end = arg()
            return end - i, f'<span id="{esc("lbl-" + key.strip())}"></span>'

        if name in ("ref", "cref", "Cref", "autoref", "eqref", "pageref", "nameref"):
            key, end = arg()
            return end - i, self.reference(name, key.strip())

        if name in ("url",):
            target, end = arg()
            safe = _safe_url(target.strip())
            return end - i, (f'<a href="{esc(safe)}" target="_blank" rel="noopener">'
                             f'{esc(target.strip())}</a>' if safe else esc(target))

        if name == "href":
            target, k = arg()
            text, end = arg(k)
            safe = _safe_url(target.strip())
            body = self.inline(text)
            return end - i, (f'<a href="{esc(safe)}" target="_blank" rel="noopener">'
                             f'{body}</a>' if safe else body)

        if name in ("textbf", "bfseries", "bf"):
            text, end = arg()
            return (end - i, f"<strong>{self.inline(text)}</strong>") if text else (j - i, "")

        if name in ("textit", "emph", "itshape", "em"):
            text, end = arg()
            return (end - i, f"<em>{self.inline(text)}</em>") if text else (j - i, "")

        if name in ("texttt", "ttfamily", "verb"):
            text, end = arg()
            return (end - i, f"<code>{self.inline(text)}</code>") if text else (j - i, "")

        if name == "textsc":
            text, end = arg()
            return end - i, f'<span class="tex-sc">{self.inline(text)}</span>'

        if name == "underline":
            text, end = arg()
            return end - i, f"<u>{self.inline(text)}</u>"

        if name == "footnote":
            text, end = arg()
            return end - i, (f'<span class="tex-footnote" title="{esc(_plain(text))}">'
                             f'[note]</span>')

        if name == "textcolor":
            _color, k = arg()
            text, end = arg(k)
            return end - i, self.inline(text)

        if name in ("SI", "si", "num"):
            value, k = arg()
            unit, end = arg(k)
            return end - i, f'<span class="tex-math">{self.math(value + " " + unit)}</span>'

        if name in ("printbibliography", "bibliography"):
            _opt, k = read_optional(src, j)
            _arg, end = arg(k)
            return max(end, j) - i, '<div id="tex-references"></div>'

        if name in ("caption", "includegraphics", "graphicspath", "addbibresource"):
            _opt, k = read_optional(src, j)
            _a, end = arg(k)
            return max(end, j) - i, ""

        if name in ("vspace", "hspace", "setlength", "addtolength", "captionsetup",
                    "hypersetup", "usepackage", "documentclass", "newcommand",
                    "renewcommand", "date", "author", "title"):
            _opt, k = read_optional(src, j)
            _a, k2 = arg(k)
            _b, end = arg(k2)
            return max(end, j) - i, ""

        # Size/style switches and anything else with no argument: drop the
        # command, keep whatever follows it.
        text, end = arg()
        if text:
            return end - i, self.inline(text)
        return j - i, ""

    def citation(self, keys):
        out = []
        for key in [k.strip() for k in (keys or "").split(",") if k.strip()]:
            if key not in self.cites:
                entry = self.bib.get(key)
                self.cites[key] = {"key": key, "number": len(self.cite_order) + 1,
                                   "known": entry is not None,
                                   "entry": entry}
                self.cite_order.append(key)
            info = self.cites[key]
            here = self.toc[-1] if self.toc else None
            self.cite_sites.append({
                "key": key,
                "anchor": here["anchor"] if here else "",
                "number": here["number"] if here else "",
                "title": here["title"] if here else "(front matter)"})
            cls = "tex-cite" if info["known"] else "tex-cite tex-cite-unknown"
            out.append(f'<a class="{cls}" href="#" data-bibkey="{esc(key)}" '
                       f'role="button">[{info["number"]}]</a>')
        return "".join(out)

    def reference(self, kind, key):
        target = self.labels.get(key)
        if not target:
            return f'<span class="tex-ref tex-ref-missing" title="unresolved \\ref">??</span>'
        number = target["number"] or "?"
        anchor = target["anchor"] if target["kind"] == "section" else "eq-" + key
        prefix = ""
        if kind in ("cref", "Cref", "autoref"):
            word = "Equation" if target["kind"] == "equation" else "Section"
            prefix = (word if kind in ("Cref", "autoref") else word.lower()) + " "
        text = f"({number})" if kind == "eqref" else f"{prefix}{number}"
        return f'<a class="tex-ref" href="#{esc(anchor)}">{esc(text)}</a>'

    def math(self, src):
        text = MATH_NOISE.sub("", src)
        text = re.sub(r"\\(?:mathbf|mathrm|mathit|bm|boldsymbol|mathcal|text|"
                      r"textrm|mathbb|operatorname)\s*\{([^{}]*)\}", r"\1", text)
        text = re.sub(r"\\frac\s*\{([^{}]*)\}\s*\{([^{}]*)\}", r"(\1)/(\2)", text)
        text = re.sub(r"\\sqrt\s*\{([^{}]*)\}", r"√(\1)", text)
        for name in sorted(SYMBOLS, key=len, reverse=True):
            text = text.replace(name, SYMBOLS[name])
        text = re.sub(r"\\\\", " ", text)
        text = re.sub(r"\\[A-Za-z@]+", "", text)
        text = re.sub(r"\s+", " ", text).strip()

        out, i = [], 0
        while i < len(text):
            ch = text[i]
            if ch in "{}":
                i += 1
                continue
            if ch in "^_" and i + 1 < len(text):
                run, i = _script_run(text, i + 1)
                table = SUPERSCRIPTS if ch == "^" else SUBSCRIPTS
                if run and all(ord(c) in table for c in run):
                    out.append(run.translate(table))
                else:
                    tag = "sup" if ch == "^" else "sub"
                    out.append(f"<{tag}>{esc(run)}</{tag}>")
                continue
            out.append(esc(ch))
            i += 1
        return "".join(out)


def _script_run(text, i):
    """Read the argument of ^ or _ : either a brace group or one character."""
    if i < len(text) and text[i] == "{":
        inner, end = read_group(text, i)
        return inner, end
    return (text[i], i + 1) if i < len(text) else ("", i)


def _plain(text):
    return re.sub(r"\s+", " ", re.sub(r"\\[A-Za-z@]+|[{}]", "", text or "")).strip()


def _safe_url(url):
    return url if re.match(r"^(https?://|mailto:)", url or "", re.I) else ""


def _url_quote(path):
    from urllib.parse import quote
    return quote(path)


# --------------------------------------------------------------------------
# public entry point
# --------------------------------------------------------------------------

def build(thesis):
    """Read the thesis off disk and render it. Returns a plain dict."""
    raw = strip_comments(_read(thesis.tex))
    raw = resolve_inputs(thesis, raw)

    m = re.search(r"\\begin\s*\{document\}", raw)
    end = re.search(r"\\end\s*\{document\}", raw)
    preamble = raw[:m.start()] if m else raw
    body = raw[m.end():end.start() if end else len(raw)] if m else raw

    macros = parse_macros(preamble)
    body = expand_macros(body, macros)

    entries, duplicates = parse_bib(thesis.bib_paths(raw))

    renderer = Renderer(thesis, entries)
    html = renderer.render(body)

    cites = [dict(renderer.cites[k], entry=renderer.cites[k]["entry"])
             for k in renderer.cite_order]
    return {
        "html": html,
        "toc": renderer.toc,
        "cites": cites,
        "bib": entries,
        "duplicates": sorted(set(duplicates)),
        "todos": renderer.todos,
        "sites": renderer.cite_sites,
        "bib_files": [str(p) for p in thesis.bib_paths(raw)],
    }


def toc_pages(thesis):
    """Section -> PDF page number, read from the .toc LaTeX writes."""
    pages = {}
    for m in re.finditer(
            r"\\contentsline\s*\{(\w+)\}\{(?:\\numberline\s*\{([^}]*)\})?"
            r"(.*?)\}\{(\d+)\}", _read(thesis.toc)):
        number = (m.group(2) or "").strip()
        if number:
            pages[number] = int(m.group(4))
    return pages
