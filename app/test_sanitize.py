"""NUL / surrogate sanitizing, against the real Postgres of the compose stack.

Run (db must be up; uses a scratch database `chatpdf_test`, created and dropped here; NaN API is stubbed):
    docker compose run --rm --no-deps app python test_sanitize.py
"""
import os
import sys
from urllib.parse import urlsplit, urlunsplit


def make_pdf(pages: list[str]) -> bytes:
    """Hand-built PDF with a Type0/Identity-H font. PyMuPDF reads each 2-byte UTF-16 code as two characters,
    so "Invoice" comes out as "\\x00I\\x00n..." (NULs interleaved with the text), like the Wikipedia print that broke indexing.
    A page of "\\x00" characters comes out as pure NULs."""
    n = len(pages)
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            f"<< /Type /Pages /Kids [{' '.join(f'{3 + 2 * i} 0 R' for i in range(n))}] /Count {n} >>".encode()]
    for i, text in enumerate(pages):
        objs.append(f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Contents {4 + 2 * i} 0 R "
                    f"/Resources << /Font << /F1 {3 + 2 * n} 0 R >> >> >>".encode())
        body = b"BT /F1 14 Tf 72 700 Td <" + text.encode("utf-16-be").hex().encode() + b"> Tj ET"
        objs.append(b"<< /Length %d >>\nstream\n" % len(body) + body + b"\nendstream")
    objs.append(f"<< /Type /Font /Subtype /Type0 /BaseFont /Helvetica /Encoding /Identity-H "
                f"/DescendantFonts [{4 + 2 * n} 0 R] >>".encode())
    objs.append(b"<< /Type /Font /Subtype /CIDFontType2 /BaseFont /Helvetica "
                b"/CIDSystemInfo << /Registry (Adobe) /Ordering (Identity) /Supplement 0 >> /DW 600 >>")
    out, offsets = b"%PDF-1.4\n", []
    for k, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % k + o + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objs) + 1) + b"".join(b"%010d 00000 n \n" % o for o in offsets)
    return out + b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF" % (len(objs) + 1, xref)


def main():
    import psycopg
    base = os.environ["DATABASE_URL"]
    test_url = urlunsplit(urlsplit(base)._replace(path="/chatpdf_test"))
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS chatpdf_test WITH (FORCE)")
        admin.execute("CREATE DATABASE chatpdf_test")
    os.environ.update(DATABASE_URL=test_url, COOKIE_SECURE="false")
    os.environ.setdefault("SECRET_KEY", "t" * 32)
    os.environ.setdefault("NAN_API_KEY", "test")
    try:
        run(psycopg)
    finally:
        import ingest
        if ingest._pool:
            ingest._pool.shutdown()
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute("DROP DATABASE IF EXISTS chatpdf_test WITH (FORCE)")


def run(psycopg):
    import fitz
    from fastapi.testclient import TestClient

    import auth, ingest, llm, main

    async def fake_embed(texts):  # no NaN API call; 4-dim vectors
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]
    llm.embed = fake_embed

    # 1. helper
    assert ingest.clean("a\x00b\ud800c") == "abc"
    assert ingest.clean("Ñandú, café, 中文\n") == "Ñandú, café, 中文\n"  # untouched without NUL
    print("ok helper: NUL and lone surrogate removed, normal text unchanged")

    # 2. the PDF really yields NUL when read raw (otherwise the test proves nothing)
    pdf = make_pdf(["Invoice total amount due for the month of March"])
    assert "\x00" in fitz.open(stream=pdf, filetype="pdf")[0].get_text(), "test PDF does not produce NUL"
    text = ingest._text_layer(pdf)[0]
    assert "\x00" not in text and "Invoice" in text, repr(text)
    print("ok extraction: raw text has NUL, extract_pages output does not")

    # 3. surrogate + NUL text chunks and inserts
    chunks = ingest.chunk([(1, ingest.clean("alpha\x00 beta \ud800 gamma"))])
    assert chunks == [(1, "alpha beta gamma")], chunks

    with psycopg.connect(os.environ["DATABASE_URL"]) as db, TestClient(main.app, base_url="http://testserver") as c:
        db.autocommit = True
        db.execute("INSERT INTO users (username, password_hash) VALUES ('t', %s)", (auth.hasher.hash("password-12345"),))
        assert c.post("/api/login", json={"username": "t", "password": "password-12345"}).status_code == 200

        def upload(body, name):
            r = c.post("/api/documents", params={"name": name}, content=body)
            assert r.status_code == 201, r.text
            return r.json()

        def row(doc_id):
            return db.execute("SELECT name, status, ocr_pages, error FROM documents WHERE id = %s", (doc_id,)).fetchone()

        def n_chunks(doc_id):
            return db.execute("SELECT count(*) FROM chunks WHERE document_id = %s", (doc_id,)).fetchone()[0]

        # 4. end to end: NUL PDF + NUL in the name indexes fine, name sanitized, no OCR for a real text layer
        d = upload(pdf, "wiki\x00pedia.pdf")
        name, status, ocr, err = row(d["id"])
        assert (name, status, ocr, err) == ("wikipedia.pdf", "ready", 0, None), (name, status, ocr, err)
        assert n_chunks(d["id"]) >= 1 and ingest._pool is None
        print("ok upload: NUL PDF indexed, name sanitized, no OCR ran")

        # 5. a page that is only NULs is junk, not content: it must go to OCR
        d = upload(make_pdf(["Invoice total amount due for the month of March", "\x00" * 40]), "junk.pdf")
        assert row(d["id"])[1] == "ready" and ingest._pool is not None
        print("ok 25-char rule: a page of only NULs was sent to OCR")

        # 6. failed insert leaves nothing behind, document is 'failed' with a message
        real_chunk = ingest.chunk
        ingest.chunk = lambda pages: [(1, "fine chunk"), (1, "bad \x00 chunk")]  # bypass sanitizing on purpose
        try:
            d = upload(pdf, "boom.pdf")
        finally:
            ingest.chunk = real_chunk
        name, status, ocr, err = row(d["id"])
        assert status == "failed" and err, (status, err)
        assert n_chunks(d["id"]) == 0, "partial chunks left behind"
        print(f"ok failure: status={status!r} error={err!r}, 0 chunks left (insert rolled back)")
    print("ALL PASSED")


if __name__ == "__main__":
    sys.exit(main())
