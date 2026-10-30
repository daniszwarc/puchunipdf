import asyncio
import logging
import multiprocessing
import os
import tempfile
import threading
from concurrent.futures import ProcessPoolExecutor
from concurrent.futures.process import BrokenProcessPool

import fitz  # PyMuPDF

import llm

log = logging.getLogger("chatpdf")
WORDS, OVERLAP, BATCH = 350, 40, 32  # ~500 tokens / ~50 overlap, using ~1.4 tokens per word
MIN_CHARS = 25  # fewer non-whitespace chars than this on a page -> OCR it
OCR_DPI = int(os.getenv("OCR_DPI", "200"))
OCR_WORKERS = int(os.getenv("OCR_WORKERS", "2"))
OCR_MAX_PAGES = int(os.getenv("OCR_MAX_PAGES", "150"))

_fitz_lock = threading.Lock()  # PyMuPDF is not thread-safe: serialize in-process use
_pool: ProcessPoolExecutor | None = None


def clean(text: str) -> str:
    """Postgres text can't hold NUL, and lone surrogates aren't valid UTF-8: strip both from extracted text."""
    return text.replace("\x00", "").encode("utf-8", "ignore").decode("utf-8")


class IngestError(Exception):
    """Message is safe to show to the user."""


def count_pages(data: bytes) -> int:
    with _fitz_lock, fitz.open(stream=data, filetype="pdf") as pdf:
        return pdf.page_count


def _geometry(page, textpage=None) -> tuple[float, float, list] | None:
    """(width, height, words) of the page AS DISPLAYED, words = [x0, y0, x1, y1, text] in reading order (PDF points).
    PyMuPDF reports word boxes unrotated, so /Rotate is applied here. None if the page has no words or extraction fails:
    that page just gets no highlight."""
    try:
        words = []
        for x0, y0, x1, y1, w, *_ in page.get_text("words", textpage=textpage):
            w = clean(w).strip()
            if w:
                r = (fitz.Rect(x0, y0, x1, y1) * page.rotation_matrix).normalize()
                words.append([round(r.x0, 1), round(r.y0, 1), round(r.x1, 1), round(r.y1, 1), w])
        return (round(page.rect.width, 1), round(page.rect.height, 1), words) if words else None
    except Exception:
        log.warning("no word boxes for page %d", page.number + 1, exc_info=True)
        return None


def _text_layer(data: bytes) -> tuple[list[str], list]:
    with _fitz_lock, fitz.open(stream=data, filetype="pdf") as pdf:
        return [clean(p.get_text()).strip() for p in pdf], [_geometry(p) for p in pdf]


def _ocr_page(path: str, index: int, dpi: int) -> tuple[str, tuple | None]:
    """Runs in a worker process (own PyMuPDF/Tesseract state). Text and word boxes come from one OCR pass."""
    with fitz.open(path) as pdf:
        page = pdf[index]
        tp = page.get_textpage_ocr(language="spa+fra+eng", dpi=dpi, full=True)
        return clean(page.get_text(textpage=tp)).strip(), _geometry(page, tp)


def _ocr_pool() -> ProcessPoolExecutor:
    # spawn, not fork: the parent runs an event loop and DB connections
    global _pool
    if _pool is None:
        _pool = ProcessPoolExecutor(OCR_WORKERS, mp_context=multiprocessing.get_context("spawn"))
    return _pool


async def extract(data: bytes, on_progress) -> tuple[list[tuple[int, str]], int, list]:
    """Return ([(page_number, text)], pages_recovered_by_ocr, per-page (width, height, words) or None).
    on_progress(done, total) after each page."""
    global _pool
    texts, geo = await asyncio.to_thread(_text_layer, data)
    total = len(texts)
    todo = [i for i, t in enumerate(texts) if len("".join(t.split())) < MIN_CHARS]
    if len(todo) > OCR_MAX_PAGES:
        raise IngestError(f"{len(todo)} pages need OCR, the limit is {OCR_MAX_PAGES} (OCR_MAX_PAGES)")
    done = total - len(todo)
    on_progress(done, total)
    ocr_used = 0
    if todo:
        loop = asyncio.get_running_loop()
        with tempfile.NamedTemporaryFile(suffix=".pdf") as f:  # path, not bytes: avoids pickling the PDF per page
            f.write(data)
            f.flush()

            async def one(i):
                nonlocal done, ocr_used
                text, g = await loop.run_in_executor(_ocr_pool(), _ocr_page, f.name, i, OCR_DPI)
                if len("".join(text.split())) > len("".join(texts[i].split())):
                    texts[i], geo[i] = text, g  # the boxes follow whichever text is indexed
                    ocr_used += 1
                done += 1
                on_progress(done, total)

            # wait for every page before the temp file goes away, then surface the first failure
            errors = [e for e in await asyncio.gather(*(one(i) for i in todo), return_exceptions=True) if e]
            if errors:
                if isinstance(errors[0], BrokenProcessPool):
                    _pool = None
                    raise IngestError("OCR worker crashed (out of memory?), try again")
                raise errors[0]
    return list(enumerate(texts, 1)), ocr_used, geo


def chunk(pages: list[tuple[int, str]]) -> list[tuple[int, str]]:
    """Chunks never cross a page boundary, so every chunk has an exact page number."""
    out = []
    for num, text in pages:
        words = text.split()
        for start in range(0, max(len(words), 1), WORDS - OVERLAP):
            piece = " ".join(words[start:start + WORDS])
            if piece:
                out.append((num, piece))
            if start + WORDS >= len(words):
                break
    return out


async def embed_all(texts: list[str]) -> list[list[float]]:
    sem = asyncio.Semaphore(4)

    async def one(batch):
        async with sem:
            return await llm.embed(batch)

    parts = await asyncio.gather(*(one(texts[i:i + BATCH]) for i in range(0, len(texts), BATCH)))
    return [v for part in parts for v in part]
