// Continuous-scroll PDF viewer on top of the vendored pdf.js (see README for the pinned version).
import * as pdfjs from "/vendor/pdfjs/pdf.min.mjs";

pdfjs.GlobalWorkerOptions.workerSrc = "/vendor/pdfjs/pdf.worker.min.mjs";

const $ = (id) => document.getElementById(id);
const box = $("viewer");
const MAX_CANVAS_PX = 16e6; // cap canvas area so high zoom on retina doesn't exhaust memory
let loading = null, pages = [], io = null, token = 0; // loading: the pdf.js loading task, which owns destroy()
let zoom = 1, cur = 1, anchor = { n: 1, frac: 0 }, lastW = 0, pending = null, layer = null; // pending: {n, h} jump waiting for a hidden pane
const visible = new Set();

export const pageCount = () => pages.length;

function setMsg(text) {
  box.replaceChildren(Object.assign(document.createElement("p"), { className: "hint", textContent: text }));
}

function teardown() {
  io?.disconnect();
  pages.forEach(release);
  pages = [];
  visible.clear();
  loading?.destroy();
  loading = null;
  pending = null;
  layer = null; // its page element is discarded with the pages
  cur = 1;
  anchor = { n: 1, frac: 0 };
  $("vbar").inert = true;
  $("pg").value = 1;
  $("pgtotal").textContent = "/ –";
}

// `load` returns a fetch Response for the PDF (or null to just clear the viewer).
export async function open(load) {
  const my = ++token;
  teardown();
  if (!load) return setMsg("Select or upload a document.");
  setMsg("Loading document…");
  let r;
  try { r = await load(); } catch { return my === token && setMsg("Could not load the document."); }
  if (my !== token) return;
  if (r.status === 404) return setMsg("Original not stored for this document. Upload it again to view it.");
  if (!r.ok) return setMsg("Could not load the document.");
  try {
    const task = pdfjs.getDocument({
      data: new Uint8Array(await r.arrayBuffer()),
      isEvalSupported: false, // no-op on pdf.js 6.x (eval was removed upstream), kept as a safeguard
      wasmUrl: "/vendor/pdfjs/wasm/",
    });
    const doc = await task.promise;
    if (my !== token) return task.destroy();
    const sizes = await Promise.all(Array.from({ length: doc.numPages }, (_, i) =>
      doc.getPage(i + 1).then((proxy) => ({ proxy, v: proxy.getViewport({ scale: 1 }) }))));
    if (my !== token) return task.destroy();
    loading = task;
    pages = sizes.map(({ proxy, v }, i) => {
      const el = Object.assign(document.createElement("div"), { className: "page" });
      el.dataset.n = i + 1;
      return { num: i + 1, proxy, w: v.width, h: v.height, el, canvas: null, task: null, gen: 0, scale: 1 };
    });
    box.replaceChildren(...pages.map((p) => p.el));
    box.scrollTop = 0;
    io = new IntersectionObserver(onIntersect, { root: box, rootMargin: "1200px 0px" });
    pages.forEach((p) => io.observe(p.el));
    lastW = box.clientWidth;
    layout();
    $("pgtotal").textContent = `/ ${pages.length}`;
    $("pg").max = pages.length;
    $("vbar").inert = false;
    updateCurrent();
    document.dispatchEvent(new Event("viewer-ready"));
  } catch (e) {
    console.error(e);
    if (my === token) setMsg("Could not display this PDF.");
  }
}

// Zoom 1 = fit width.
function layout() {
  const avail = box.clientWidth - 24;
  if (avail <= 0) return false; // pane hidden (mobile tab)
  for (const p of pages) {
    p.scale = (avail / p.w) * zoom;
    p.el.style.width = `${p.w * p.scale}px`;
    p.el.style.height = `${p.h * p.scale}px`;
  }
  return true;
}

function relayout() {
  if (!pages.length || !layout()) return;
  for (const n of visible) release(pages[n - 1]);
  const a = pages[anchor.n - 1];
  if (a) box.scrollTop = a.el.offsetTop + anchor.frac * a.el.offsetHeight;
  if (pending) { const q = pending; pending = null; goTo(q.n, true, q.h); }
  for (const n of [...visible]) render(pages[n - 1]);
  updateCurrent();
}

function onIntersect(entries) {
  for (const e of entries) {
    const p = pages[e.target.dataset.n - 1];
    if (!p) continue;
    if (e.isIntersecting) { visible.add(p.num); render(p); }
    else { visible.delete(p.num); release(p); }
  }
  updateCurrent();
}

async function render(p) {
  if (p.canvas || !box.clientWidth) return;
  const gen = ++p.gen;
  let ratio = Math.min(window.devicePixelRatio || 1, 3);
  const px = p.w * p.scale * ratio * p.h * p.scale * ratio;
  if (px > MAX_CANVAS_PX) ratio *= Math.sqrt(MAX_CANVAS_PX / px);
  const viewport = p.proxy.getViewport({ scale: p.scale * ratio });
  const canvas = document.createElement("canvas");
  canvas.width = Math.floor(viewport.width);
  canvas.height = Math.floor(viewport.height);
  p.canvas = canvas;
  try {
    p.task = p.proxy.render({ canvasContext: canvas.getContext("2d"), viewport });
    await p.task.promise;
    if (p.gen === gen && p.canvas === canvas) p.el.append(canvas);
  } catch (e) {
    if (e?.name !== "RenderingCancelledException") console.error(e);
  }
}

function release(p) {
  p.gen++;
  p.task?.cancel();
  p.task = null;
  if (p.canvas) { p.canvas.width = p.canvas.height = 0; p.canvas.remove(); p.canvas = null; }
}

// The current page is the one nearest to a line 35% down the viewer.
function updateCurrent() {
  if (!pages.length || !box.clientWidth) return;
  const r = box.getBoundingClientRect();
  const line = r.top + r.height * 0.35;
  let best = cur, bestD = Infinity;
  for (const n of visible) {
    const b = pages[n - 1].el.getBoundingClientRect();
    const d = b.top > line ? b.top - line : b.bottom < line ? line - b.bottom : 0;
    if (d < bestD) { bestD = d; best = n; }
  }
  cur = best;
  const b = pages[cur - 1].el.getBoundingClientRect();
  anchor = { n: cur, frac: (r.top - b.top) / (b.height || 1) };
  if (document.activeElement !== $("pg")) $("pg").value = cur;
}

const reduceMotion = matchMedia("(prefers-reduced-motion: reduce)");

// One highlight at a time: yellow rectangles over the page, placed in percent of the page box so zoom, fit and resize keep them aligned.
export function clearHighlight() {
  layer?.remove();
  layer = null;
}

function drawHighlight(el, h) {
  clearHighlight();
  layer = Object.assign(document.createElement("div"), { className: "hl-layer" });
  for (const [x0, y0, x1, y1] of h.rects) {
    const r = document.createElement("i");
    r.style.cssText = `left:${(100 * x0) / h.width}%;top:${(100 * y0) / h.height}%;width:${(100 * (x1 - x0)) / h.width}%;height:${(100 * (y1 - y0)) / h.height}%`;
    layer.append(r);
  }
  el.append(layer);
}

// h (optional): {page, width, height, rects} from a citation. The page is scrolled so the highlight is centered, then highlighted.
export function goTo(n, instant = false, h = null) {
  if (!pages.length || !(n >= 1 && n <= pages.length)) return false;
  n = Math.round(n);
  if (!h?.rects?.length) h = null;
  anchor = { n, frac: 0 }; // any re-fit from here on lands on the target instead of fighting the scroll
  if (!box.clientWidth) { pending = { n, h }; return true; } // pane hidden: apply once shown
  if (refit()) instant = true; // pane just reappeared: fit it now, not on the next frame
  const el = pages[n - 1].el;
  let top = el.offsetTop - 12;
  if (h) {
    const mid = (Math.min(...h.rects.map((r) => r[1])) + Math.max(...h.rects.map((r) => r[3]))) / 2;
    top = el.offsetTop + (mid / h.height) * el.offsetHeight - box.clientHeight / 2;
    anchor = { n, frac: (top - el.offsetTop) / el.offsetHeight };
    drawHighlight(el, h);
  }
  box.scrollTo({ top, behavior: instant || reduceMotion.matches ? "auto" : "smooth" });
  el.classList.remove("flash");
  void el.offsetWidth;
  el.classList.add("flash");
  setTimeout(() => el.classList.remove("flash"), 1500);
  return true;
}

let ticking = false;
box.addEventListener("scroll", () => {
  if (ticking) return;
  ticking = true;
  requestAnimationFrame(() => { ticking = false; updateCurrent(); });
});

// Re-fit when the pane resizes or reappears (ignore scrollbar-sized wiggles).
function refit() {
  const w = box.clientWidth;
  if (w === lastW || (w && lastW && Math.abs(w - lastW) < 24)) return false;
  lastW = w;
  relayout();
  return true;
}
new ResizeObserver(refit).observe(box);

function setZoom(z) {
  zoom = Math.min(4, Math.max(0.4, z));
  relayout();
}

$("prev").onclick = () => goTo(cur - 1);
$("next").onclick = () => goTo(cur + 1);
$("pg").onchange = () => { goTo(+$("pg").value) || ($("pg").value = cur); };
$("zin").onclick = () => setZoom(zoom * 1.25);
$("zout").onclick = () => setZoom(zoom / 1.25);
$("fit").onclick = () => setZoom(1);
