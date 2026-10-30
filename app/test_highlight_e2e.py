"""Citation highlights end to end: real PyMuPDF, real Tesseract, real Postgres (scratch database); only the NaN API is stubbed.

Run (db must be up; creates and drops the scratch database `chatpdf_test`):
    docker compose run --rm --no-deps -T -v "$PWD/app:/srv:ro" -e PYTHONDONTWRITEBYTECODE=1 app python -W ignore test_highlight_e2e.py

Fixtures are drawn line by line at known positions, so the expected rectangles come from construction, not from the code under test.
"""
import json
import os
import random
import sys
from urllib.parse import urlsplit, urlunsplit

FS = 13
HELV = dict(fontname="helv", fontsize=FS)


def main():
    import psycopg
    base = os.environ["DATABASE_URL"]
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS chatpdf_test WITH (FORCE)")
        admin.execute("CREATE DATABASE chatpdf_test")
    os.environ.update(DATABASE_URL=urlunsplit(urlsplit(base)._replace(path="/chatpdf_test")), COOKIE_SECURE="false")
    os.environ.setdefault("SECRET_KEY", "t" * 32)
    os.environ.setdefault("NAN_API_KEY", "test")
    try:
        run(psycopg)
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute("DROP DATABASE IF EXISTS chatpdf_test WITH (FORCE)")


def put(fitz, page, x, y0, lines, lh=20):
    """Draw lines at known baselines. Returns [(word, ink_rect)] per line: the box the glyphs occupy."""
    out = []
    for i, line in enumerate(lines):
        y, cx, row = y0 + i * lh, x, []
        page.insert_text((x, y), line, **HELV)
        for w in line.split(" "):
            n = fitz.get_text_length(w, **HELV)
            row.append((w, fitz.Rect(cx, y - 0.75 * FS, cx + n, y + 0.2 * FS)))
            cx += n + fitz.get_text_length(" ", **HELV)
        out.append(row)
    return out


def phrase_rects(fitz, rows, phrase):
    """Per-line union of the boxes of `phrase` (consecutive words of the drawn text)."""
    flat = [(li, w, r) for li, row in enumerate(rows) for w, r in row]
    want = phrase.split()
    for i in range(len(flat) - len(want) + 1):
        if [w for _, w, _ in flat[i:i + len(want)]] == want:
            by_line = {}
            for li, _, r in flat[i:i + len(want)]:
                by_line[li] = by_line[li] | r if li in by_line else fitz.Rect(r)
            return [by_line[k] for k in sorted(by_line)]
    raise AssertionError(f"phrase not drawn: {phrase!r}")


def run(psycopg):
    import fitz
    from fastapi.testclient import TestClient

    import auth, highlight, ingest, llm, main

    # ---- NaN stub: constant embeddings (a small document is retrieved whole), scripted answers ----
    async def fake_embed(texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    script = {"answer": ""}

    async def fake_chat(messages):
        for tok in script["answer"].replace(" ", " \x00").split("\x00"):
            yield tok

    async def fake_rewrite(history, q):
        return q

    llm.embed, llm.chat, llm.rewrite = fake_embed, fake_chat, fake_rewrite

    # ---- text PDF: plain wrap, /Rotate 90, offset mediabox with a hyphenated line break, two columns ----
    T = fitz.open()
    p1 = T.new_page(width=612, height=792)
    rows1 = put(fitz, p1, 72, 100, ["Quarterly report.", "Revenue grew strongly in the third quarter because the new",
                                    "subscription plan attracted many customers across Europe. Costs",
                                    "remained flat during the same period. The board approved the", "annual budget in November."])
    p2 = T.new_page(width=612, height=792)
    rows2 = put(fitz, p2, 72, 100, ["Annual overview.", "Operating costs fell sharply across every region during the",
                                    "whole of last year. Headcount stayed constant."])
    p2.set_rotation(90)
    p3 = T.new_page(width=612, height=792)
    rows3 = put(fitz, p3, 72, 100, ["The comprehensive infor-", "mation system stores every customer record. Backups run", "nightly."])
    p3.set_mediabox(fitz.Rect(40, 0, 612, 742))  # does not start at 0,0: displayed = drawn - (40, 50)
    p4 = T.new_page(width=612, height=792)
    left = put(fitz, p4, 50, 100, ["Methods. We surveyed two thousand", "households in the northern region and", "then compared the answers with national", "census figures from"])
    right = put(fitz, p4, 340, 100, ["the previous decade. Results show", "that household income rose faster", "than expected."])
    assert max(r.x1 for row in left for _, r in row) < 320, "fixture: the columns must not overlap"
    text_pdf = T.tobytes()
    Tdoc = fitz.open("pdf", text_pdf)

    # ---- scanned PDF: image-only pages rendered from known text; page 2 is stored sideways with /Rotate 90 ----
    S = fitz.open()
    sp = S.new_page(width=612, height=792)
    srows = put(fitz, sp, 72, 100, ["Maintenance windows are scheduled every second Sunday", "of the month at dawn. Customers receive a warning email",
                                    "three days earlier. Emergency patches are exempt."], lh=24)
    sp2 = S.new_page(width=612, height=792)
    srows2 = put(fitz, sp2, 72, 100, ["Shipping takes five business days within Spain.", "International orders may take two weeks."], lh=24)
    scan = fitz.open()
    pg = scan.new_page(width=612, height=792)
    pg.insert_image(pg.rect, pixmap=S[0].get_pixmap(dpi=200))
    S[1].set_rotation(270)  # drawn upright, displayed turned 270 degrees
    side = S[1].get_pixmap(dpi=200)
    pg = scan.new_page(width=side.width * 72 / 200, height=side.height * 72 / 200)
    pg.insert_image(pg.rect, pixmap=side)
    pg.set_rotation(90)  # turns it back: the page DISPLAYS upright but is stored sideways
    scan_pdf = scan.tobytes()
    Sdoc = fitz.open("pdf", scan_pdf)
    assert (Sdoc[1].rect.width, Sdoc[1].rect.height) == (612, 792) and Sdoc[1].rotation == 90

    def ink(doc, n, r):
        """Share of dark pixels under r on the page as displayed."""
        pix = doc[n - 1].get_pixmap(dpi=144)
        x0, y0, x1, y1 = [int(v * 2) for v in r]
        dark = total = 0
        for y in range(max(y0, 0), min(y1, pix.height)):
            for x in range(max(x0, 0), min(x1, pix.width)):
                total += 1
                dark += sum(pix.pixel(x, y)[:3]) < 300
        return dark / max(total, 1)

    def events(r):
        out = {}
        for block in r.text.strip().split("\n\n"):
            ev, payload = block.split("\n")
            out.setdefault(ev[7:], []).append(json.loads(payload[6:]))
        return out

    def covers(rects, expected, doc, n, tol, label):
        """One returned rectangle per expected line, covering its glyphs, hugging its x extent, sitting on ink, inside the page."""
        assert len(rects) == len(expected), f"{label}: {len(rects)} rects for {len(expected)} lines: {rects}"
        page = doc[n - 1].rect
        for got, want in zip(rects, expected):
            g = fitz.Rect(got)
            assert g.x0 <= want.x0 + tol and g.x1 >= want.x1 - tol and g.y0 <= want.y0 + tol and g.y1 >= want.y1 - tol, f"{label}: {got} misses {want}"
            flat = want.width >= want.height  # text runs along x (else the page is turned and it runs along y)
            along = (g.x0 >= want.x0 - tol and g.x1 <= want.x1 + tol) if flat else (g.y0 >= want.y0 - tol and g.y1 <= want.y1 + tol)
            across = g.height <= 2.2 * want.height if flat else g.width <= 2.2 * want.width
            assert along and across, f"{label}: {got} is longer or thicker than {want}"
            assert page.contains(g), f"{label}: {got} outside page {page}"
            assert ink(doc, n, g) > 0.04, f"{label}: no ink under {got}"

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as db, TestClient(main.app, base_url="http://testserver") as c:
        db.execute("INSERT INTO users (username, password_hash) VALUES ('t', %s)", (auth.hasher.hash("password-12345"),))
        assert c.post("/api/login", json={"username": "t", "password": "password-12345"}).status_code == 200

        def upload(data, name):
            r = c.post("/api/documents", params={"name": name}, content=data)
            assert r.status_code == 201, r.text
            doc = r.json()["id"]
            assert db.execute("SELECT status FROM documents WHERE id = %s", (doc,)).fetchone()[0] == "ready", \
                db.execute("SELECT error FROM documents WHERE id = %s", (doc,)).fetchone()
            return doc

        def ask(doc, answer):
            script["answer"] = answer
            c.delete(f"/api/documents/{doc}/messages")
            r = c.post("/api/chat", json={"document_id": doc, "question": "q"})
            ev = events(r)
            assert "error" not in ev, ev
            return ev["citations"][0][0], ev  # one event, one cited page per answer here

        # 1. text PDF: page_words for every page, as displayed; chunks exactly as before
        tdoc = upload(text_pdf, "text.pdf")
        got = db.execute("SELECT page, width, height, jsonb_array_length(words) FROM page_words WHERE document_id = %s ORDER BY page", (tdoc,)).fetchall()
        assert [(p, w, h) for p, w, h, _ in got] == [(1, 612, 792), (2, 792, 612), (3, 572, 742), (4, 612, 792)], got
        assert all(n > 0 for *_, n in got)
        expect = ingest.chunk([(i, ingest.clean(pg.get_text()).strip()) for i, pg in enumerate(Tdoc, 1)])
        have = db.execute("SELECT page, content FROM chunks WHERE document_id = %s ORDER BY id", (tdoc,)).fetchall()
        assert have == expect, "chunks changed"
        assert db.execute("SELECT ocr_pages FROM documents WHERE id = %s", (tdoc,)).fetchone()[0] == 0
        print("ok text PDF: page_words for all 4 pages with displayed sizes (rotated page 792x612, offset page 572x742); chunks identical to a plain get_text() chunking")

        # 2. highlight per page: plain, rotated, offset + hyphenation, two columns
        cit, ev = ask(tdoc, "Revenue grew strongly in Q3 thanks to the new subscription plan [p. 1].")
        hl = cit["highlight"]
        assert hl and hl["page"] == 1 and (hl["width"], hl["height"]) == (612, 792)
        covers(hl["rects"], phrase_rects(fitz, rows1, "Revenue grew strongly in the third quarter because the new subscription plan attracted "
                                         "many customers across Europe."), Tdoc, 1, 3, "plain page")
        assert sorted(s["page"] for s in ev["sources"][0]) == [1, 2, 3, 4], "sources event unchanged"
        print("ok text page: the Revenue sentence is highlighted as two line rectangles on the right words")

        cit, _ = ask(tdoc, "Costs fell sharply in every region [p. 2].")
        hl = cit["highlight"]
        assert hl and (hl["width"], hl["height"]) == (792, 612)
        rot = Tdoc[1].rotation_matrix
        want = [(r * rot).normalize() for r in phrase_rects(fitz, rows2, "Operating costs fell sharply across every region during the whole of last year.")]
        assert want[0].height > want[0].width / 3  # the page really is turned: the 'line' is a column of the displayed page
        covers(hl["rects"], want, Tdoc, 2, 3, "rotated page")
        print("ok /Rotate 90 page: rectangles follow the displayed (turned) text")

        cit, _ = ask(tdoc, "The information system stores all customer records [p. 3].")
        hl = cit["highlight"]
        assert hl and (hl["width"], hl["height"]) == (572, 742)
        want = [fitz.Rect(r.x0 - 40, r.y0 - 50, r.x1 - 40, r.y1 - 50)
                for r in phrase_rects(fitz, rows3, "The comprehensive infor- mation system stores every customer record.")]
        covers(hl["rects"], want, Tdoc, 3, 3, "offset mediabox + hyphenation")
        print("ok offset mediabox page with a line-end hyphen (infor- / mation): shifted rectangles, sentence found across the break")

        cit, _ = ask(tdoc, "They surveyed two thousand households and compared the answers with census figures [p. 4].")
        hl = cit["highlight"]
        assert hl
        want = phrase_rects(fitz, left + right, "We surveyed two thousand households in the northern region and then compared the answers with "
                                                "national census figures from the previous decade.")
        covers(hl["rects"], want, Tdoc, 4, 3, "two columns")
        assert len(hl["rects"]) == 5 and not any(r[0] < 320 < r[2] for r in hl["rects"]), "a rectangle spans both columns"
        print("ok two-column page: 4 rectangles in the left column + 1 in the right, none spanning the gutter")

        # 3. no match, no page_words, one failing page
        assert ask(tdoc, "Employees enjoy generous parental leave and free lunches [p. 1].")[0]["highlight"] is None
        cit, _ = ask(tdoc, "Revenue grew strongly in the third quarter [p. 1].")
        stored = c.get(f"/api/documents/{tdoc}/messages").json()[-1]["citations"]
        assert stored == [cit] and stored[0]["highlight"]["rects"], "stored citations must equal the streamed ones, highlight included"
        db.execute("DELETE FROM page_words WHERE document_id = %s", (tdoc,))
        cit, ev = ask(tdoc, "Revenue grew strongly in the third quarter [p. 1].")
        assert cit["highlight"] is None and cit["text"] and "token" in ev, "documents without page_words keep working"
        print("ok unrelated claim -> highlight null; stored citations == streamed; document without page_words -> null, answer unaffected")

        orig = fitz.Page.get_text

        def flaky(self, *a, **kw):
            if a and a[0] == "words" and self.number == 1:
                raise RuntimeError("boom")
            return orig(self, *a, **kw)
        fitz.Page.get_text = flaky
        try:
            fdoc = upload(text_pdf, "flaky.pdf")
        finally:
            fitz.Page.get_text = orig
        assert [r[0] for r in db.execute("SELECT page FROM page_words WHERE document_id = %s ORDER BY page", (fdoc,)).fetchall()] == [1, 3, 4]
        assert db.execute("SELECT count(*) FROM chunks WHERE document_id = %s", (fdoc,)).fetchone()[0] == len(expect)
        print("ok word extraction failing on page 2: document still indexes, all chunks present, that page alone has no page_words")

        # 4. scanned PDF: OCR once, words from the same pass
        sdoc = upload(scan_pdf, "scan.pdf")
        assert db.execute("SELECT ocr_pages FROM documents WHERE id = %s", (sdoc,)).fetchone()[0] == 2
        got = db.execute("SELECT page, width, height, words FROM page_words WHERE document_id = %s ORDER BY page", (sdoc,)).fetchall()
        assert [(p, w, h) for p, w, h, _ in got] == [(1, 612, 792), (2, 612, 792)], [(p, w, h) for p, w, h, _ in got]
        chunks = dict(db.execute("SELECT page, content FROM chunks WHERE document_id = %s", (sdoc,)).fetchall())
        for p, _, _, words in got:
            assert " ".join(w[4] for w in words).split() == chunks[p].split(), f"page {p}: indexed text and boxes differ"
        print("ok scanned PDF: 2 OCR pages; for each, the box words equal the indexed chunk text (one OCR pass)")

        cit, _ = ask(sdoc, "Maintenance is scheduled every second Sunday of the month at dawn [p. 1].")
        hl = cit["highlight"]
        assert hl and (hl["width"], hl["height"]) == (612, 792)
        want = phrase_rects(fitz, srows, "Maintenance windows are scheduled every second Sunday of the month at dawn.")
        covers(hl["rects"], want, Sdoc, 1, 6, "scanned page")
        cit, _ = ask(sdoc, "Shipping takes five days inside Spain [p. 2].")
        hl = cit["highlight"]
        assert hl and (hl["width"], hl["height"]) == (612, 792)
        covers(hl["rects"], phrase_rects(fitz, srows2, "Shipping takes five business days within Spain."), Sdoc, 2, 6, "scanned /Rotate 90 page")
        print("ok scanned pages (upright and stored sideways with /Rotate 90): rectangles sit on the recognized sentence")

        # 5. cascade
        for d in (tdoc, fdoc, sdoc):
            assert c.delete(f"/api/documents/{d}").status_code == 204
        assert db.execute("SELECT count(*) FROM page_words").fetchone()[0] == 0
        print("ok deleting documents deletes their page_words")

        # 6. size of page_words for a 50-page document (dense pages)
        rng = random.Random(7)
        vocab = [("w" + str(i) * (1 + i % 4)) for i in range(400)] + "the of and to in a is that for it as was with be by on not he this are or his from at which".split()
        big = fitz.open()
        for _ in range(50):
            words = [rng.choice(vocab) for _ in range(480)]
            text = ". ".join(" ".join(words[i:i + 12]) for i in range(0, 480, 12)) + "."
            big.new_page().insert_textbox(fitz.Rect(40, 40, 572, 752), text, fontsize=8.5)
        bdoc = upload(big.tobytes(), "big50.pdf")
        n_words, raw, stored, total = db.execute(
            "SELECT sum(jsonb_array_length(words)), sum(length(words::text)), sum(pg_column_size(words)), "
            "pg_total_relation_size('page_words') FROM page_words WHERE document_id = %s", (bdoc,)).fetchone()
        print(f"SIZE 50 pages x ~480 words: {n_words} words, JSON text {raw / 1024:.0f} KiB, stored (compressed) {stored / 1024:.0f} KiB, "
              f"whole table with index {total / 1024:.0f} KiB (~{total / 50 / 1024:.1f} KiB per page)")
        fb = db.execute("SELECT count(*) FROM chunks WHERE document_id = %s", (bdoc,)).fetchone()[0]
        print(f"     (the same document has {fb} chunks)")
        assert c.delete(f"/api/documents/{bdoc}").status_code == 204
    print(f"MIN_SCORE={highlight.MIN_SCORE} MIN_COVERAGE={highlight.MIN_COVERAGE}")
    print("ALL PASSED")


if __name__ == "__main__":
    sys.exit(main())
