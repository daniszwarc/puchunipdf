import asyncio

import fitz  # PyMuPDF

import llm

WORDS, OVERLAP, BATCH = 350, 40, 32  # ~500 tokens / ~50 overlap, using ~1.4 tokens per word


def extract_pages(data: bytes) -> list[tuple[int, str]]:
    with fitz.open(stream=data, filetype="pdf") as pdf:
        return [(i + 1, p.get_text().strip()) for i, p in enumerate(pdf)]


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
