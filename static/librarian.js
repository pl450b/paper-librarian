/* The Librarian in the web UI: status chip, per-paper Librarian tab, semantic
   search, and the Librarian mode (Ask / Reading queue / Wanted PDFs / Activity).

   Everything here talks to /api/librarian/*, which app.py proxies to the
   Librarian daemon. If the daemon is absent, all of it stays hidden and the
   app behaves exactly as before. Every interpolated value goes through esc();
   markdown() output is the app's own renderer and escapes its input first. */

const L = {
  online: false, status: null, view: "ask",
  fulltext: {}, fulltextAt: -1,          // paper id -> {source, arxiv_id, pdf}
  chat: [], chatBusy: false, chatLoaded: false,
};

/* Key-claim ids -> text, from brief.md via the daemon's /status. */
const claimName = (id) => ((L.status && L.status.claims) || {})[id] || "";
const TIER_NAME = { deep: "read in full", skim: "skimmed", wanted: "wanted (no PDF)" };

/* -------------------------------------------------------------- helpers */

function relBadge(r) {
  if (r === null || r === undefined) return "";
  return `<span class="lib-rel r${+r}" title="Relevance to your project (0-3)">${+r}/3</span>`;
}

/* #123 in an answer becomes a link that opens the paper's Librarian tab.
   Applied to markdown() output, so it only touches text, never tag attributes
   (markdown escapes '<', and ids never contain quotes). */
function linkifyIds(html) {
  return html.replace(/(^|[\s(\[,;>])#(\d{1,5})\b/g,
    (_, pre, id) => `${pre}<a href="#paper/${id}/librarian" data-open-paper="${id}">#${id}</a>`);
}

/* The note's frontmatter and its own "# Title" line: the panel header already
   shows the title. */
function stripFrontmatter(md) {
  const body = md.startsWith("---") ? md.split("---").slice(2).join("---").trim() : md;
  return body.replace(/^# .*\n+/, "");
}

async function libApi(path, opts) {
  return api(`/api/librarian/${path}`, opts);
}

/* --------------------------------------------------------------- status */

async function pollStatus() {
  try {
    L.status = await libApi("status");
    L.online = true;
  } catch {
    L.online = false;
  }
  for (const id of ["lib-chip", "tab-librarian", "mode-librarian", "semantic"]) {
    const el = document.getElementById(id);
    if (el) el.hidden = !L.online;
  }
  if (!L.online) {
    if (state.semantic) { state.semantic = false; load(); }
    return;
  }
  const s = L.status;
  if (s.cataloged !== L.fulltextAt) loadFulltext(s.cataloged);
  const run = s.running ? ` · ${s.running.kind} #${s.running.paper_id ?? s.running.arg}` : "";
  const chip = $("#lib-chip");
  chip.textContent = `📚 ${s.cataloged}/${s.papers}` +
    (s.queued ? ` · ${s.queued} queued` : "") + run + (s.paused ? " · ⏸" : "");
  chip.title = s.paused ? `Librarian paused: ${s.paused}` : "Librarian status — click for activity";
  chip.classList.toggle("busy", !!s.running);
  if (state.mode === "librarian" && L.view === "activity") renderActivity();
}

$("#lib-chip").addEventListener("click", () => { L.view = "activity"; setMode("librarian"); });

/* ------------------------------------- full text the Librarian holds */

async function loadFulltext(at) {
  try {
    L.fulltext = await libApi("fulltext");
    L.fulltextAt = at;
  } catch { return; }
  const btn = $("#quick-libtext");
  if (btn) {
    btn.hidden = false;
    btn.querySelector(".count").textContent = Object.keys(L.fulltext).length;
  }
  if (state.papers.length && !state.semantic) renderList();
  // A paper opened before the map arrived (e.g. from a #paper/<id>/pdf link)
  // rendered without the Librarian's copy; show it now.
  if (state.paper && !state.paper.pdf_path && state.tab === "pdf" && L.fulltext[state.paper.id])
    renderDetail();
}

window.libChip = (p) => {
  const f = L.fulltext[p.id];
  if (!f || p.pdf_path) return "";
  return `<span class="chip libtext" title="${f.source === "arxiv"
    ? "The Librarian read the arXiv full text" : "The Librarian downloaded an open-access PDF"}">${
    f.source === "arxiv" ? "arXiv text" : "OA PDF"} · librarian</span>`;
};

window.libPdfHTML = (p) => {
  const f = L.fulltext[p.id];
  if (!f) return "";
  if (f.pdf) {
    return `<div class="pdf-bar"><span class="path">Open-access copy downloaded by the Librarian
        (librarian/cache/pdf/, not in papers/)</span>
        <a class="btn" href="/api/librarian/pdf/${p.id}" target="_blank" rel="noopener">Open tab</a></div>
      <iframe class="pdf-frame" src="/api/librarian/pdf/${p.id}#view=FitH" title="PDF"></iframe>`;
  }
  if (f.arxiv_id) {
    const id = encodeURIComponent(f.arxiv_id);
    return `<div class="lib-callout" style="margin:16px">The Librarian read this paper's arXiv
      full text (arXiv:${esc(f.arxiv_id)}).
      <a href="https://arxiv.org/abs/${id}" target="_blank" rel="noopener">abstract page</a> ·
      <a href="https://arxiv.org/pdf/${id}" target="_blank" rel="noopener">PDF</a></div>`;
  }
  return "";
};

/* ------------------------------------------------- the Librarian tab */

window.librarianPanel = (p) =>
  `<div class="dpanel" id="lib-panel" data-paper="${p.id}">
     ${headerHTML(p)}<div class="empty" style="padding:30px">Asking the Librarian…</div></div>`;

async function fillLibrarianPanel(p) {
  const panel = $("#lib-panel");
  if (!panel || +panel.dataset.paper !== p.id) return;
  let d;
  try {
    d = await libApi(`paper/${p.id}`);
  } catch (err) {
    panel.innerHTML = `${headerHTML(p)}<div class="empty">${esc(err.message)}</div>`;
    return;
  }
  if (!$("#lib-panel") || +$("#lib-panel").dataset.paper !== p.id) return;
  if (!d.cataloged) {
    panel.innerHTML = `${headerHTML(p)}
      <div class="lib-empty">
        <p>The Librarian hasn't cataloged this paper yet.</p>
        <button class="btn primary" data-lib-catalog="${p.id}">Catalog it now</button>
      </div>`;
    return;
  }
  const st = d.state;
  const claims = (st.claims || []).map((l) =>
    `<span class="chip lib-leg" title="${esc(claimName(l))}">Claim ${esc(l)}</span>`).join("");
  const sugg = (st.suggested_tags || []).filter((t) => !p.tags.includes(t));
  const tags = sugg.length ? `<div class="lib-sugg"><span class="lib-label">Suggested tags</span>
      ${sugg.map((t) => `<button class="chip tag lib-accept" data-accept-tag="${esc(t)}"
          title="Add this tag">+ ${esc(t)}</button>`).join("")}</div>` : "";
  const inLib = d.references.filter((r) => r.matched_paper_id);
  const ghosts = d.references.filter((r) => !r.matched_paper_id && r.ghost_id);
  const refList = `
    ${inLib.length ? `<h4>Cites, in your library (${inLib.length})</h4><ul class="lib-refs">
      ${inLib.map((r) => `<li><a href="#paper/${r.matched_paper_id}/librarian"
          data-open-paper="${r.matched_paper_id}">${esc(r.title || "#" + r.matched_paper_id)}</a>
          <span class="year">${esc(r.year)}</span></li>`).join("")}</ul>` : ""}
    ${ghosts.length ? `<details><summary>Cites, not in your library (${ghosts.length})</summary>
      <ul class="lib-refs">${ghosts.map((r) => `<li>${esc(r.title || r.raw.slice(0, 160))}
        <span class="year">${esc(r.year)}</span>${r.ghost_count > 1
          ? ` <span class="chip" title="cited by this many library papers">${+r.ghost_count}×</span>` : ""}
        ${r.doi ? ` <a href="https://doi.org/${encodeURI(r.doi)}" target="_blank" rel="noopener">doi</a>` : ""}
        </li>`).join("")}</ul></details>` : ""}`;
  const citedBy = d.cited_by.length ? `<h4>Cited by, in your library (${d.cited_by.length})</h4>
    <ul class="lib-refs">${d.cited_by.map((c) => `<li><a href="#paper/${c.id}/librarian"
      data-open-paper="${c.id}">${esc(c.title)}</a> <span class="year">${esc(c.year)}</span></li>`).join("")}</ul>` : "";
  const req = d.request && d.request.state !== "received"
    ? `<div class="lib-callout">PDF requested (${esc(d.request.state)}). Attach one from the PDF tab and
       the Librarian will read it in full.</div>` : "";
  panel.innerHTML = `${headerHTML(p)}
    <div class="lib-badges">${relBadge(st.relevance)}
      <span class="chip">${esc(TIER_NAME[st.tier] || st.tier)}</span>
      <span class="chip" title="Where the Librarian's text came from">from ${esc(st.text_source)}</span>
      ${claims}
      ${st.global_citations != null ? `<span class="chip" title="OpenAlex citation count">${+st.global_citations} citations</span>` : ""}
      ${st.doi ? `<a class="chip" href="https://doi.org/${encodeURI(st.doi)}" target="_blank" rel="noopener">doi</a>` : ""}
      <span class="lib-spacer"></span>
      <button class="btn tiny" data-lib-catalog="${p.id}" title="Read and judge this paper again">Re-catalog</button>
    </div>
    ${req}${tags}
    <div class="md-preview lib-note">${linkifyIds(markdown(stripFrontmatter(d.note || st.summary || "")))}</div>
    <div class="lib-meta">Cataloged ${esc((st.cataloged_at || "").replace("T", " "))} ·
      ${esc(st.note_model || st.triage_model)} · ${esc(st.note_path)}</div>
    ${citedBy}${refList}`;
}

document.addEventListener("detail:rendered", (e) => {
  if (state.tab === "librarian") fillLibrarianPanel(e.detail);
});

document.addEventListener("click", async (e) => {
  const open = e.target.closest("[data-open-paper]");
  if (open) {
    e.preventDefault();
    if (state.mode === "thesis") setMode("library");
    return openPaper(+open.dataset.openPaper, "librarian");
  }
  const cat = e.target.closest("[data-lib-catalog]");
  if (cat) {
    await libApi("enqueue", { method: "POST",
      body: JSON.stringify({ paper_id: +cat.dataset.libCatalog, force: true }) });
    toast("queued — the Librarian will get to it shortly");
    return pollStatus();
  }
  const accept = e.target.closest("[data-accept-tag]");
  if (accept && state.paper) {
    await api(`/api/papers/${state.paper.id}/tags`, { method: "POST",
      body: JSON.stringify({ name: accept.dataset.acceptTag }) });
    toast(`tagged: ${accept.dataset.acceptTag}`);
    await reloadPaper(state.paper.id);
  }
});

/* --------------------------------------------------------- semantic search */

$("#semantic").addEventListener("click", () => {
  state.semantic = !state.semantic;
  $("#semantic").setAttribute("aria-pressed", state.semantic);
  $("#q").placeholder = state.semantic
    ? "Describe what you're looking for, in your own words…"
    : "Search titles, authors, venues, notes…  (press /)";
  load();
});

window.librarianSearch = async function () {
  $("#result-count").textContent = "searching by meaning…";
  let hits;
  try {
    hits = await libApi(`search?q=${encodeURIComponent(state.q)}&k=40`);
  } catch (err) {
    $("#list").innerHTML = `<div class="empty">${esc(err.message)}</div>`;
    return;
  }
  state.papers = [];
  state.total = hits.length;
  $("#list").innerHTML = hits.length ? hits.map((h) => `
    <article class="card lib-hit" data-id="${h.paper_id}" tabindex="0">
      <div class="card-top">${relBadge(h.relevance)}
        <div class="ttl">${esc(h.title)}</div><span class="year">${esc(h.year)}</span></div>
      <div class="lib-snippet"><span class="chip">${esc(h.kind === "fts" ? "keyword" : h.kind)}${
        h.page ? ` · p. ${esc(h.page)}` : ""}</span> ${esc(h.snippet.slice(0, 280))}</div>
    </article>`).join("")
    : `<div class="empty">Nothing in the cataloged library matches that meaning.</div>`;
  $("#result-count").textContent = `${hits.length} semantic match${hits.length === 1 ? "" : "es"}`;
  $("#load-more").hidden = true;
};

/* ---------------------------------------------------------- Librarian mode */

document.addEventListener("mode:changed", (e) => {
  if (e.detail === "librarian") renderLibView();
});

$("#lib-views").addEventListener("click", (e) => {
  const b = e.target.closest("[data-lview]");
  if (!b) return;
  L.view = b.dataset.lview;
  renderLibView();
});

function renderLibView() {
  $$("#lib-views button").forEach((b) => b.setAttribute("aria-pressed", b.dataset.lview === L.view));
  ({ ask: renderAsk, queue: renderQueue, wanted: renderWanted, activity: renderActivity })[L.view]();
}

/* Ask --------------------------------------------------------------------- */

async function renderAsk() {
  $("#lib-meta").textContent = "library-wide questions; answers cite papers as #id";
  const body = $("#lib-body");
  body.innerHTML = `<div class="lib-ask">
    <div class="chat-log" id="lib-chat-log"></div>
    <form class="chat-form" id="lib-chat-form">
      <textarea id="lib-chat-input" rows="2" spellcheck="true"
        placeholder="e.g. Which papers disagree with my key claims, and why?  (Enter to send)"></textarea>
      <button class="btn primary" type="submit" id="lib-chat-send">Ask</button>
      <button class="btn tiny" type="button" id="lib-chat-reset" title="Start a new conversation">New</button>
    </form></div>`;
  if (!L.chatLoaded) {
    try {
      L.chat = (await libApi("chat/history?channel=web")).messages;
      L.chatLoaded = true;
    } catch { L.chat = []; }
  }
  renderLibChat();
  $("#lib-chat-form").addEventListener("submit", (e) => { e.preventDefault(); sendLibChat(); });
  $("#lib-chat-input").addEventListener("keydown", (e) => {
    if (e.key === "Enter" && !e.shiftKey) { e.preventDefault(); sendLibChat(); }
  });
  $("#lib-chat-reset").addEventListener("click", async () => {
    await libApi("chat/reset", { method: "POST", body: JSON.stringify({ channel: "web" }) });
    L.chat = [];
    renderLibChat();
  });
  $("#lib-chat-input").focus();
}

function renderLibChat() {
  const log = $("#lib-chat-log");
  if (!log) return;
  log.innerHTML = L.chat.length ? L.chat.map((m) => `
    <div class="chat-msg ${m.role === "assistant" ? "assistant" : "user"}">
      <div class="chat-who">${m.role === "assistant" ? "Librarian" : "You"}</div>
      <div class="chat-body">${m.role === "assistant"
        ? linkifyIds(markdown(m.content)) : `<p>${esc(m.content)}</p>`}</div>
    </div>`).join("")
    : `<div class="chat-empty">Ask about the whole library: what's been measured, who
       disagrees with whom, what fills a citation gap, what you should read next.
       The Librarian searches its notes and opens papers before answering.</div>`;
  log.scrollTop = log.scrollHeight;
}

async function sendLibChat() {
  const input = $("#lib-chat-input");
  const text = (input?.value || "").trim();
  if (!text || L.chatBusy) return;
  input.value = "";
  L.chatBusy = true;
  $("#lib-chat-send").disabled = true;
  L.chat.push({ role: "user", content: text });
  renderLibChat();
  const log = $("#lib-chat-log");
  log.insertAdjacentHTML("beforeend", `<div class="chat-msg assistant streaming" id="lib-streaming">
    <div class="chat-who">Librarian <span class="chat-status" id="lib-chat-status">thinking…</span></div>
    <div class="chat-body"></div></div>`);
  const out = $("#lib-streaming .chat-body");
  let answer = "";
  try {
    const res = await fetch("/api/librarian/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ message: text }),
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
      buffer = frames.pop();
      for (const frame of frames) {
        const line = frame.replace(/^data: ?/, "").trim();
        if (!line) continue;
        let ev;
        try { ev = JSON.parse(line); } catch { continue; }
        const status = $("#lib-chat-status");
        if (ev.type === "tool" && status) status.textContent = `${ev.name}…`;
        else if (ev.type === "delta") {
          if (status) status.textContent = "";
          answer += ev.text;
          out.textContent = answer;
          log.scrollTop = log.scrollHeight;
        } else if (ev.type === "done") answer = ev.text || answer;
        else if (ev.type === "error") throw new Error(ev.message);
      }
    }
    L.chat.push({ role: "assistant", content: answer || "(no answer)" });
  } catch (err) {
    L.chat.push({ role: "assistant", content: `**Failed.** ${err.message}` });
  } finally {
    L.chatBusy = false;
    if ($("#lib-chat-send")) $("#lib-chat-send").disabled = false;
    renderLibChat();
  }
}

/* Reading queue (ghost papers) -------------------------------------------- */

async function renderQueue() {
  $("#lib-meta").textContent = "papers your library keeps citing but doesn't have";
  const body = $("#lib-body");
  body.innerHTML = `<div class="empty">Loading…</div>`;
  const rows = await libApi("ghosts?n=80&min=2").catch(() => []);
  body.innerHTML = rows.length ? `<div class="lib-list">${rows.map((g) => `
    <article class="card lib-ghost" data-ghost="${g.id}">
      <div class="card-top">${relBadge(g.relevance)}
        <div class="ttl">${esc(g.title || g.key)}</div><span class="year">${esc(g.year)}</span></div>
      ${g.authors ? `<div class="authors">${esc(g.authors)}</div>` : ""}
      <div class="row"><span class="chip" title="library papers that cite it">cited by ${+g.cite_count} of yours</span>
        ${g.global_citations != null ? `<span class="chip">${+g.global_citations} citations overall</span>` : ""}
        ${g.doi ? `<a class="chip" href="https://doi.org/${encodeURI(g.doi)}" target="_blank" rel="noopener">doi</a>` : ""}
      </div>
      ${g.why || g.summary ? `<div class="lib-snippet">${esc(g.why || g.summary)}</div>` : ""}
      <div class="lib-actions">
        <button class="btn tiny primary" data-ghost-add="${g.id}">Add to library</button>
        <button class="btn tiny" data-ghost-dismiss="${g.id}">Dismiss</button>
      </div>
    </article>`).join("")}</div>`
    : `<div class="empty">No ghost papers yet. They appear once the Librarian has read
       reference lists (two or more library papers must cite the same missing paper).</div>`;
  body.querySelectorAll("[data-ghost-add]").forEach((b) => b.addEventListener("click", async () => {
    const g = rows.find((x) => x.id === +b.dataset.ghostAdd);
    const citation = [g.authors, `"${g.title},"`, g.venue, g.year].filter(Boolean).join(", ")
      + (g.doi ? ` doi:${g.doi}` : "");
    const paper = await api("/api/papers", { method: "POST", body: JSON.stringify({
      title: g.title, citation, authors: g.authors, year: g.year, venue: g.venue }) });
    await libApi(`ghost/${g.id}`, { method: "POST", body: JSON.stringify({ state: "added" }) });
    toast(`added as #${paper.id}; the Librarian will catalog it`);
    renderQueue();
  }));
  body.querySelectorAll("[data-ghost-dismiss]").forEach((b) => b.addEventListener("click", async () => {
    await libApi(`ghost/${b.dataset.ghostDismiss}`, { method: "POST",
      body: JSON.stringify({ state: "dismissed" }) });
    b.closest(".card").remove();
  }));
}

/* Wanted PDFs --------------------------------------------------------------- */

async function renderWanted() {
  $("#lib-meta").textContent = "relevant papers the Librarian has no full text for";
  const body = $("#lib-body");
  body.innerHTML = `<div class="empty">Loading…</div>`;
  const rows = await libApi("requests").catch(() => []);
  body.innerHTML = rows.length ? `<div class="lib-list">${rows.map((r) => `
    <article class="card" data-want="${r.paper_id}">
      <div class="card-top">${relBadge(r.relevance)}
        <div class="ttl"><a href="#paper/${r.paper_id}/librarian" data-open-paper="${r.paper_id}">${esc(r.title)}</a></div>
        <span class="year">${esc(r.year)}</span></div>
      <div class="authors">${esc(r.authors)}</div>
      <div class="row"><span class="chip">${esc(r.request_state)}</span>
        ${r.doi ? `<a class="chip" href="https://doi.org/${encodeURI(r.doi)}" target="_blank" rel="noopener">doi</a>` : ""}
        ${r.why ? `<span class="lib-snippet">${esc(r.why)}</span>` : ""}</div>
      <div class="lib-actions">
        <label class="btn tiny primary">Upload PDF<input type="file" accept="application/pdf"
          data-want-upload="${r.paper_id}" hidden></label>
        <button class="btn tiny" data-want-decline="${r.paper_id}">Can't get it</button>
      </div>
    </article>`).join("")}</div>`
    : `<div class="empty">Nothing wanted right now.</div>`;
  body.querySelectorAll("[data-want-upload]").forEach((inp) => inp.addEventListener("change", async () => {
    const file = inp.files[0];
    if (!file) return;
    toast(`uploading ${file.name}…`);
    const { path } = await api(`/api/upload?name=${encodeURIComponent(file.name)}`,
      { method: "PUT", body: file });
    await api(`/api/papers/${inp.dataset.wantUpload}`, { method: "PATCH",
      body: JSON.stringify({ pdf_path: path }) });
    toast("attached; the Librarian will read it in full");
    renderWanted();
  }));
  body.querySelectorAll("[data-want-decline]").forEach((b) => b.addEventListener("click", async () => {
    await libApi(`request/${b.dataset.wantDecline}`, { method: "POST",
      body: JSON.stringify({ state: "declined" }) });
    b.closest(".card").remove();
  }));
}

/* Activity ------------------------------------------------------------------- */

function renderActivity() {
  const s = L.status;
  $("#lib-meta").textContent = "what the Librarian is doing";
  const body = $("#lib-body");
  if (!s) { body.innerHTML = `<div class="empty">Librarian offline.</div>`; return; }
  const q = Object.entries(s.queue).map(([k, n]) => `${esc(k)} ${+n}`).join(" · ") || "empty";
  body.innerHTML = `<div class="dpanel lib-activity">
    <div class="lib-stats">
      <div><b>${+s.cataloged}</b> of ${+s.papers} papers cataloged
        (${+(s.tiers.deep || 0)} read in full, ${+(s.tiers.skim || 0)} skimmed,
        ${+(s.tiers.wanted || 0)} wanted)</div>
      <div>Queue: ${q}${s.running ? ` — now: ${esc(s.running.kind)} #${esc(s.running.paper_id ?? s.running.arg)}` : ""}</div>
      ${s.failed ? `<div class="lib-warn">${+s.failed} job(s) failed after retries</div>` : ""}
      ${s.brief_missing ? `<div class="lib-warn">Waiting for you to fill in <code>librarian/brief.md</code>
        (and delete its TEMPLATE marker). Nothing is cataloged until then.</div>` : ""}
      ${s.paused ? `<div class="lib-warn">Paused: ${esc(s.paused)}</div>` : ""}
    </div>
    <div class="lib-actions">
      ${s.paused ? `<button class="btn" data-lib-resume>Resume</button>`
                 : `<button class="btn" data-lib-pause>Pause</button>`}
      ${s.backfill ? `<span class="chip">backfill on</span>`
        : `<button class="btn primary" data-lib-backfill title="Queue every paper that predates the Librarian">Start backfill…</button>`}
    </div>
    <h4>Recent events</h4>
    <ul class="lib-events">${s.events.map((e) => `<li><span class="chip">${esc(e.kind)}</span>
      ${e.paper_id ? `<a href="#paper/${e.paper_id}/librarian" data-open-paper="${e.paper_id}">#${e.paper_id}</a> ` : ""}
      ${esc(e.text)} <span class="year">${esc(e.created_at)}</span></li>`).join("") || "<li>none yet</li>"}</ul>
  </div>`;
  body.querySelector("[data-lib-pause]")?.addEventListener("click", async () => {
    await libApi("pause", { method: "POST", body: "{}" }); pollStatus(); });
  body.querySelector("[data-lib-resume]")?.addEventListener("click", async () => {
    await libApi("resume", { method: "POST", body: "{}" }); pollStatus(); });
  body.querySelector("[data-lib-backfill]")?.addEventListener("click", async () => {
    if (!confirm(`Queue all ${s.papers - s.cataloged} un-cataloged papers? Each costs one ` +
                 "Haiku triage, plus a Sonnet read for relevant papers with full text. " +
                 "It runs in the background and pauses itself on usage limits.")) return;
    const r = await libApi("backfill", { method: "POST", body: "{}" });
    toast(`queued ${r.backfill} papers`);
    pollStatus();
  });
}

/* ------------------------------------------------------------------- boot */

document.addEventListener("library:ready", () => {
  pollStatus();
  setInterval(() => { if (!document.hidden) pollStatus(); }, 8000);
  const m = location.hash.match(/^#librarian(?:\/(ask|queue|wanted|activity))?/);
  if (m) { L.view = m[1] || "ask"; setMode("librarian"); }
});
