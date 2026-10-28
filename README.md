# chatpdf

Upload a PDF, index it, chat with it. Streaming answers cite pages as `[p. N]`; click a citation to see the source snippet.

Stack: FastAPI, Postgres + pgvector, plain HTML/JS. LLM `qwen3.6` and embeddings `qwen3-embedding` via https://api.nan.builders/v1.

## Run locally

```sh
cp .env.example .env        # fill NAN_API_KEY, POSTGRES_PASSWORD, BASIC_AUTH_USERS
docker network create n8n_default   # only if it doesn't exist (compose declares it external)
docker compose up --build
```

Open http://localhost:8000. (Basic auth only applies through Traefik, not on this local port.)

## Production

Same compose file on the VPS: Traefik routes `puchunipdf.daniszwarc.cloud` over the `n8n_default` network with the `mytlschallenge` resolver and a basicauth middleware fed by `BASIC_AUTH_USERS` (`htpasswd -nb user pass`, keep it single-quoted in `.env`).

## NaN API settings

All optional, read by the backend only (see `.env.example`).

| Variable | Default | |
|---|---|---|
| `CHAT_MODEL` | `qwen3.6` | chat model id |
| `EMBED_MODEL` | `qwen3-embedding` | embedding model id (4096 dims) |
| `REASONING_EFFORT` | `none` | sent as `reasoning_effort`. Only `delta.content` reaches the UI; `reasoning_content` is dropped |
| `CHAT_MAX_TOKENS` | `8000` | reasoning tokens count against this. Too small with reasoning on → empty content, reported as an error |

Calls retry on 429/5xx (up to 4 attempts, 1s/2s/4s backoff) and never on 4xx. 401/402/404 show as "NaN API key invalid", "NaN token quota exhausted for this model" and "model id not found". Embeddings go out in batches of at most 32 (`encoding_format: float`); NaN allows 60 requests/min per key.

## API

| Route | |
|---|---|
| `POST /api/documents?name=file.pdf` | body = raw PDF bytes. Extracts text per page, chunks (~350 words, 40 overlap, never across pages), embeds, stores. |
| `GET /api/documents` | list |
| `DELETE /api/documents/{id}` | delete document and its chunks |
| `POST /api/chat` | `{document_id, question}` → SSE events `sources`, `token`, `error`, `done` |

## Notes

- The vector column size is taken from a real embeddings call at startup (the first time, it creates the table). If you change embedding model, drop the `chunks` table.
- Chunk size uses words as a proxy for tokens (no tokenizer dependency).
- The API key is read only by the backend; the frontend never sees it.
- Scanned PDFs (no text layer) are rejected: no OCR.
