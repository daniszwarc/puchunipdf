# chatpdf

Upload a PDF, index it, chat with it. Streaming answers cite pages as `[p. N]`; click a citation to see the source snippet.

Stack: FastAPI, Postgres + pgvector, plain HTML/JS. LLM `qwen3.6` and embeddings `qwen3-embedding` via https://api.nan.builders/v1.

## Run locally

Copy `.env.example` to `.env` **before** running `docker compose up`: the app container reads all its settings from `.env`.

```sh
cp .env.example .env        # fill NAN_API_KEY, POSTGRES_PASSWORD, SECRET_KEY
docker network create n8n_default   # only if it doesn't exist (compose declares it external)
docker compose up --build
```

Generate the session key and put it in `.env` as `SECRET_KEY` (the app refuses to start without 32+ characters):

```sh
openssl rand -hex 32
```

For plain-http local use set `COOKIE_SECURE=false` (Secure cookies need https). Then create the two users and open http://localhost:8000:

```sh
docker compose exec app python manage.py create-user <name>     # prompts twice, min 12 chars
docker compose exec app python manage.py set-password <name>
```

## Production

Same compose file on the VPS: Traefik routes `puchunipdf.daniszwarc.cloud` over the `n8n_default` network with the `mytlschallenge` resolver. Authentication is the app's own login (below); both users see all documents.

## PDF viewer

The page is a document viewer (left, ~55%) and the chat (right, ~45%). On screens narrower than 900 px they become two tabs, "Document" and "Chat". Clicking a `[p. N]` citation scrolls the viewer to that page and flashes it; the small caret next to the chip still expands the source excerpt.

- **pdf.js is vendored**, not loaded from a CDN, and needs no build step: `app/static/vendor/pdfjs/` holds `pdfjs-dist` **6.4.299** (Apache-2.0; `pdf.min.mjs`, `pdf.worker.min.mjs`, the image-codec `wasm/` files and the licenses). The version is also in `vendor/pdfjs/VERSION`. To upgrade, download the new tarball from the npm registry, verify its checksum, and replace those files.
- `getDocument` is called with `isEvalSupported: false`. pdf.js 6.x no longer has an eval code path (the option is a no-op there).
- **Originals are stored in Postgres** (table `document_files`, `bytea`, same 50 MB limit as uploads). They are saved when the upload arrives, before indexing starts, so the viewer works while a document is still indexing. Deleting a document deletes its original. Back them up with the database.
- **Documents uploaded before this feature have no original.** The viewer shows "Original not stored for this document. Upload it again to view it."; chat on them keeps working.
- Not vendored: pdf.js CMaps and standard font data. Rare PDFs that rely on predefined CJK CMaps or non-embedded standard fonts may render with substitute fonts.

## Citation highlights

Clicking a `[p. N]` chip also highlights, on page N of the original, the sentence that supports the answer: yellow rectangles over the page, one per text line, the page scrolls so the highlight is centered, then it pulses once. It works for text PDFs and for scanned (OCR) pages; the viewer always shows the original page, never a text replacement. If no sentence can be placed, the chip just jumps and flashes the page as before and a small hint says "Highlight not available for this citation".

How it works (no extra model call; the chat prompt, retrieval, chunking and embeddings are untouched):

1. **At indexing**, the word boxes of every page are stored in `page_words` (`document_id`, `page`, `width`, `height`, `words` as JSON `[x0, y0, x1, y1, text]`, in reading order). Boxes are in PDF points **as displayed** (page rotation applied, mediabox offset normalized). Text pages use the PyMuPDF text layer; OCR pages use the words of the **same OCR pass** that produced the indexed text, so there is no second OCR run. If word extraction fails for a page, indexing still succeeds and that page just has no highlight. About 12 KiB per dense page (a 50-page document with ~480 words per page: 24,000 words, ~600 KiB including the index).
2. **When an answer finishes**, for each cited page the server picks, among the retrieved chunks of that page, the sentence (or two consecutive sentences, at most 400 characters) closest to the answer sentence that contains the chip. Closeness is a token overlap score over lowercase, accent-free words without Spanish/French/English stopwords; the score weighs how much of the claim is covered four times as much as how much unrelated text the candidate carries (F2). Below `MIN_SCORE` there is no highlight.
3. The chosen sentence is aligned to the page words (exact contiguous match first, otherwise the best-overlap window that covers at least `MIN_COVERAGE` of its content words), tolerating line-end hyphenation, soft hyphens, ligatures (fi, fl, ff), curly quotes and non-breaking spaces. The matched word boxes are merged into one rectangle per line (lines turned by /Rotate 90/270 or 180 included, and a column change starts a new rectangle).
4. The result is added to the citation as `highlight: {page, width, height, rects: [[x0, y0, x1, y1], ...]}` or `null`, streamed in a new `citations` event after the answer and stored with the message in `chat_messages.citations`.

Constants (top of `app/highlight.py`): `MIN_SCORE = 0.35` (on the fixtures, correct sentences score 0.65 to 0.97, paraphrases and unrelated claims 0.00 to 0.14), `MIN_COVERAGE = 0.7`, `MAX_CHARS = 400`. Raise `MIN_SCORE` for fewer, surer highlights; lower it for more.

Limits:

- **Closest-sentence logic, not proof.** The highlighted sentence is the one most similar to what the model wrote, which is not always the one it used. A sentence that cites several pages is scored as a whole.
- **Answers in another language than the document** share few words with the source (names, numbers and cognates aside), so they often score below the threshold and get no highlight. Heavy paraphrases likewise.
- **Documents uploaded before this feature have no `page_words`**, so their citations have no highlight (upload them again). Older chat messages keep working without one.
- **No text selection** in the viewer: there is no pdf.js text layer, only the overlay.
- Scanned pages depend on OCR quality: misread words reduce coverage, and very noisy pages may get no highlight.
- Rectangles are positioned as percentages of the page box, so zoom, fit width and resize keep them aligned without recomputation.

## Conversation memory

Each user's conversation with each document is saved in Postgres (table `chat_messages`, per user and document) and reloaded when you select the document again. Documents stay shared, conversations do not: you only ever see your own. **New chat** (top of the chat pane) deletes your conversation for that document for good; deleting a document deletes everyone's messages for it.

- The last `CHAT_HISTORY_TURNS` turns (default 8) are sent to the model as earlier user and assistant messages. Answers still come only from the fragments retrieved for the new question, and the fragments themselves are never stored (only the question, the answer and the pages it cites, with their excerpts and highlight).
- For follow-ups ("and the second one?"), one extra short non-streaming call to `CHAT_MODEL` (reasoning off, temperature 0) rewrites the question into a standalone one. It is used only to search the document; the model still answers your original wording. If that call fails, the original question is used. The first question of a conversation makes no extra call.
- A turn is saved only after the answer finishes. If the stream fails, is cut off, or hits `CHAT_MAX_TOKENS`, nothing is saved and an error is shown.
- The NaN key allows 60 requests per minute and 5 concurrent: follow-ups cost two chat calls (rewrite + answer) plus one embedding call.

## Authentication

Username + password (argon2), signed session cookie (HttpOnly, SameSite=Lax, `Secure` unless `COOKIE_SECURE=false`, 30 days). Everything except `/login`, `POST /api/login` and `/health` needs a session: API calls get 401, pages redirect to `/login`. POST/DELETE requests with a mismatching `Origin` get 403. After 5 failed logins for the same client IP + username, that pair gets 429 for 15 minutes (in memory: resets on restart). The client IP comes from `X-Forwarded-For`, trusted only from `172.16.0.0/12` (uvicorn `--forwarded-allow-ips` in the Dockerfile): adjust if your Traefik network uses another range.

## NaN API settings

All optional, read by the backend only (see `.env.example`).

| Variable | Default | |
|---|---|---|
| `CHAT_MODEL` | `qwen3.6` | chat model id |
| `EMBED_MODEL` | `qwen3-embedding` | embedding model id (4096 dims) |
| `REASONING_EFFORT` | `none` | sent as `reasoning_effort`. Only `delta.content` reaches the UI; `reasoning_content` is dropped |
| `CHAT_MAX_TOKENS` | `8000` | reasoning tokens count against this. Too small with reasoning on → empty content, reported as an error |

Calls retry on 429/5xx (up to 4 attempts, 1s/2s/4s backoff) and never on 4xx. 401/402/404 show as "NaN API key invalid", "NaN token quota exhausted for this model" and "model id not found". Embeddings go out in batches of at most 32 (`encoding_format: float`); NaN allows 60 requests/min per key.

## OCR

A page is OCR'd only if its text layer has fewer than 25 non-whitespace characters; real text layers are used as-is, so text PDFs never touch OCR. Pages recognized this way are marked with an "OCR" badge in the document list (text may contain errors).

| Variable | Default | |
|---|---|---|
| `OCR_DPI` | `200` | render resolution for OCR |
| `OCR_WORKERS` | `2` (`.env.example` sets `1`) | OCR worker processes, each ~single-threaded and ~100 MB resident |
| `OCR_MAX_PAGES` | `150` | documents needing more OCR pages than this fail with a clear message |

The first image build is slower and the image larger because it installs Tesseract and the Spanish/French language data.

## API

| Route | |
|---|---|
| `POST /api/documents?name=file.pdf` | body = raw PDF bytes. Returns 201 `{id, status: "indexing"}` immediately; text extraction (+ OCR), chunking (~350 words, 40 overlap, never across pages) and embedding continue in the background. |
| `GET /api/documents` | list with `status` (`indexing`/`ready`/`failed`), `ocr_pages`, `error`, and `done_pages`/`total_pages` while indexing |
| `GET /api/documents/{id}/file` | the original PDF (`application/pdf`, inline, `Cache-Control: private`); 404 if not stored |
| `DELETE /api/documents/{id}` | delete document, its chunks and its stored original |
| `POST /api/chat` | `{document_id, question}` → SSE events `sources` (retrieved chunks), `token`, `citations` (one `{page, text, highlight}` per cited page, after the answer), `error`, `done` |

## Notes

- The vector column size is taken from a real embeddings call at startup (the first time, it creates the table). If you change embedding model, drop the `chunks` table.
- Chunk size uses words as a proxy for tokens (no tokenizer dependency).
- The API key is read only by the backend; the frontend never sees it.
- Scanned pages are OCR'd (Tesseract via PyMuPDF, Spanish/French/English). Indexing runs in the background; the UI polls and shows progress, and chat unlocks when the document is ready.
- A restart during indexing marks that document as failed ("interrupted, upload again").
- Scanned pages stored with `/Rotate 270` get no OCR text with the PyMuPDF version in the image (`get_textpage_ocr` returns nothing for them, while Tesseract on the displayed page reads them fine), so they have neither text nor highlights. Upright, 90° and 180° scans work.
