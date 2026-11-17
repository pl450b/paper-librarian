/* Thesis view — reads main.tex through /api/thesis and puts it in the middle
   pane, so clicking a citation opens that paper in the existing detail pane.

   Loaded after app.js and deliberately reuses its globals ($, esc, api, state,
   route, toast, openPaper). Nothing here touches the library rendering. */

const T = {
  data: null,
  view: "read",          // "read" | "pdf"
  anchor: "",            // section anchor currently in view
  poll: null,
  mtime: 0,
  pdfMtime: 0,
  loading: false,
};

const POLL_MS = 4000;

/* ------------------------------------------------------------------ routing */

/* #thesis/<anchor>[/paper/<id>/<tab>] — the anchor is "_" when there is none,
   so the paper part always sits at a fixed position. */
route.prefix = () =>
  state.mode === "thesis" ? `#thesis/${T.anchor || "_"}/` : "#";

function syncHash() {
  if (state.mode !== "thesis") return;
  const tail = state.selected ? `paper/${state.selected}/${state.tab}` : "";
  history.replaceState(null, "", `#thesis/${T.anchor || "_"}/${tail}`.replace(/\/$/, ""));
}

/* -------------------------------------------------------------------- modes */

function setMode(mode, { push = true } = {}) {
  state.mode = mode;
  document.body.classList.toggle("thesis-mode", mode === "thesis");
  $("#sidebar").hidden = mode !== "library";
  $("#thesis-sidebar").hidden = mode !== "thesis";
  $(".list-pane").hidden = mode !== "library";
  $("#thesis-pane").hidden = mode !== "thesis";
  const libPane = $("#librarian-pane");          // librarian.js
  if (libPane) libPane.hidden = mode !== "librarian";
  document.body.classList.toggle("librarian-mode", mode === "librarian");
  document.dispatchEvent(new CustomEvent("mode:changed", { detail: mode }));
  $$("#mode-switch button").forEach((b) =>
    b.setAttribute("aria-pressed", b.dataset.mode === mode));

  if (mode === "thesis") {
    if (!T.data && !T.loading) loadThesis();
    startPolling();
    if (push) syncHash();
  } else {
    stopPolling();
    if (push) {
      history.replaceState(null, "", state.selected
        ? `#paper/${state.selected}/${state.tab}` : location.pathname);
    }
  }
}

/* ------------------------------------------------------------------ loading */

async function loadThesis({ keepScroll = false } = {}) {
  T.loading = true;
  const body = $("#thesis-body");
  const scrollTop = body.scrollTop;
  if (!T.data) body.innerHTML = `<div class="empty">Reading the draft…</div>`;
  try {
    const data = await api("/api/thesis");
    T.data = data;
    T.mtime = data.mtime;
    T.pdfMtime = data.pdf_mtime;
    renderThesis();
    if (keepScroll) body.scrollTop = scrollTop;
  } catch (err) {
    body.innerHTML = `<div class="empty">${esc(err.message)}</div>`;
    $("#thesis-meta").textContent = "not found";
  } finally {
    T.loading = false;
  }
}

function startPolling() {
  stopPolling();
  T.poll = setInterval(async () => {
    if (document.hidden || state.mode !== "thesis") return;
    try {
      const s = await api("/api/thesis/status");
      if (!s.exists) return;
      if (s.mtime !== T.mtime) {
        toast("draft changed — reloading");
        await loadThesis({ keepScroll: true });
      } else if (s.pdf_mtime !== T.pdfMtime) {
        T.pdfMtime = s.pdf_mtime;
        if (T.view === "pdf") showPdf(true);
        renderMeta();
      }
    } catch (_) { /* server restarting; try again on the next tick */ }
  }, POLL_MS);
}

function stopPolling() {
  if (T.poll) clearInterval(T.poll);
  T.poll = null;
}

/* ---------------------------------------------------------------- rendering */

function renderThesis() {
  const d = T.data;
  $("#thesis-body").innerHTML = d.html;
  decorateCites();
  renderReferences();
  renderOutline();
  renderSidebarLists();
  renderMeta();
  observeSections();
  if (T.anchor && T.anchor !== "_") scrollToAnchor(T.anchor, false);
}

function citeIndex() {
  const map = new Map();
  (T.data?.cites || []).forEach((c) => map.set(c.key, c));
  return map;
}

function decorateCites() {
  const map = citeIndex();
  $$("#thesis-body .tex-cite").forEach((el) => {
    const cite = map.get(el.dataset.bibkey);
    if (!cite) return;
    el.classList.toggle("linked", !!cite.paper);
    el.classList.toggle("unlinked", !cite.paper);
    el.title = cite.paper
      ? `${cite.paper.title || cite.title}`
      : cite.known ? `${cite.title} — not in your library yet`
                   : `${el.dataset.bibkey} is not in your .bib file`;
  });
}

function renderReferences() {
  const host = $("#tex-references");
  if (!host) return;
  const rows = (T.data.cites || []).map((c) => `
    <li class="ref-row" data-bibkey="${esc(c.key)}">
      <span class="ref-num">[${c.number}]</span>
      <span class="ref-body">
        <span class="ref-title">${esc(c.title || c.key)}</span>
        <span class="ref-meta">${esc([c.author, c.journal, c.year].filter(Boolean).join(" · "))}</span>
      </span>
      ${c.paper
        ? `<button class="btn tiny" data-open-paper="${c.paper.id}">open</button>`
        : `<button class="btn tiny warn" data-link-key="${esc(c.key)}">link…</button>`}
    </li>`).join("");
  host.innerHTML = `<h2 class="tex-head tex-h1" id="references">References</h2>
    <ol class="ref-list">${rows}</ol>`;
}

function renderOutline() {
  const pages = T.data.pages || {};
  $("#outline").innerHTML = (T.data.toc || []).map((s) => `
    <button data-anchor="${esc(s.anchor)}" class="outline-item lvl${s.level}"
            title="${esc(s.title)}">
      <span class="name">${s.number ? `<b>${esc(s.number)}</b> ` : ""}${esc(s.title)}</span>
      ${pages[s.number] ? `<span class="count">p${pages[s.number]}</span>` : ""}
    </button>`).join("") || `<div class="count" style="padding:4px 8px">no sections</div>`;
}

function renderSidebarLists() {
  const d = T.data;
  const unlinked = (d.cites || []).filter((c) => !c.paper);
  $("#unlinked-group").hidden = !unlinked.length;
  $("#unlinked-count").textContent = unlinked.length || "";
  $("#unlinked").innerHTML = unlinked.map((c) => `
    <button data-link-key="${esc(c.key)}" title="${esc(c.title || c.key)}">
      <span class="name">[${c.number}] ${esc(c.title || c.key)}</span>
      <span class="count">link</span>
    </button>`).join("");

  const uncited = d.uncited || [];
  $("#uncited-group").hidden = !uncited.length;
  $("#uncited-count").textContent = uncited.length || "";
  $("#uncited").innerHTML = uncited.map((u) => `
    <button ${u.paper_id ? `data-open-paper="${u.paper_id}"` : `data-link-key="${esc(u.key)}"`}
            title="${esc(u.title || u.key)}">
      <span class="name">${esc(u.title || u.key)}</span>
      <span class="count">${u.paper_id ? "open" : "link"}</span>
    </button>`).join("");

  const todos = d.todos || [];
  $("#todo-group").hidden = !todos.length;
  $("#todo-count").textContent = todos.length || "";
  $("#todos").innerHTML = todos.map((t) => `
    <button data-anchor="${esc(t.section)}" title="${esc(t.text)}">
      <span class="name"><b class="todo-kind ${esc(t.kind)}">${esc(t.kind)}</b> ${esc(t.text)}</span>
    </button>`).join("");
}

function renderMeta() {
  const d = T.data;
  if (!d) return;
  const when = d.mtime ? new Date(d.mtime * 1000) : null;
  const linked = (d.cites || []).filter((c) => c.paper).length;
  const bits = [
    `${d.cites.length} citations`,
    `${linked} linked`,
    when ? `saved ${when.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}` : "",
  ];
  if (d.duplicates?.length) bits.push(`⚠ duplicate key: ${d.duplicates.join(", ")}`);
  $("#thesis-meta").textContent = bits.filter(Boolean).join(" · ");
  $("#thesis-meta").title = d.tex;
}

/* ------------------------------------------------------------ scroll + view */

function scrollToAnchor(anchor, smooth = true) {
  const el = document.getElementById(anchor);
  if (!el) return;
  el.scrollIntoView({ block: "start", behavior: smooth ? "smooth" : "auto" });
  T.anchor = anchor;
  syncHash();
}

function observeSections() {
  if (T.observer) T.observer.disconnect();
  const heads = $$("#thesis-body .tex-head");
  if (!heads.length) return;
  T.observer = new IntersectionObserver((entries) => {
    const top = entries.filter((e) => e.isIntersecting)
      .sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0];
    if (!top) return;
    T.anchor = top.target.id;
    $$("#outline [data-anchor]").forEach((b) =>
      b.classList.toggle("current", b.dataset.anchor === T.anchor));
  }, { rootMargin: "-10% 0px -80% 0px" });
  heads.forEach((h) => T.observer.observe(h));
}

function setView(view) {
  T.view = view;
  $$("#thesis-views button").forEach((b) =>
    b.setAttribute("aria-pressed", b.dataset.view === view));
  $("#thesis-body").hidden = view !== "read";
  $("#thesis-frame").hidden = view !== "pdf";
  if (view === "pdf") showPdf();
}

function showPdf(force = false) {
  const frame = $("#thesis-frame");
  const page = T.data?.pages?.[currentNumber()] || 1;
  const src = `/api/thesis/pdf?v=${T.pdfMtime}#page=${page}&view=FitH`;
  if (force || frame.dataset.src !== src) {
    frame.dataset.src = src;
    frame.src = src;
  }
}

function currentNumber() {
  const entry = (T.data?.toc || []).find((s) => s.anchor === T.anchor);
  return entry ? entry.number : "";
}

/* -------------------------------------------------------------- linking UI */

function citeCardHTML(cite) {
  const p = cite.paper;
  return `<div class="citecard-head">
      <span class="ref-num">[${cite.number}]</span>
      <b>${esc(cite.title || cite.key)}</b>
      <button class="btn icon" data-close-card title="Close">✕</button>
    </div>
    <div class="citecard-meta">${esc([cite.author, cite.journal, cite.year]
      .filter(Boolean).join(" · "))}</div>
    ${p ? `<div class="citecard-linked">
        <span class="chip ${p.pdf_path ? "pdf" : "kw"}">${p.pdf_path ? "PDF" : "no PDF"}</span>
        <span class="chip ${p.has_notes ? "note" : "kw"}">${p.has_notes ? "notes" : "no notes"}</span>
        <span class="chip kw">${esc(p.read_status)}</span>
        <button class="btn tiny" data-open-paper="${p.id}">open</button>
        <button class="btn tiny" data-relink="${esc(cite.key)}">relink…</button>
      </div>`
     : `<div class="citecard-unlinked">
        <p>Not linked to a paper in your library.</p>
        <input class="tag-input wide" data-search="${esc(cite.key)}"
               placeholder="search your library…" autocomplete="off">
        <div class="citecard-results" data-results></div>
        <div class="citecard-actions">
          <button class="btn tiny" data-add-paper="${esc(cite.key)}">Add to library</button>
          <button class="btn tiny" data-ignore="${esc(cite.key)}">Not in library</button>
        </div>
      </div>`}`;
}

function openCiteCard(key, at) {
  const cite = citeIndex().get(key);
  if (!cite) return;
  const card = $("#citecard");
  card.innerHTML = citeCardHTML(cite);
  card.hidden = false;
  const rect = at.getBoundingClientRect();
  card.style.top = `${Math.min(rect.bottom + 8, window.innerHeight - 260)}px`;
  card.style.left =
    `${Math.min(rect.left, window.innerWidth - card.offsetWidth - 16)}px`;
  const search = $("[data-search]", card);
  if (search) {
    search.focus();
    search.addEventListener("input", debounce(async () => {
      const q = search.value.trim();
      const box = $("[data-results]", card);
      if (!q) return (box.innerHTML = "");
      const { papers } = await api(`/api/papers?q=${encodeURIComponent(q)}&limit=6`);
      box.innerHTML = papers.map((p) => `
        <button class="citecard-hit" data-pick="${p.id}" data-key="${esc(key)}">
          <span class="name">${esc(p.title || p.citation.slice(0, 90))}</span>
          <span class="count">${esc(p.year)}</span>
        </button>`).join("") || `<div class="count">no matches</div>`;
    }, 220));
  }
}

function closeCiteCard() {
  $("#citecard").hidden = true;
}

async function linkKey(key, paperId, { ignored = false } = {}) {
  await api("/api/thesis/link", {
    method: "POST",
    body: JSON.stringify({ bibkey: key, paper_id: paperId, ignored }),
  });
  closeCiteCard();
  await loadThesis({ keepScroll: true });
  toast(ignored ? "marked as not in the library" : "linked");
}

async function addCiteToLibrary(key) {
  const cite = citeIndex().get(key);
  if (!cite) return;
  const paper = await api("/api/papers", {
    method: "POST",
    body: JSON.stringify({
      title: cite.title || key,
      citation: [cite.author, cite.title, cite.journal, cite.year]
        .filter(Boolean).join(". ") || key,
      authors: cite.author || "",
      year: cite.year || "",
      venue: cite.journal || "",
    }),
  });
  await linkKey(key, paper.id);
  openPaper(paper.id, "meta");
}

/* ------------------------------------------- phase 5: paper -> thesis panel */

async function renderBacklinks(paper) {
  const body = $("#detail-body");
  if (!body || state.tab !== "meta") return;
  let data;
  try {
    data = await api(`/api/thesis/backlinks/${paper.id}`);
  } catch (_) {
    return;                       // no thesis configured — nothing to add
  }
  if (state.paper?.id !== paper.id || state.tab !== "meta") return;

  const cited = data.sites.length;
  const keys = data.bibkeys.filter((k) => data.in_bib.includes(k));
  const panel = document.createElement("div");
  panel.className = "dcite thesis-backlinks";
  panel.innerHTML = `<h4>In my draft</h4>
    ${keys.length ? keys.map((k) => `
      <div class="bibkey-row">
        <code>\\cite{${esc(k)}}</code>
        <button class="btn tiny" data-copy-cite="${esc(k)}">copy</button>
        ${data.numbers[k] ? `<span class="count">[${data.numbers[k]}]</span>` : ""}
      </div>`).join("")
      : `<p class="count">Not in your .bib yet.
           <button class="btn tiny" data-make-bib="${paper.id}">Add to references.bib</button></p>`}
    ${cited ? `<div class="backlink-list">${data.sites.map((s) => `
        <button class="backlink" data-goto="${esc(s.anchor)}">
          ${s.number ? `<b>${esc(s.number)}</b> ` : ""}${esc(s.title)}
        </button>`).join("")}</div>`
      : keys.length ? `<p class="count">In the bibliography, but not cited yet.</p>` : ""}`;

  const foot = $(".detail-foot", body);
  foot ? foot.parentNode.insertBefore(panel, foot) : body.firstChild.appendChild(panel);
}

async function makeBibEntry(paperId) {
  const preview = await api("/api/thesis/bibentry", {
    method: "POST", body: JSON.stringify({ paper_id: +paperId, preview: true }),
  });
  const key = prompt(
    `Append this to ${preview.file}?\n\n${preview.entry}\nCitation key:`,
    preview.key);
  if (!key) return;
  const made = await api("/api/thesis/bibentry", {
    method: "POST", body: JSON.stringify({ paper_id: +paperId, key }),
  });
  toast(`added ${made.cite} to ${made.file}`);
  if (T.data) await loadThesis({ keepScroll: true });
  if (state.paper?.id === +paperId) reloadPaper(+paperId);
}

/* ------------------------------------------------------------------ wiring */

$("#mode-switch").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-mode]");
  if (btn) setMode(btn.dataset.mode);
});

$("#thesis-views").addEventListener("click", (e) => {
  const btn = e.target.closest("[data-view]");
  if (btn) setView(btn.dataset.view);
});

$("#thesis-reload").addEventListener("click", () => loadThesis({ keepScroll: true }));

$("#thesis-body").addEventListener("click", (e) => {
  const cite = e.target.closest(".tex-cite");
  if (cite) {
    e.preventDefault();
    const key = cite.dataset.bibkey;
    const info = citeIndex().get(key);
    if (info?.paper) return openPaper(info.paper.id, state.tab);
    return openCiteCard(key, cite);
  }
  const open = e.target.closest("[data-open-paper]");
  if (open) return openPaper(+open.dataset.openPaper, state.tab);

  const link = e.target.closest("[data-link-key]");
  if (link) return openCiteCard(link.dataset.linkKey, link);

  const ref = e.target.closest(".tex-ref");
  if (ref && ref.getAttribute("href")?.startsWith("#")) {
    e.preventDefault();
    scrollToAnchor(ref.getAttribute("href").slice(1));
  }
});

$("#thesis-sidebar").addEventListener("click", (e) => {
  const anchor = e.target.closest("[data-anchor]");
  if (anchor && anchor.dataset.anchor) {
    if (T.view === "pdf") setView("read");
    return scrollToAnchor(anchor.dataset.anchor);
  }
  const open = e.target.closest("[data-open-paper]");
  if (open) return openPaper(+open.dataset.openPaper, state.tab);
  const link = e.target.closest("[data-link-key]");
  if (link) return openCiteCard(link.dataset.linkKey, link);
});

$("#citecard").addEventListener("click", async (e) => {
  const card = $("#citecard");
  if (e.target.closest("[data-close-card]")) return closeCiteCard();

  const pick = e.target.closest("[data-pick]");
  if (pick) return linkKey(pick.dataset.key, +pick.dataset.pick);

  const open = e.target.closest("[data-open-paper]");
  if (open) { closeCiteCard(); return openPaper(+open.dataset.openPaper, state.tab); }

  const relink = e.target.closest("[data-relink]");
  if (relink) {
    const cite = citeIndex().get(relink.dataset.relink);
    if (cite) { cite.paper = null; card.innerHTML = citeCardHTML(cite); openCiteCard(cite.key, card); }
    return;
  }

  const add = e.target.closest("[data-add-paper]");
  if (add) return addCiteToLibrary(add.dataset.addPaper);

  const ignore = e.target.closest("[data-ignore]");
  if (ignore) return linkKey(ignore.dataset.ignore, null, { ignored: true });
});

document.addEventListener("click", (e) => {
  if (!$("#citecard").hidden && !e.target.closest("#citecard")
      && !e.target.closest(".tex-cite") && !e.target.closest("[data-link-key]")) {
    closeCiteCard();
  }
});

/* Capture phase: Escape closes the citation card *instead of* the detail pane,
   which app.js would otherwise close on the same keypress. */
document.addEventListener("keydown", (e) => {
  if (e.key === "Escape" && !$("#citecard").hidden) {
    e.stopPropagation();
    closeCiteCard();
  }
}, true);

document.addEventListener("keydown", (e) => {
  const typing = /^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName);
  if (!typing && e.key.toLowerCase() === "t" && !e.metaKey && !e.ctrlKey) {
    setMode(state.mode === "thesis" ? "library" : "thesis");
  }
});

/* Phase 5: the Details tab gets a "In my draft" panel. */
document.addEventListener("detail:rendered", (e) => renderBacklinks(e.detail));

document.addEventListener("click", (e) => {
  const copy = e.target.closest("[data-copy-cite]");
  if (copy) {
    navigator.clipboard?.writeText(`\\cite{${copy.dataset.copyCite}}`);
    toast("citation copied");
  }
  const make = e.target.closest("[data-make-bib]");
  if (make) makeBibEntry(make.dataset.makeBib).catch((err) => toast(err.message));
  const goto = e.target.closest("[data-goto]");
  if (goto) {
    setMode("thesis");
    setTimeout(() => scrollToAnchor(goto.dataset.goto), T.data ? 0 : 400);
  }
});

/* -------------------------------------------------------------------- boot */

document.addEventListener("library:ready", async () => {
  const m = location.hash.match(
    /^#thesis(?:\/([^/]*))?(?:\/paper\/(\d+)(?:\/(notes|pdf|meta))?)?/);
  if (!m) return;
  T.anchor = m[1] && m[1] !== "_" ? m[1] : "";
  setMode("thesis", { push: false });
  if (m[2]) openPaper(+m[2], m[3] || "notes");
});
