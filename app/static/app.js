const $ = (id) => document.getElementById(id);
let docId = null;

async function loadDocs() {
  const docs = await (await fetch("/api/documents")).json();
  $("docs").replaceChildren(...docs.map((d) => {
    const li = document.createElement("li");
    li.className = d.id === docId ? "sel" : "";
    const name = Object.assign(document.createElement("span"), { textContent: d.name, title: d.name });
    const pages = Object.assign(document.createElement("small"), { textContent: `${d.pages}p` });
    const del = Object.assign(document.createElement("button"), { textContent: "×", title: "Delete", ariaLabel: "Delete " + d.name });
    del.onclick = async (e) => {
      e.stopPropagation();
      if (!confirm(`Delete "${d.name}"?`)) return;
      await fetch(`/api/documents/${d.id}`, { method: "DELETE" });
      if (docId === d.id) select(null);
      loadDocs();
    };
    li.onclick = () => { select(d.id); loadDocs(); };
    li.append(name, pages, del);
    return li;
  }));
}

function select(id) {
  docId = id;
  $("q").disabled = $("form").querySelector("button").disabled = !id;
  $("log").replaceChildren();
}

$("file").onchange = async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  $("status").textContent = `Indexing ${f.name}…`;
  const r = await fetch("/api/documents?name=" + encodeURIComponent(f.name), { method: "POST", body: f });
  if (r.ok) { const d = await r.json(); $("status").textContent = ""; select(d.id); }
  else $("status").textContent = (await r.json().catch(() => ({}))).detail || "Upload failed";
  loadDocs();
};

// Render text with [p. N] as buttons that toggle the matching source snippets. Uses textContent only.
function render(el, text, sources) {
  el.replaceChildren();
  text.split(/(\[p\.\s*\d+\])/).forEach((part) => {
    const m = part.match(/^\[p\.\s*(\d+)\]$/);
    if (!m) return el.append(part);
    const btn = Object.assign(document.createElement("button"), { className: "cite", textContent: `p. ${m[1]}` });
    btn.onclick = () => toggle(el, btn, +m[1], sources);
    el.append(btn);
  });
}

function toggle(el, btn, page, sources) {
  const key = "snip" + page;
  const open = el.parentNode.querySelector(`[data-k="${key}"]`);
  btn.classList.toggle("open", !open);
  if (open) return open.remove();
  const texts = sources.filter((s) => s.page === page).map((s) => s.text);
  const box = Object.assign(document.createElement("div"), {
    className: "snippet",
    textContent: texts.length ? texts.join("\n…\n") : "This page was not among the retrieved excerpts.",
  });
  box.dataset.k = key;
  el.after(box);
}

$("form").onsubmit = async (e) => {
  e.preventDefault();
  const q = $("q").value.trim();
  if (!q || !docId) return;
  $("q").value = "";
  const log = $("log");
  log.append(Object.assign(document.createElement("div"), { className: "msg user", textContent: q }));
  const ans = Object.assign(document.createElement("div"), { className: "msg" });
  log.append(ans);
  $("form").querySelector("button").disabled = true;
  let text = "", sources = [];
  try {
    const r = await fetch("/api/chat", {
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
        else if (ev === "token") { text += payload; render(ans, text, sources); }
        else if (ev === "error") { ans.classList.add("err"); ans.textContent = payload; }
        log.scrollTop = log.scrollHeight;
      }
    }
  } catch (err) {
    ans.classList.add("err");
    ans.textContent = err.message;
  }
  $("form").querySelector("button").disabled = false;
  $("q").focus();
};

loadDocs();
