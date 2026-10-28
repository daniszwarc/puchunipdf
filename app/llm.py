import asyncio
import json
import os

import httpx

BASE = os.getenv("NAN_BASE_URL", "https://api.nan.builders/v1")
HEADERS = {"Authorization": f"Bearer {os.environ['NAN_API_KEY']}"}
TIMEOUT = httpx.Timeout(30, read=300)
CHAT_MODEL = os.getenv("CHAT_MODEL", "qwen3.6")
EMBED_MODEL = os.getenv("EMBED_MODEL", "qwen3-embedding")
REASONING_EFFORT = os.getenv("REASONING_EFFORT", "none")
CHAT_MAX_TOKENS = int(os.getenv("CHAT_MAX_TOKENS", "8000"))
MAX_ATTEMPTS = 4
ERRORS = {401: "NaN API key invalid", 402: "NaN token quota exhausted for this model", 404: "model id not found"}


class NaNError(Exception):
    """Message is safe to show to the user."""


async def _open(client: httpx.AsyncClient, path: str, body: dict) -> httpx.Response:
    """POST with retry on 429/5xx (exponential backoff). Other errors raise immediately. Caller closes."""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        r = await client.send(client.build_request("POST", path, json=body), stream=True)
        if r.status_code < 400:
            return r
        await r.aclose()
        if (r.status_code == 429 or r.status_code >= 500) and attempt < MAX_ATTEMPTS:
            await asyncio.sleep(2 ** (attempt - 1))
            continue
        raise NaNError(ERRORS.get(r.status_code, f"NaN API error {r.status_code}"))


async def embed(texts: list[str]) -> list[list[float]]:
    assert len(texts) <= 32, "NaN embeddings accept at most 32 inputs per request"
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=TIMEOUT) as c:
        r = await _open(c, "/embeddings", {"model": EMBED_MODEL, "input": texts, "encoding_format": "float"})
        try:
            await r.aread()
            data = r.json()["data"]
        finally:
            await r.aclose()
        return [d["embedding"] for d in sorted(data, key=lambda d: d["index"])]


async def _deltas(messages: list[dict]):
    """Yield (content, finish_reason). delta.reasoning_content is deliberately ignored."""
    body = {"model": CHAT_MODEL, "messages": messages, "stream": True,
            "max_tokens": CHAT_MAX_TOKENS, "reasoning_effort": REASONING_EFFORT, "temperature": 0.2}
    async with httpx.AsyncClient(base_url=BASE, headers=HEADERS, timeout=TIMEOUT) as c:
        r = await _open(c, "/chat/completions", body)
        try:
            async for line in r.aiter_lines():
                if not line.startswith("data:") or line.endswith("[DONE]"):
                    continue
                choices = json.loads(line[5:]).get("choices")
                if choices:
                    yield choices[0].get("delta", {}).get("content") or "", choices[0].get("finish_reason")
        finally:
            await r.aclose()


async def chat(messages: list[dict]):
    """Stream answer text with <think>...</think> blocks removed (tags may split across chunks)."""
    buf, thinking, emitted, finish = "", False, False, None
    async for d, fr in _deltas(messages):
        finish = fr or finish
        buf += d
        while True:
            if thinking:
                i = buf.find("</think>")
                if i < 0:
                    buf = buf[-7:]
                    break
                buf, thinking = buf[i + 8:], False
            else:
                i = buf.find("<think>")
                if i >= 0:
                    out, buf, thinking = buf[:i], buf[i + 7:], True
                    if out:
                        emitted = True
                        yield out
                    continue
                keep = next((k for k in range(6, 0, -1) if "<think>".startswith(buf[-k:])), 0)
                if len(buf) > keep:
                    emitted = True
                    yield buf[:len(buf) - keep]
                    buf = buf[len(buf) - keep:]
                break
    if buf and not thinking:
        emitted = True
        yield buf
    if finish == "length" and not emitted:
        raise NaNError("Answer budget exhausted while reasoning: raise CHAT_MAX_TOKENS or lower REASONING_EFFORT")
