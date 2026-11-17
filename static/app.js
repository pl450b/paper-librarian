/* Paper Librarian — frontend. No framework, no build step. */

const $ = (s, root = document) => root.querySelector(s);
const $$ = (s, root = document) => [...root.querySelectorAll(s)];
const PAGE = 60;

const state = {
  q: "", sort: "popularity",
  tags: new Set(), keywords: new Set(),
  flag: "", has_pdf: null, has_notes: null, read_status: "", min_pop: 0,
  offset: 0, papers: [], total: 0,
  selected: null, tab: "notes", facets: null,
  panes: null, paneMRU: ["pdf", "notes", "chat"],
  chatLog: [], chatBusy: false, chatLoadedFor: null,
  mode: "library",          // "library" | "thesis" — thesis.js owns the switch
  lib_fulltext: false,      // librarian.js: "read in full by the Librarian" filter
};

/* thesis.js overrides prefix() so opening a paper from the thesis view keeps
   the thesis in the URL instead of replacing it. */
const route = { prefix: () => "#" };

const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) =>
  ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

function toast(msg) {
  const el = $("#toast");
  el.textContent = msg;
  el.hidden = false;
  clearTimeout(toast._t);
  toast._t = setTimeout(() => { el.hidden = true; }, 2200);
}

async function api(path, opts = {}) {
  const res = await fetch(path, {
    headers: opts.body ? { "Content-Type": "application/json" } : {},
    ...opts,
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.error || `${res.status} ${res.statusText}`);
  }
  return res.status === 204 ? null : res.json();
}

/* ------------------------------------------------------------------ query */

function queryString() {
  const p = new URLSearchParams();
  if (state.q) p.set("q", state.q);
  p.set("sort", state.sort);
  state.tags.forEach((v) => p.append("tag", v));
  state.keywords.forEach((v) => p.append("keyword", v));
  if (state.flag) p.set("flag", state.flag);
  if (state.min_pop) p.set("min_pop", state.min_pop);
  if (state.has_pdf !== null) p.set("has_pdf", state.has_pdf ? "1" : "0");
  if (state.has_notes !== null) p.set("has_notes", state.has_notes ? "1" : "0");
  if (state.read_status) p.set("read_status", state.read_status);
  if (state.lib_fulltext) p.set("lib_fulltext", "1");
  p.set("limit", PAGE);
  p.set("offset", state.offset);
  return p.toString();
}

async function load({ append = false } = {}) {
  if (!append) state.offset = 0;
  // librarian.js: semantic search replaces the keyword list while it's on.
  if (state.semantic && state.q && window.librarianSearch) return window.librarianSearch();
  const data = await api("/api/papers?" + queryString());
  state.papers = append ? state.papers.concat(data.papers) : data.papers;
  state.total = data.total;
  renderList();
  renderActiveFilters();
  syncFacetPressed();
}

const debounce = (fn, ms) => {
  let t;
  return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); };
};

/* --------------------------------------------------------------- sidebar */

function renderFacets(f) {
  state.facets = f;
  const s = f.stats;
  $("#statline").textContent =
    `${s.total} papers · ${s.noted} with notes · ${s.pdfs} PDFs · ${s.multi} multi-cited`;

  $("#quick").innerHTML = [
    ["all", "Everything", s.total],
    ["notes", "Has my notes", s.noted],
    ["pdf", "Has a PDF", s.pdfs],
    ["multi", "Cited 2+ times", s.multi],
    ["draft", "Cited in my draft", s.draft],
    ["reading", "Currently reading", ""],
    ["libtext", "Full text (Librarian)", ""],
  ].map(([k, label, n]) =>
    `<button data-quick="${k}" aria-pressed="false"${k === "libtext"
       ? ` hidden id="quick-libtext" title="Read in full by the Librarian from an open-access PDF or arXiv, though no PDF is linked in papers/"` : ""}>
       <span class="name">${label}</span><span class="count">${n}</span></button>`).join("");

  const facet = (id, items, key) => {
    $(id).innerHTML = items.map((it) =>
      `<button data-facet="${key}" data-value="${esc(it.name)}" aria-pressed="false"
               title="${esc(it.label || it.name)}">
         <span class="name">${esc(it.name)}</span>
         <span class="count">${it.count}</span></button>`).join("")
      || `<div class="count" style="padding:4px 8px">none yet</div>`;
  };
  facet("#facet-tags", f.tags, "tags");
  facet("#facet-keywords", f.keywords, "keywords");

  $("#all-tags").innerHTML = f.tags.map((t) => `<option value="${esc(t.name)}">`).join("");
  syncFacetPressed();
}

function syncFacetPressed() {
  $$("[data-facet]").forEach((b) => {
    b.setAttribute("aria-pressed", state[b.dataset.facet].has(b.dataset.value));
  });
  $$(".filter-group").forEach((g) => {
    const key = $(".clear", g)?.dataset.clear;
    g.classList.toggle("has-active", !!key && state[key].size > 0);
  });
  const quickActive = {
    all: !state.flag && !state.min_pop && state.has_pdf === null
         && state.has_notes === null && !state.read_status && !state.lib_fulltext,
    notes: state.has_notes === true,
    pdf: state.has_pdf === true,
    multi: state.min_pop >= 2,
    draft: state.flag === "draft",
    reading: state.read_status === "reading",
    libtext: state.lib_fulltext,
  };
  $$("[data-quick]").forEach((b) => {
    b.setAttribute("aria-pressed", !!quickActive[b.dataset.quick]);
  });
}

function toggleFacet(key, value) {
  const set = state[key];
  set.has(value) ? set.delete(value) : set.add(value);
  load();
}

/* ------------------------------------------------------------------ list */

function heatChip(n) {
  if (!n) return "";
  return `<span class="heat ${n >= 4 ? "h4" : n < 2 ? "h1" : ""}">${n}×</span>`;
}

function cardHTML(p) {
  const chips = [
    p.pdf_path ? `<span class="chip pdf">PDF</span>` : "",
    p.has_notes ? `<span class="chip note">notes</span>` : "",
    window.libChip ? window.libChip(p) : "",
    ...p.tags.map((t) => `<span class="chip tag">${esc(t)}</span>`),
    ...p.keywords.slice(0, 3).map((k) => `<span class="chip kw">${esc(k)}</span>`),
  ].filter(Boolean).join("");

  return `<article class="card${state.selected === p.id ? " selected" : ""}"
                   data-id="${p.id}" tabindex="0">
    <div class="card-top">
      ${heatChip(p.popularity)}
      <div class="ttl">${esc(p.title || p.citation.slice(0, 140))}</div>
      <span class="year">${esc(p.year)}</span>
    </div>
    ${p.authors ? `<div class="authors">${esc(p.authors)}</div>` : ""}
    <div class="row">${chips}</div>
  </article>`;
}

function renderList() {
  const list = $("#list");
  if (!state.papers.length) {
    list.innerHTML = `<div class="empty">Nothing matches those filters.</div>`;
  } else {
    list.innerHTML = state.papers.map(cardHTML).join("");
  }
  $("#result-count").textContent =
    `${state.total} result${state.total === 1 ? "" : "s"}`;
  $("#load-more").hidden = state.papers.length >= state.total;
}

function renderActiveFilters() {
  const pills = [];
  const add = (key, value, label) =>
    pills.push(`<span class="pill">${esc(label)}
      <button data-remove="${key}" data-value="${esc(value)}" title="Remove">✕</button></span>`);
  state.tags.forEach((v) => add("tags", v, `tag: ${v}`));
  state.keywords.forEach((v) => add("keywords", v, v));
  $("#active-filters").innerHTML = pills.join("");
}

/* ---------------------------------------------------------------- detail */

/* ------------------------------------------------------------------- math */

/* Same vocabulary as thesis.py's SYMBOLS, so \tau means the same thing whether
   you read it in the thesis pane or in your own notes. Keyed without the
   backslash. */
const TEX_SYM = {
  alpha: "α", beta: "β", gamma: "γ", delta: "δ", epsilon: "ε", varepsilon: "ε",
  zeta: "ζ", eta: "η", theta: "θ", vartheta: "ϑ", iota: "ι", kappa: "κ",
  lambda: "λ", mu: "μ", nu: "ν", xi: "ξ", pi: "π", rho: "ρ", sigma: "σ",
  tau: "τ", upsilon: "υ", phi: "φ", varphi: "φ", chi: "χ", psi: "ψ", omega: "ω",
  Gamma: "Γ", Delta: "Δ", Theta: "Θ", Lambda: "Λ", Xi: "Ξ", Pi: "Π",
  Sigma: "Σ", Upsilon: "Υ", Phi: "Φ", Psi: "Ψ", Omega: "Ω",
  sum: "∑", prod: "∏", int: "∫", oint: "∮", partial: "∂", infty: "∞",
  nabla: "∇", cdot: "·", cdots: "⋯", ldots: "…", dots: "…", vdots: "⋮",
  times: "×", div: "÷", pm: "±", mp: "∓", ast: "∗", star: "⋆", circ: "∘",
  leq: "≤", le: "≤", geq: "≥", ge: "≥", neq: "≠", ne: "≠", approx: "≈",
  equiv: "≡", propto: "∝", sim: "∼", simeq: "≃", ll: "≪", gg: "≫",
  in: "∈", notin: "∉", subset: "⊂", subseteq: "⊆", supset: "⊃",
  supseteq: "⊇", cup: "∪", cap: "∩", emptyset: "∅", setminus: "∖",
  forall: "∀", exists: "∃", neg: "¬", land: "∧", lor: "∨",
  rightarrow: "→", to: "→", leftarrow: "←", leftrightarrow: "↔",
  Rightarrow: "⇒", Leftarrow: "⇐", Leftrightarrow: "⇔", mapsto: "↦",
  top: "⊤", bot: "⊥", perp: "⊥", angle: "∠", prime: "′", hbar: "ħ", ell: "ℓ",
  oplus: "⊕", otimes: "⊗", parallel: "∥", therefore: "∴", because: "∵",
  quad: "\u2003", qquad: "\u2003\u2003",
  lvert: "|", rvert: "|", lVert: "\u2016", rVert: "\u2016",
  langle: "\u27e8", rangle: "\u27e9",
  lceil: "\u2308", rceil: "\u2309", lfloor: "\u230a", rfloor: "\u230b",
  ",": "\u2009", ";": "\u2005", ":": "\u2005", "!": "", " ": " ",
  "{": "{", "}": "}", "%": "%", "&": "&", _: "_", "#": "#", $: "$",
  "|": "\u2016",                                  // \| is the double bar
};

/* Names that set upright, like \sin — not variables. */
const TEX_FUNC = new Set(["sin", "cos", "tan", "arcsin", "arccos", "arctan",
  "sinh", "cosh", "tanh", "log", "ln", "exp", "min", "max", "arg", "argmax",
  "argmin", "det", "dim", "ker", "deg", "gcd", "lim", "sup", "inf", "mod"]);

const TEX_BB = { R: "ℝ", N: "ℕ", Z: "ℤ", Q: "ℚ", C: "ℂ", E: "𝔼", P: "ℙ" };

/* Commands whose only job is sizing/spacing; drop them but keep the argument. */
const TEX_DROP = new Set(["left", "right", "bigl", "bigr", "Bigl", "Bigr",
  "biggl", "biggr", "displaystyle", "limits", "nolimits", "middle"]);

/* A small TeX subset for reading notes: symbols, sub/superscripts, fractions,
   roots, and the usual operators. Not a typesetter — no dependency is worth
   more than this whole app — but enough that $\tau_m \frac{dV}{dt}$ reads as
   itself instead of as source. Unknown commands are printed verbatim rather
   than swallowed, so nothing you typed silently disappears. */
function texToHTML(src) {
  let out = "", i = 0;

  // Raw source of the next {group} or single token, without rendering it.
  const readRaw = () => {
    while (src[i] === " ") i++;
    if (src[i] === "{") {
      const start = ++i;
      let depth = 1;
      while (i < src.length && depth) {
        if (src[i] === "\\") { i += 2; continue; }
        if (src[i] === "{") depth++;
        else if (src[i] === "}") depth--;
        i++;
      }
      return src.slice(start, i - (depth ? 0 : 1));
    }
    if (src[i] === "\\") {
      const start = i++;
      while (i < src.length && /[a-zA-Z]/.test(src[i])) i++;
      if (i === start + 1) i++;
      return src.slice(start, i);
    }
    return src[i++] ?? "";
  };
  const arg = () => texToHTML(readRaw());

  while (i < src.length) {
    const c = src[i];

    if (c === "\\") {
      const start = i++;
      while (i < src.length && /[a-zA-Z]/.test(src[i])) i++;
      let name = src.slice(start + 1, i);
      if (!name) { name = src[i] ?? ""; i++; }

      if (name === "\\") { out += "<br>"; continue; }
      if (name === "frac" || name === "dfrac" || name === "tfrac") {
        out += `<span class="frac"><span class="fnum">${arg()}</span>`
             + `<span class="fden">${arg()}</span></span>`;
      } else if (name === "sqrt") {
        let idx = "";
        if (src[i] === "[") {
          const end = src.indexOf("]", i);
          idx = `<span class="sroot">${texToHTML(src.slice(i + 1, end))}</span>`;
          i = end + 1;
        }
        out += `${idx}<span class="radic">√</span><span class="sarg">${arg()}</span>`;
      } else if (name === "mathbb") {
        out += `<span class="mup">${[...readRaw()]
          .map((ch) => TEX_BB[ch] || esc(ch)).join("")}</span>`;
      } else if (name === "text" || name === "mathrm" || name === "textrm"
                 || name === "operatorname" || name === "mathsf") {
        out += `<span class="mup">${arg()}</span>`;
      } else if (name === "mathbf" || name === "bm" || name === "boldsymbol") {
        out += `<span class="mbf">${arg()}</span>`;
      } else if (name === "mathcal" || name === "mathit") {
        out += `<span class="mit">${arg()}</span>`;
      } else if (name === "begin" || name === "end") {
        readRaw();                                   // environment name, dropped
      } else if (TEX_DROP.has(name)) {
        continue;                                    // the delimiter renders itself
      } else if (TEX_FUNC.has(name)) {
        out += `<span class="mop">${name}</span>`;
      } else if (name in TEX_SYM) {
        out += esc(TEX_SYM[name]);
      } else {
        out += esc("\\" + name);
      }
      continue;
    }

    if (c === "^" || c === "_") {
      i++;
      out += c === "^" ? `<sup>${arg()}</sup>` : `<sub>${arg()}</sub>`;
      continue;
    }

    if (c === "{") { out += arg(); continue; }
    if (c === "}") { i++; continue; }
    if (c === "&") { i++; out += " "; continue; }

    if (/[0-9]/.test(c)) {                           // digits stay upright
      const start = i;
      while (i < src.length && /[0-9.]/.test(src[i])) i++;
      out += `<span class="mn">${src.slice(start, i)}</span>`;
      continue;
    }

    if ("+=<>|".includes(c)) {                       // binary ops get air
      out += `<span class="mo">${esc(c)}</span>`;
      i++;
      continue;
    }
    if (c === "-") { out += `<span class="mo">−</span>`; i++; continue; }

    out += esc(c);
    i++;
  }
  return out;
}

function mathHTML(tex, display) {
  let body;
  try {
    body = texToHTML(tex.trim());
  } catch {
    body = esc(tex);                                 // never lose what you typed
  }
  return `<span class="math${display ? " math-display" : ""}">${body}</span>`;
}

/* Deliberately tiny markdown: headings, bold/italic, code, lists, quotes,
   links, and inline TeX. Enough for reading notes, small enough to trust.

   Code spans and math are lifted out into placeholders before anything else
   runs, so `esc()` cannot mangle a `<` inside $a < b$ and `*` inside math is
   never mistaken for emphasis. They are put back, already-rendered, at the end.
   Write \$ for a literal dollar sign. */
function markdown(src) {
  const slots = [];
  // Private-use codepoints survive esc() untouched and cannot occur in
  // anything a human types, so a placeholder can never collide with the text.
  const hold = (html) => "\uE000" + (slots.push(html) - 1) + "\uE001";

  let s = String(src ?? "").replace(/[\uE000-\uE002]/g, "");
  s = s.replace(/\\\$/g, "\uE002");        // \$ = a literal dollar sign
  s = s.replace(/`([^`\n]+)`/g, (_, code) => hold(`<code>${esc(code)}</code>`));
  s = s.replace(/\$\$([\s\S]+?)\$\$/g, (_, t) => hold(mathHTML(t, true)));
  s = s.replace(/\\\[([\s\S]+?)\\\]/g, (_, t) => hold(mathHTML(t, true)));
  s = s.replace(/\$([^$\n]+?)\$/g, (_, t) => hold(mathHTML(t, false)));
  s = s.replace(/\\\(([\s\S]+?)\\\)/g, (_, t) => hold(mathHTML(t, false)));

  const inline = (str) => esc(str)
    .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
    .replace(/(^|\W)\*([^*]+)\*/g, "$1<em>$2</em>")
    .replace(/\[([^\]]+)\]\((https?:[^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');

  const out = [];
  let inList = false;
  for (const line of s.split("\n")) {
    const bullet = line.match(/^\s*[-*]\s+(.*)$/);
    if (bullet) {
      if (!inList) { out.push("<ul>"); inList = true; }
      out.push(`<li>${inline(bullet[1])}</li>`);
      continue;
    }
    if (inList) { out.push("</ul>"); inList = false; }

    const head = line.match(/^(#{1,3})\s+(.*)$/);
    if (head) out.push(`<h${head[1].length}>${inline(head[2])}</h${head[1].length}>`);
    else if (/^\s*>\s?/.test(line)) out.push(`<blockquote>${inline(line.replace(/^\s*>\s?/, ""))}</blockquote>`);
    else if (line.trim()) out.push(`<p>${inline(line)}</p>`);
  }
  if (inList) out.push("</ul>");
  return out.join("")
    .replace(/\uE000(\d+)\uE001/g, (_, n) => slots[+n])
    .replace(/\uE002/g, "$");
}

async function openPaper(id, tab) {
  state.selected = id;
  if (tab) state.tab = tab;
  history.replaceState(null, "", route.prefix() + detailPath(id));
  $("#app").classList.add("with-detail");
  $("#detail").hidden = false;
  $$(".card").forEach((c) => c.classList.toggle("selected", +c.dataset.id === id));
  const paper = await api(`/api/papers/${id}`);
  state.paper = paper;
  if (state.chatLoadedFor !== id) { state.chatLog = []; state.chatLoadedFor = null; }
  renderDetail();
}

/* #paper/<id>/split/<a>+<b> -- which two panes are open is part of the URL,
   so a bookmark reopens the layout you left. A bare .../split still means
   pdf+notes, which is what every older bookmark says. */
function detailPath(id = state.paper?.id ?? state.selected) {
  const tail = state.tab === "split" && state.panes
    ? `split/${state.panes.join("+")}` : state.tab;
  return `paper/${id}/${tail}`;
}

function closeDetail() {
  state.selected = null;
  state.wideBeforeSplit = undefined;
  const home = route.prefix();
  history.replaceState(null, "", home === "#" ? location.pathname : home.replace(/\/$/, ""));
  $("#app").classList.remove("with-detail", "wide");
  $("#detail").hidden = true;
  $$(".card").forEach((c) => c.classList.remove("selected"));
}

function renderDetail() {
  const p = state.paper;
  if (!p) return;
  $$(".tab").forEach((t) => t.setAttribute("aria-pressed", t.dataset.tab === state.tab));
  history.replaceState(null, "", route.prefix() + detailPath(p.id));
  const body = $("#detail-body");
  body.innerHTML =
    { notes: notesPanel, pdf: pdfPanel, split: splitPanel, meta: metaPanel,
      librarian: (x) => (window.librarianPanel ? window.librarianPanel(x) : "") }[state.tab](p);
  // Two columns need the whole window; 44% of it is not a reading surface.
  // Leaving the split puts the layout back the way the reader had it.
  const app = $("#app");
  if (state.tab === "split") {
    if (state.wideBeforeSplit === undefined)
      state.wideBeforeSplit = app.classList.contains("wide");
    app.classList.add("wide");
    applyPanes();
  } else if (state.wideBeforeSplit !== undefined) {
    app.classList.toggle("wide", state.wideBeforeSplit);
    state.wideBeforeSplit = undefined;
  }
  wireDetail(p);
  // thesis.js listens for this to add the "cited in your draft" panel.
  document.dispatchEvent(new CustomEvent("detail:rendered", { detail: p }));
}

function headerHTML(p) {
  return `<div class="dtitle">${esc(p.title || "(untitled)")}</div>
    ${p.authors ? `<div class="dauthors">${esc(p.authors)}</div>` : ""}
    ${p.venue ? `<div class="dvenue">${esc(p.venue)}${p.year ? ` · ${esc(p.year)}` : ""}</div>` : ""}
    <div class="dchips">
      ${p.popularity ? heatChip(p.popularity) : ""}
      ${p.keywords.map((k) => `<span class="chip kw">${esc(k)}</span>`).join("")}
    </div>`;
}

function tagRowHTML(p) {
  return `${p.tags.map((t) => `<span class="chip tag">${esc(t)}
       <button data-untag="${esc(t)}" title="Remove tag">✕</button></span>`).join("")}
    <input class="tag-input" id="tag-input" list="all-tags"
           placeholder="+ tag" autocomplete="off">`;
}

function tagEditorHTML(p) {
  return `<div class="tag-editor">
    <h4>My tags</h4>
    <div class="tag-row">${tagRowHTML(p)}</div>
  </div>`;
}

function notesPanel(p) {
  return `<div class="dpanel">
    ${headerHTML(p)}
    ${tagEditorHTML(p)}
    <div class="notes-head">
      <h4>Notes <span style="text-transform:none;letter-spacing:0">— markdown, autosaved</span></h4>
      <span class="save-state" id="save-state"></span>
    </div>
    <textarea id="notes" placeholder="What does this paper actually say? What did you take from it?"
      spellcheck="true">${esc(p.notes)}</textarea>
    <div class="md-preview" id="md-preview">${markdown(p.notes)}</div>
  </div>`;
}

/* The viewer and the attach form are shared by the PDF tab and the split view,
   so both stay in one place. Only one panel is ever in the DOM at a time, which
   is what lets them keep reusing the same ids. */
function pdfViewerHTML(p, { compact = false } = {}) {
  return `<div class="pdf-bar">
      <span class="path">papers/${esc(p.pdf_path)}</span>
      <a class="btn" href="/pdf/${encodeURI(p.pdf_path)}" target="_blank" rel="noopener">Open tab</a>
      ${compact ? "" : `<button class="btn" id="detach-pdf">Detach</button>`}
    </div>
    <iframe class="pdf-frame" src="/pdf/${encodeURI(p.pdf_path)}#view=FitH"
            title="PDF"></iframe>`;
}

function pdfAttachHTML({ libCopy = false } = {}) {
  return `<div class="pdf-attach">
    <p>${libCopy ? "Attach your own copy to papers/:" : "No PDF attached yet."}</p>
    <select id="pdf-picker"><option value="">— pick a file from papers/ —</option></select>
    <p style="margin-top:18px">…or upload one:</p>
    <input type="file" id="pdf-upload" accept="application/pdf">
  </div>`;
}

function pdfPanel(p) {
  if (p.pdf_path) return `<div class="dpanel pdf">${pdfViewerHTML(p)}</div>`;
  // librarian.js: the Librarian's own copy (cached open-access PDF or arXiv).
  const lib = window.libPdfHTML ? window.libPdfHTML(p) : "";
  return `<div class="dpanel pdf">${lib}${pdfAttachHTML({ libCopy: !!lib })}</div>`;
}

/* Read the paper, write about it, and ask Claude about it -- but only two at
   a time, because three readable columns do not fit on one screen. All three
   panes stay in the DOM and visibility is toggled with CSS: rebuilding the
   markup on every swap would reload the PDF iframe, lose its scroll position,
   and drop the caret out of the notes. Panes sit in fixed grid columns so
   hiding one does not shift the others. */
const PANES = ["pdf", "notes", "chat"];
const PANE_LABEL = { pdf: "PDF", notes: "Notes", chat: "Claude" };

function splitPanel(p) {
  return `<div class="dpanel split-wrap">
    <div class="pane-picker" id="pane-picker">
      <span class="pane-picker-label">Panes</span>
      ${PANES.map((k) => `<button class="pane-btn" data-pane="${k}"
          aria-pressed="false">${PANE_LABEL[k]}</button>`).join("")}
      <div class="split-title" title="${esc(p.title || "")}">${esc(p.title || "(untitled)")}</div>
    </div>
    <div class="split" id="split">
      <div class="split-side" id="pane-pdf">
        ${p.pdf_path ? pdfViewerHTML(p, { compact: true }) : pdfAttachHTML()}
      </div>
      <div class="split-gutter" id="split-gutter-1" role="separator"
           aria-orientation="vertical" tabindex="0"
           title="Drag to resize (double-click to reset)"></div>
      <div class="split-side notes-side" id="pane-notes">
        <div class="split-head">
          <h4 class="pane-heading">Notes</h4>
          <span class="save-state" id="save-state"></span>
          <button class="btn tiny" id="toggle-preview" aria-pressed="false">Preview</button>
        </div>
        <div class="tag-row split-tags">${tagRowHTML(p)}</div>
        <textarea id="notes" spellcheck="true"
          placeholder="What does this paper actually say? What did you take from it?">${esc(p.notes)}</textarea>
        <div class="md-preview split-preview" id="md-preview" hidden>${markdown(p.notes)}</div>
      </div>
      <div class="split-gutter" id="split-gutter-2" role="separator"
           aria-orientation="vertical" tabindex="0"
           title="Drag to resize (double-click to reset)"></div>
      ${chatPaneHTML(p)}
    </div>
  </div>`;
}

/* One remembered width per pair: 55% is right for a PDF beside notes and
   wrong for notes beside a chat log. */
const splitKey = (panes) => `splitWidth:${panes.join("+")}`;
const splitWidth = (panes) => {
  const v = +localStorage.getItem(splitKey(panes));
  return v >= 20 && v <= 80 ? v : 55;
};

const PAIR_KEY = "splitPanes";
function storedPanes() {
  const raw = (localStorage.getItem(PAIR_KEY) || "").split("+");
  return validPanes(raw) ? raw : ["pdf", "notes"];
}
const validPanes = (v) =>
  Array.isArray(v) && v.length === 2 && v[0] !== v[1] && v.every((k) => PANES.includes(k));

/* Grid columns are pane, gutter, pane, gutter, pane. A hidden pane collapses
   to a zero track, and the gutter shown is the one that ends up between the
   two visible panes. */
function applyPanes() {
  const wrap = $("#split");
  if (!wrap) return;
  if (!state.panes) state.panes = storedPanes();
  const [a, b] = state.panes;
  const on = (k) => k === a || k === b;
  state.panes = PANES.filter(on);
  const w = splitWidth(state.panes) + "%";
  const track = (k, first) =>
    (on(k) ? (first ? "var(--split-w)" : "minmax(220px,1fr)") : "0");
  // The left-hand visible pane carries the resizable width.
  const left = PANES.find(on);
  const cols = PANES.map((k) => track(k, k === left));
  const gut1 = on("pdf") ? "7px" : "0";
  const gut2 = on("pdf") ? "0" : "7px";

  wrap.style.setProperty("--split-w", w);
  wrap.style.gridTemplateColumns =
    `${cols[0]} ${gut1} ${cols[1]} ${gut2} ${cols[2]}`;
  PANES.forEach((k) => { const el = $(`#pane-${k}`); if (el) el.hidden = !on(k); });
  const g1 = $("#split-gutter-1"), g2 = $("#split-gutter-2");
  if (g1) g1.hidden = gut1 === "0";
  if (g2) g2.hidden = gut2 === "0";
  $$(".pane-btn").forEach((btn) =>
    btn.setAttribute("aria-pressed", on(btn.dataset.pane)));
  localStorage.setItem(PAIR_KEY, state.panes.join("+"));
  history.replaceState(null, "", route.prefix() + detailPath());
  if (on("chat")) loadChat(state.paper);
}

/* Clicking a pane swaps out whichever visible pane you touched least
   recently, so the one you are actively working in stays put. */
function setPane(key) {
  if (!PANES.includes(key)) return;
  state.paneMRU = [key, ...state.paneMRU.filter((k) => k !== key)];
  if (state.panes.includes(key)) return;
  const keep = state.panes
    .slice()
    .sort((x, y) => state.paneMRU.indexOf(x) - state.paneMRU.indexOf(y))[0];
  state.panes = PANES.filter((k) => k === key || k === keep);
  applyPanes();
}

/* ------------------------------------------------------------------ chat */
/* Claude, with the paper already in front of it. The server drives the
   `claude` CLI and streams the answer back as SSE; this side only has to
   append deltas and, at the end, re-render the finished text as markdown --
   markdown mid-stream would mangle a half-written $...$ and flicker. */

function chatPaneHTML(p) {
  return `<div class="split-side chat-side" id="pane-chat">
    <div class="split-head">
      <h4 class="pane-heading">Claude</h4>
      <span class="chat-status" id="chat-status"></span>
      <select class="chat-model" id="chat-model" title="Which model answers">
        <option value="haiku">Haiku</option>
        <option value="sonnet">Sonnet</option>
      </select>
      <button class="btn tiny" id="chat-reset"
              title="Forget this conversation and start over">Reset</button>
    </div>
    <div class="chat-log" id="chat-log"></div>
    <form class="chat-form" id="chat-form">
      <textarea id="chat-input" rows="2" spellcheck="true"
        placeholder="Ask about this paper — Enter to send, Shift+Enter for a new line"></textarea>
      <button class="btn" type="submit" id="chat-send">Send</button>
    </form>
  </div>`;
}

const CHAT_MODEL_KEY = "chatModel";
const chatModel = () => $("#chat-model")?.value || localStorage.getItem(CHAT_MODEL_KEY) || "haiku";

async function loadChat(p) {
  if (!p || state.chatLoadedFor === p.id) return;
  state.chatLoadedFor = p.id;
  try {
    const data = await api(`/api/papers/${p.id}/chat`);
    state.chatLog = data.messages;
    state.chatBackend = data.backend;
    const picker = $("#chat-model");
    if (picker) picker.value = localStorage.getItem(CHAT_MODEL_KEY) || data.model;
    renderChatLog();
  } catch (err) {
    state.chatLoadedFor = null;
    toast(err.message);
  }
}

function renderChatLog() {
  const log = $("#chat-log");
  if (!log) return;
  if (state.chatBackend === false) {
    log.innerHTML = `<div class="chat-empty">The <code>claude</code> CLI is not
      on this machine's PATH, so there is no backend for this pane.</div>`;
    return;
  }
  log.innerHTML = state.chatLog.length
    ? state.chatLog.map(chatBubbleHTML).join("")
    : `<div class="chat-empty">Ask about this paper. Claude reads the attached
       PDF on the first question, then keeps it in mind for follow-ups.</div>`;
  log.scrollTop = log.scrollHeight;
}

function chatBubbleHTML(msg) {
  const body = msg.role === "assistant"
    ? markdown(msg.content)
    : `<p>${esc(msg.content)}</p>`;
  return `<div class="chat-msg ${msg.role}" data-msg="${msg.id ?? ""}">
    <div class="chat-who">${msg.role === "assistant" ? "Claude" : "You"}
      ${msg.role === "assistant"
        ? `<button class="chat-tonotes" data-tonotes="${msg.id ?? ""}"
             title="Append this answer to your notes">→ notes</button>` : ""}
    </div>
    <div class="chat-body">${body}</div>
  </div>`;
}

/* The reason the pane is worth having next to the notes: one click moves an
   answer into the thing you actually keep. */
function appendToNotes(text) {
  const notes = $("#notes");
  if (!notes || !text) return toast("no notes pane to write to");
  notes.value += (notes.value.trim() ? "\n\n" : "") + text;
  // Reuse the textarea's own handler so autosave and preview stay in sync.
  notes.dispatchEvent(new Event("input"));
  toast("added to notes");
}

async function sendChat(p) {
  const input = $("#chat-input");
  const text = (input?.value || "").trim();
  if (!text || state.chatBusy) return;
  input.value = "";
  state.chatBusy = true;
  $("#chat-send").disabled = true;

  state.chatLog.push({ role: "user", content: text });
  renderChatLog();
  const log = $("#chat-log");
  log.insertAdjacentHTML("beforeend",
    `<div class="chat-msg assistant streaming" id="chat-streaming">
       <div class="chat-who">Claude</div><div class="chat-body"></div></div>`);
  const body = $("#chat-streaming .chat-body");
  const status = $("#chat-status");
  let answer = "";

  const flush = () => {
    // Plain text while streaming; markdown() once the answer is whole.
    body.textContent = answer;
    log.scrollTop = log.scrollHeight;
  };

  try {
    const res = await fetch(`/api/papers/${p.id}/chat`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text, model: chatModel() }),
    });
    if (!res.ok) {
      const err = await res.json().catch(() => ({}));
      throw new Error(err.error || `chat failed (${res.status})`);
    }

    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buffer += decoder.decode(value, { stream: true });
      const frames = buffer.split("\n\n");
      buffer = frames.pop();           // last frame may be incomplete
      for (const frame of frames) {
        const line = frame.replace(/^data: ?/, "").trim();
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        if (ev.type === "tool") {
          status.textContent = `reading ${p.pdf_path || "the paper"}…`;
        } else if (ev.type === "delta") {
          status.textContent = "";
          answer += ev.text;
          flush();
        } else if (ev.type === "done") {
          answer = ev.text || answer;
          state.chatLog.push({ id: ev.id, role: "assistant", content: answer });
        } else if (ev.type === "error") {
          throw new Error(ev.message);
        }
      }
    }
    if (!state.chatLog.length || state.chatLog.at(-1).role !== "assistant") {
      state.chatLog.push({ role: "assistant", content: answer });
    }
  } catch (err) {
    state.chatLog.push({ role: "assistant", content: `**Chat failed.** ${err.message}` });
    toast(err.message);
  } finally {
    state.chatBusy = false;
    const send = $("#chat-send");
    if (send) send.disabled = false;
    if ($("#chat-status")) $("#chat-status").textContent = "";
    renderChatLog();
  }
}

function wireChat(p) {
  const form = $("#chat-form");
  if (!form) return;

  form.addEventListener("submit", (e) => { e.preventDefault(); sendChat(p); });

  $("#chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendChat(p); }
  });

  $("#chat-model").addEventListener("change", (e) =>
    localStorage.setItem(CHAT_MODEL_KEY, e.target.value));

  $("#chat-reset").addEventListener("click", async () => {
    if (state.chatBusy) return toast("wait for the current answer");
    await api(`/api/papers/${p.id}/chat`, { method: "DELETE" });
    state.chatLog = [];
    renderChatLog();
    toast("conversation reset");
  });

  // Delegated: bubbles are re-rendered on every turn.
  $("#chat-log").addEventListener("click", (e) => {
    const btn = e.target.closest(".chat-tonotes");
    if (!btn) return;
    const msg = state.chatLog.find((m) => String(m.id) === btn.dataset.tonotes);
    appendToNotes(msg?.content || btn.closest(".chat-msg").querySelector(".chat-body").innerText);
  });
}

function metaPanel(p) {
  const field = (key, label, value, type = "input") =>
    `<div class="field"><label for="f-${key}">${label}</label>
      ${type === "textarea"
        ? `<textarea id="f-${key}" data-field="${key}" rows="4">${esc(value)}</textarea>`
        : `<input id="f-${key}" data-field="${key}" value="${esc(value)}">`}</div>`;

  return `<div class="dpanel">
    ${field("title", "Title", p.title)}
    ${field("authors", "Authors", p.authors)}
    <div class="field-row">
      ${field("year", "Year", p.year)}
      ${field("popularity", "Popularity (cite count)", p.popularity)}
    </div>
    ${field("venue", "Venue", p.venue)}
    <div class="field">
      <label for="f-read_status">Reading status</label>
      <select id="f-read_status" data-field="read_status">
        ${["unread", "reading", "read"].map((v) =>
          `<option value="${v}"${p.read_status === v ? " selected" : ""}>${v}</option>`).join("")}
      </select>
    </div>
    ${field("citation", "Raw citation", p.citation, "textarea")}
    <div class="dcite"><h4>Linked PDF</h4>${p.pdf_path
      ? `papers/${esc(p.pdf_path)}` : "none — attach one from the PDF tab"}</div>
    <div class="detail-foot">
      <button class="btn primary" id="save-meta">Save changes</button>
      <button class="btn danger" id="delete-paper">Delete paper</button>
    </div>
  </div>`;
}

/* --------------------------------------------------------- detail wiring */

const saveNotes = debounce(async (id, text) => {
  const el = $("#save-state");
  if (el) { el.textContent = "saving…"; el.className = "save-state saving"; }
  try {
    const updated = await api(`/api/papers/${id}`, {
      method: "PATCH", body: JSON.stringify({ notes: text }),
    });
    if (state.paper?.id === id) state.paper.notes = updated.notes;
    const row = state.papers.find((p) => p.id === id);
    if (row) { row.notes = updated.notes; row.has_notes = updated.has_notes; }
    if (el) { el.textContent = "saved"; el.className = "save-state saved"; }
    refreshCard(id);
  } catch (err) {
    if (el) { el.textContent = "save failed"; el.className = "save-state"; }
    toast(err.message);
  }
}, 600);

function refreshCard(id) {
  const card = $(`.card[data-id="${id}"]`);
  const paper = state.papers.find((p) => p.id === id);
  if (card && paper) card.outerHTML = cardHTML(paper);
}

async function reloadPaperData(id) {
  state.paper = await api(`/api/papers/${id}`);
  const idx = state.papers.findIndex((p) => p.id === id);
  if (idx >= 0) { state.papers[idx] = state.paper; refreshCard(id); }
  api("/api/bootstrap").then(renderFacets);
  return state.paper;
}

async function reloadPaper(id) {
  await reloadPaperData(id);
  renderDetail();
}

/* In split view a full re-render would reload the PDF iframe and throw away the
   reader's scroll position, so patch the tag row in place instead. */
async function afterTagChange(id) {
  const fresh = await reloadPaperData(id);
  const row = state.tab === "split" ? $(".split-tags") : null;
  if (!row) return renderDetail();
  row.innerHTML = tagRowHTML(fresh);
  wireTagRow(fresh);
}

function wireTagRow(p) {
  const tagInput = $("#tag-input");
  tagInput?.addEventListener("keydown", async (e) => {
    if (e.key !== "Enter" || !tagInput.value.trim()) return;
    e.preventDefault();
    await api(`/api/papers/${p.id}/tags`, {
      method: "POST", body: JSON.stringify({ name: tagInput.value }),
    });
    await afterTagChange(p.id);
    $("#tag-input")?.focus();
  });

  $$("[data-untag]").forEach((btn) => btn.addEventListener("click", async () => {
    await api(`/api/papers/${p.id}/tags/${encodeURIComponent(btn.dataset.untag)}`,
      { method: "DELETE" });
    afterTagChange(p.id);
  }));
}

/* Dragging over an <iframe> loses the pointer to the PDF viewer, so the drag
   runs on the window with pointer capture and the iframe muted meanwhile. */
function wireSplitGutter() {
  const wrap = $("#split");
  if (!wrap) return;
  $$(".split-gutter", wrap).forEach((g) => wireGutter(g, wrap));
}

/* The hidden pane collapses to a zero-width track, so the left visible pane
   always starts at the wrapper's left edge -- the percentage maths below is
   the same for all three pairs. */
function wireGutter(gutter, wrap) {
  const apply = (pct) => {
    const clamped = Math.min(80, Math.max(20, pct));
    wrap.style.setProperty("--split-w", clamped + "%");
    localStorage.setItem(splitKey(state.panes), Math.round(clamped));
  };

  gutter.addEventListener("pointerdown", (e) => {
    e.preventDefault();
    gutter.setPointerCapture(e.pointerId);
    wrap.classList.add("dragging");
    // A wide, short gutter means the media query stacked the columns.
    const stacked = gutter.offsetWidth > gutter.offsetHeight;
    const move = (ev) => {
      const box = wrap.getBoundingClientRect();
      apply(stacked
        ? ((ev.clientY - box.top) / box.height) * 100
        : ((ev.clientX - box.left) / box.width) * 100);
    };
    const up = () => {
      wrap.classList.remove("dragging");
      gutter.removeEventListener("pointermove", move);
      gutter.removeEventListener("pointerup", up);
      gutter.removeEventListener("pointercancel", up);
    };
    gutter.addEventListener("pointermove", move);
    gutter.addEventListener("pointerup", up);
    gutter.addEventListener("pointercancel", up);
  });

  gutter.addEventListener("dblclick", () => apply(55));
  gutter.addEventListener("keydown", (e) => {
    const step = e.key === "ArrowLeft" ? -3 : e.key === "ArrowRight" ? 3 : 0;
    if (!step) return;
    e.preventDefault();
    apply(parseFloat(wrap.style.getPropertyValue("--split-w")) + step);
  });
}

function wireDetail(p) {
  const notes = $("#notes");
  if (notes) {
    notes.addEventListener("input", () => {
      // In the split the preview is behind a toggle; don't re-render it unseen.
      const md = $("#md-preview");
      if (!md.hidden) md.innerHTML = markdown(notes.value);
      $("#save-state").textContent = "…";
      saveNotes(p.id, notes.value);
    });
  }

  const preview = $("#toggle-preview");
  preview?.addEventListener("click", () => {
    const on = preview.getAttribute("aria-pressed") !== "true";
    preview.setAttribute("aria-pressed", on);
    preview.textContent = on ? "Edit" : "Preview";
    if (on) $("#md-preview").innerHTML = markdown($("#notes").value);
    $("#notes").hidden = on;
    $("#md-preview").hidden = !on;
    if (!on) $("#notes").focus();
  });

  wireSplitGutter();
  wireTagRow(p);
  wireChat(p);

  $("#pane-picker")?.addEventListener("click", (e) => {
    const btn = e.target.closest(".pane-btn");
    if (btn) setPane(btn.dataset.pane);
  });

  $("#detach-pdf")?.addEventListener("click", async () => {
    await api(`/api/papers/${p.id}`, {
      method: "PATCH", body: JSON.stringify({ pdf_path: "" }),
    });
    reloadPaper(p.id);
  });

  const picker = $("#pdf-picker");
  if (picker) {
    api("/api/pdfs").then(({ files }) => {
      picker.innerHTML += files.map((f) =>
        `<option value="${esc(f.path)}">${esc(f.path)}${f.attached ? " (in use)" : ""}</option>`
      ).join("");
    });
    picker.addEventListener("change", async () => {
      if (!picker.value) return;
      await api(`/api/papers/${p.id}`, {
        method: "PATCH", body: JSON.stringify({ pdf_path: picker.value }),
      });
      state.tab = "pdf";
      reloadPaper(p.id);
    });
  }

  $("#pdf-upload")?.addEventListener("change", async (e) => {
    const file = e.target.files[0];
    if (!file) return;
    toast(`uploading ${file.name}…`);
    const { path } = await api(`/api/upload?name=${encodeURIComponent(file.name)}`,
      { method: "PUT", body: file, headers: {} });
    await api(`/api/papers/${p.id}`, {
      method: "PATCH", body: JSON.stringify({ pdf_path: path }),
    });
    toast("attached");
    reloadPaper(p.id);
  });

  $("#save-meta")?.addEventListener("click", async () => {
    const fields = {};
    $$("[data-field]").forEach((el) => { fields[el.dataset.field] = el.value; });
    await api(`/api/papers/${p.id}`, { method: "PATCH", body: JSON.stringify(fields) });
    toast("saved");
    reloadPaper(p.id);
  });

  $("#delete-paper")?.addEventListener("click", async () => {
    if (!confirm("Delete this paper and its notes? This cannot be undone.")) return;
    await api(`/api/papers/${p.id}`, { method: "DELETE" });
    closeDetail();
    load();
    api("/api/bootstrap").then(renderFacets);
  });
}

/* ----------------------------------------------------------------- wiring */

$("#q").addEventListener("input", debounce((e) => {
  state.q = e.target.value.trim();
  if (state.q && state.sort === "popularity") {
    state.sort = "relevance";
    $("#sort").value = "relevance";
  }
  load();
}, 200));

$("#sort").addEventListener("change", (e) => { state.sort = e.target.value; load(); });

$("#sidebar").addEventListener("click", (e) => {
  const facet = e.target.closest("[data-facet]");
  if (facet) return toggleFacet(facet.dataset.facet, facet.dataset.value);

  const clear = e.target.closest("[data-clear]");
  if (clear) { state[clear.dataset.clear].clear(); return load(); }

  const quick = e.target.closest("[data-quick]");
  if (!quick) return;
  const k = quick.dataset.quick;
  const wasOn = quick.getAttribute("aria-pressed") === "true";
  state.flag = ""; state.min_pop = 0; state.has_pdf = null;
  state.has_notes = null; state.read_status = ""; state.lib_fulltext = false;
  if (!wasOn || k === "all") {
    if (k === "notes") state.has_notes = true;
    else if (k === "pdf") state.has_pdf = true;
    else if (k === "multi") state.min_pop = 2;
    else if (k === "draft") state.flag = "draft";
    else if (k === "reading") state.read_status = "reading";
    else if (k === "libtext") state.lib_fulltext = true;
  }
  load();
});

$("#active-filters").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-remove]");
  if (!btn) return;
  state[btn.dataset.remove].delete(btn.dataset.value);
  load();
});

$("#list").addEventListener("click", (e) => {
  const card = e.target.closest(".card");
  if (card) openPaper(+card.dataset.id);
});

$("#list").addEventListener("keydown", (e) => {
  const card = e.target.closest(".card");
  if (card && (e.key === "Enter" || e.key === " ")) {
    e.preventDefault();
    openPaper(+card.dataset.id);
  }
});

$("#load-more").addEventListener("click", () => {
  state.offset += PAGE;
  load({ append: true });
});

$(".detail-tabs").addEventListener("click", (e) => {
  const tab = e.target.closest(".tab");
  if (!tab) return;
  state.tab = tab.dataset.tab;
  renderDetail();
});

$("#close-detail").addEventListener("click", closeDetail);
$("#expand").addEventListener("click", () => $("#app").classList.toggle("wide"));

$("#new-paper").addEventListener("click", async () => {
  const title = prompt("Title of the new paper?");
  if (!title) return;
  const paper = await api("/api/papers", {
    method: "POST", body: JSON.stringify({ title, citation: title }),
  });
  await load();
  openPaper(paper.id, "meta");
});

$("#theme").addEventListener("click", () => {
  const now = document.documentElement.dataset.theme;
  const next = now === "dark" ? "light" : now === "light" ? "" : "dark";
  document.documentElement.dataset.theme = next;
  localStorage.setItem("theme", next);
});
document.documentElement.dataset.theme = localStorage.getItem("theme") || "";

document.addEventListener("keydown", (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (e.key === "/" && !typing) { e.preventDefault(); $("#q").focus(); }
  if (e.key === "Escape") {
    if (typing) e.target.blur();
    else if (state.selected) closeDetail();
  }
  if (typing || !state.papers.length || state.mode === "thesis") return;
  if (e.key === "j" || e.key === "k") {
    e.preventDefault();
    const ids = state.papers.map((p) => p.id);
    const at = ids.indexOf(state.selected);
    const next = e.key === "j"
      ? Math.min(ids.length - 1, at + 1)
      : Math.max(0, at <= 0 ? 0 : at - 1);
    openPaper(ids[next]);
    $(`.card[data-id="${ids[next]}"]`)?.scrollIntoView({ block: "nearest" });
  }
});

/* ------------------------------------------------------------------ boot */

(async function boot() {
  try {
    renderFacets(await api("/api/bootstrap"));
    await load();
    // #paper/<id>/<tab> — bookmarkable, and survives a reload.
    const deep = location.hash.match(
      /^#paper\/(\d+)(?:\/(notes|pdf|split|meta|librarian))?(?:\/([a-z]+\+[a-z]+))?/);
    if (deep) {
      const pair = (deep[3] || "").split("+");
      if (deep[2] === "split" && validPanes(pair)) state.panes = pair;
      await openPaper(+deep[1], deep[2] || "notes");
    }
    document.dispatchEvent(new CustomEvent("library:ready"));
  } catch (err) {
    $("#list").innerHTML =
      `<div class="empty">Could not reach the API — is app.py running?<br><br>${esc(err.message)}</div>`;
  }
})();
