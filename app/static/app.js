import * as viewer from "./viewer.js";

const $ = (id) => document.getElementById(id);
const el = (tag, props = {}) => Object.assign(document.createElement(tag), props);
let docId = null, docs = [], busy = false, timer = null, answers = [];
const narrow = matchMedia("(max-width: 899px)");
const OCR_TIP = "Text recognized from scanned pages, may contain errors";

// fetch wrapper: an expired session sends the user to the login page.
async function api(...args) {
  const r = await fetch(...args);
  if (r.status === 401) { location.href = "/login"; throw new Error("not authenticated"); }
  return r;
}
// Chips depend on the page count: redraw answers once the viewer has (re)loaded.
document.addEventListener("viewer-ready", () => answers.forEach((m) => m._src && render(m, ...m._src)));
$("logout").onclick = async () => { await fetch("/api/logout", { method: "POST" }); location.href = "/login"; };

// ---- tabs (narrow screens) ----
function setTab(tab) {
  document.body.dataset.tab = tab;
  document.querySelectorAll("#tabs [role=tab]").forEach((b) => b.setAttribute("aria-selected", b.dataset.tab === tab));
}
document.querySelectorAll("#tabs [role=tab]").forEach((b) => (b.onclick = () => setTab(b.dataset.tab)));

// ---- documents ----
const sel = () => docs.find((d) => d.id === docId);
const noteOf = (d) => d.status === "indexing" ? `Indexing page ${d.done_pages} of ${d.total_pages}`
  : d.status === "failed" ? d.error || "Indexing failed" : "";

// Chat is only enabled for the selected document once it is ready (and not mid-answer).
function syncChat() {
  const d = sel();
  $("q").disabled = $("form").querySelector("button").disabled = d?.status !== "ready" || busy;
  $("newchat").disabled = !d || busy;
  $("q").placeholder = !d ? "Select a document to start" : d.status === "ready" ? "Ask about the document…" : noteOf(d) + (d.status === "indexing" ? "…" : "");
}

function summary(d, withPages) {
  const parts = [el("span", { className: "name", textContent: d.name, title: d.name })];
  if (withPages) parts.push(el("small", { textContent: `${d.pages}p` }));
  if (d.ocr_pages > 0) parts.push(el("span", { className: "badge", textContent: "OCR", title: OCR_TIP }));
  const note = noteOf(d);
  if (note) parts.push(el("span", { className: "stat" + (d.status === "failed" ? " err" : ""), textContent: note }));
  return parts;
}

function renderPicker() {
  const d = sel();
  $("picker-btn").replaceChildren(...(d ? summary(d, false) : [el("span", { className: "name", textContent: "Select a document" })]));
  $("delete").disabled = !d;
  $("docs").replaceChildren(...(docs.length ? docs.map((x) => {
    const li = el("li", { tabIndex: 0 });
    li.setAttribute("role", "option");
    li.setAttribute("aria-selected", x.id === docId);
    li.append(...summary(x, true));
    li.onclick = () => { closePicker(); select(x.id); };
    li.onkeydown = (e) => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); li.onclick(); } };
    return li;
  }) : [el("li", { className: "empty", textContent: "No documents yet. Upload a PDF." })]));
}

function closePicker() {
  $("docs").hidden = true;
  $("picker-btn").setAttribute("aria-expanded", "false");
}
$("picker-btn").onclick = () => {
  const open = $("docs").hidden;
  $("docs").hidden = !open;
  $("picker-btn").setAttribute("aria-expanded", open);
  if (open) $("docs").querySelector("li[aria-selected=true], li")?.focus();
};
document.addEventListener("click", (e) => { if (!e.target.closest("#picker")) closePicker(); });
document.addEventListener("keydown", (e) => { if (e.key === "Escape" && !$("docs").hidden) { closePicker(); $("picker-btn").focus(); } });

async function loadDocs() {
  clearTimeout(timer);
  try {
    docs = await (await api("/api/documents")).json();
  } catch {
    if (docs.some((d) => d.status === "indexing")) timer = setTimeout(loadDocs, 2000);
    return;
  }
  renderPicker();
  syncChat();
  if (docs.some((d) => d.status === "indexing")) timer = setTimeout(loadDocs, 2000);
}

let loadTok = 0;

function emptyHint() {
  $("log").replaceChildren(el("p", {
    className: "hint",
    textContent: docId ? "Ask anything about this document. Your conversation is saved, only you can see it, and New chat deletes it."
      : "Select or upload a document, then ask a question.",
  }));
}

// Saved conversation of the current user for this document (never mixed: ignore stale responses).
async function loadChat(id, my) {
  try {
    const r = await api(`/api/documents/${id}/messages`);
    if (!r.ok || my !== loadTok) return;
    const msgs = await r.json();
    if (my !== loadTok || !msgs.length || $("log").querySelector(".msg")) return;
    msgs.forEach((m) => addMsg(m.role, m.content, m.citations));
    $("log").scrollTop = $("log").scrollHeight;
  } catch { /* the empty state stays */ }
}

function select(id) {
  docId = id;
  answers = [];
  const my = ++loadTok;
  emptyHint();
  if (id) loadChat(id, my);
  viewer.open(id ? () => api(`/api/documents/${id}/file`) : null);
  if (id && narrow.matches) setTab("doc");
  renderPicker();
  syncChat();
}

$("upload").onclick = () => $("file").click();
$("file").onchange = async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  $("status").textContent = `Uploading ${f.name}…`;
  const r = await api("/api/documents?name=" + encodeURIComponent(f.name), { method: "POST", body: f });
  if (r.ok) {
    const d = await r.json();
    $("status").textContent = "";
    await loadDocs();
    select(d.id);
  } else {
    $("status").textContent = (await r.json().catch(() => ({}))).detail || "Upload failed";
  }
};

$("delete").onclick = async () => {
  const d = sel();
  if (!d || !confirm(`Delete "${d.name}"?`)) return;
  await api(`/api/documents/${d.id}`, { method: "DELETE" });
  select(null);
  loadDocs();
};

// ---- answers ----
let toastTimer;
function toast(text) {
  $("toast").textContent = text;
  $("toast").hidden = false;
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => ($("toast").hidden = true), 3500);
}

// h: the citation's highlight (rectangles of the supporting sentence) or null: then just jump and say so.
function jump(n, h) {
  if (narrow.matches) setTab("doc");
  if (!h?.rects?.length) {
    viewer.clearHighlight();
    if (!busy) toast("Highlight not available for this citation"); // while streaming, highlights are not computed yet
  }
  requestAnimationFrame(() => viewer.goTo(n, false, h));
}

// Render text with [p. N] as a chip (jumps the viewer) plus a caret (toggles the source excerpt). textContent only.
function render(msg, text, sources) {
  msg.replaceChildren();
  msg._src = [text, sources];
  const total = viewer.pageCount();
  text.split(/(\[p\.\s*\d+\])/).forEach((part) => {
    const m = part.match(/^\[p\.\s*(\d+)\]$/);
    if (!m) return msg.append(part);
    const n = +m[1];
    const chip = el("button", { type: "button", className: "cite", textContent: `p. ${n}` });
    if (n >= 1 && n <= total) { chip.title = `Go to page ${n}`; chip.onclick = () => jump(n, sources.find((s) => s.page === n)?.highlight); }
    else { chip.disabled = true; chip.title = total ? "Page not in this document" : "Original not available"; }
    const more = el("button", { type: "button", className: "cite-more", textContent: "▾", title: "Show source excerpt" });
    more.setAttribute("aria-label", `Show source excerpt for page ${n}`);
    more.onclick = () => toggle(msg, more, n, sources);
    const group = el("span", { className: "cite-group" });
    group.append(chip, more);
    msg.append(group);
  });
}

function addMsg(role, content, citations) {
  const log = $("log");
  log.querySelector(".hint")?.remove();
  const m = el("div", { className: "msg" + (role === "user" ? " user" : "") });
  log.append(m);
  if (role === "user") m.textContent = content;
  else { answers.push(m); render(m, content, citations || []); }
  return m;
}

$("newchat").onclick = async () => {
  if (!docId || !confirm("Delete this conversation? New chat clears it for good.")) return;
  const id = docId;
  await api(`/api/documents/${id}/messages`, { method: "DELETE" });
  if (docId === id) { answers = []; viewer.clearHighlight(); emptyHint(); }
};

function toggle(msg, btn, page, sources) {
  const key = "snip" + page;
  const open = msg.parentNode.querySelector(`[data-k="${key}"]`);
  btn.classList.toggle("open", !open);
  if (open) return open.remove();
  const texts = sources.filter((s) => s.page === page).map((s) => s.text);
  const box = el("div", {
    className: "snippet",
    textContent: texts.length ? texts.join("\n…\n") : "This page was not among the retrieved excerpts.",
  });
  box.dataset.k = key;
  msg.after(box);
}

$("form").onsubmit = async (e) => {
  e.preventDefault();
  const q = $("q").value.trim();
  if (!q || !docId) return;
  $("q").value = "";
  const log = $("log");
  addMsg("user", q);
  const ans = addMsg("assistant", "");
  busy = true;
  syncChat();
  let text = "", sources = [];
  try {
    const r = await api("/api/chat", {
      method: "POST", headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ document_id: docId, question: q }),
    });
    if (!r.ok) throw new Error((await r.json().catch(() => ({}))).detail || "Request failed");
    const reader = r.body.getReader(), dec = new TextDecoder();
    let buf = "";
    for (;;) {
      const { value, done } = await reader.read();
      if (done) break;
      buf += dec.decode(value, { stream: true });
      let i;
      while ((i = buf.indexOf("\n\n")) >= 0) {
        const [ev, data] = buf.slice(0, i).split("\n").map((l) => l.replace(/^(event|data): ?/, ""));
        buf = buf.slice(i + 2);
        const payload = JSON.parse(data);
        if (ev === "sources") sources = payload;
        else if (ev === "citations") { sources = payload; render(ans, text, sources); } // adds each page's highlight
        else if (ev === "token") { text += payload; render(ans, text, sources); }
        else if (ev === "error") { ans._src = null; ans.classList.add("err"); ans.textContent = payload; }
        log.scrollTop = log.scrollHeight;
      }
    }
  } catch (err) {
    ans._src = null;
    ans.classList.add("err");
    ans.textContent = err.message;
  }
  busy = false;
  syncChat();
  $("q").focus();
};

loadDocs();
