"""Keyword channel (keywords.py): unit tests, then DB-backed tests through the real /api/chat.

Run:
    unit only (stdlib, Python 3.10+):  python app/test_keywords.py --unit
    everything (db must be up; creates and drops the scratch database `chatpdf_test`):
        docker compose run --rm --no-deps -T -v "$PWD/app:/srv:ro" -e PYTHONDONTWRITEBYTECODE=1 app python -W ignore test_keywords.py
"""
import json
import os
import sys
from urllib.parse import urlsplit, urlunsplit

import keywords as kw

Q = "me pasarías el índice del libro?"
TOC = ("9 | Intro | Glue Mágico para Guitarristas Índice Puesta en Marcha 10 El 1er problema del guitarrista 21 "
       "Los acordes que nadie te enseñó 34 Ritmo y mano derecha 52 Improvisar sin miedo 77")


def unit():
    # the real failing question: generic words and unknown words are not searched for noise; "libro" stays but is weighted per document
    assert kw.terms(Q) == ["pasarias", "indice", "libro"], kw.terms(Q)
    assert kw.terms("ÍNDICE") == kw.terms("indice") == kw.terms("Índice?") == ["indice"]
    print("ok terms: stopwords (me, el, del) dropped, accents and case folded")

    # every word a stopword (or nothing searchable): no keyword channel
    assert kw.terms("¿Y el de la?") == [] and kw.terms("?!  ...") == [] and kw.terms("") == []
    assert kw.terms("what is it about the of") == []
    print("ok terms: all-stopword question -> []")

    # folding of ñ ç ü œ (the SQL side is checked against Postgres in the DB block)
    assert kw.terms("español años cœur über ça") == ["espanol", "anos", "coeur", "uber", "ca"]
    assert kw.terms("a 7 año año") == ["7", "ano"]  # single letters dropped, digits kept, duplicates once
    assert len(kw.terms(" ".join(f"palabra{i}" for i in range(50)))) == kw.MAX_TERMS
    print("ok terms: ñ ç ü œ folded, duplicates, digits, MAX_TERMS")

    # patterns: only [a-z0-9] reaches them; 4+ letters match word starts, shorter ones whole words
    assert kw.pattern("indice") == r"\yindice" and kw.pattern("ley") == r"\yley\y"
    assert all(c.isalnum() for c in "".join(kw.terms("a'b; DROP TABLE x -- (y) [z] .* ^$ \\y")))
    print("ok patterns")

    # pick: relative cutoff and KEYWORD_K
    assert kw.pick([]) == []
    assert kw.pick([(1, "a", 4.0), (2, "b", 2.0), (3, "c", 1.9), (4, "d", 1.0)]) == [(1, "a"), (2, "b")]
    assert kw.pick([(i, str(i), 1.0) for i in range(10)]) == [(i, str(i)) for i in range(kw.KEYWORD_K)]
    print("ok pick: cutoff 0.5 of the best, at most 3")

    # fuse
    v = [(p, f"v{p}") for p in range(1, 9)]
    assert kw.fuse(v, []) == v and kw.fuse(v, []) is not v  # no hits: exactly today's list
    assert kw.fuse(v, [(3, "v3")]) == [(3, "v3")] + [x for x in v if x != (3, "v3")]  # in both lists: boosted, no duplicate
    out = kw.fuse(v, [(90, "k1"), (91, "k2"), (92, "k3")])
    assert len(out) == kw.FINAL_K == 10 and out[:5] != v[:5] and set(v[:7]) <= set(out) and (8, "v8") not in out, out
    assert out.index((90, "k1")) == 1 and out.index((1, "v1")) == 0  # equal rank: vector first, then keyword
    assert kw.fuse(v, [(90, "k1")]) == [v[0], (90, "k1")] + v[1:]
    print("ok fuse: RRF order, dedupe, tie -> vector first, cap 10 drops only the weakest vector chunk")


def main():
    unit()
    if "--unit" in sys.argv:
        print("UNIT PASSED (db block skipped)")
        return
    import psycopg
    base = os.environ["DATABASE_URL"]
    with psycopg.connect(base, autocommit=True) as admin:
        admin.execute("DROP DATABASE IF EXISTS chatpdf_test WITH (FORCE)")
        admin.execute("CREATE DATABASE chatpdf_test")
    os.environ.update(DATABASE_URL=urlunsplit(urlsplit(base)._replace(path="/chatpdf_test")), COOKIE_SECURE="false")
    os.environ.setdefault("SECRET_KEY", "t" * 32)
    os.environ.setdefault("NAN_API_KEY", "test")
    try:
        db_block(psycopg)
    finally:
        with psycopg.connect(base, autocommit=True) as admin:
            admin.execute("DROP DATABASE IF EXISTS chatpdf_test WITH (FORCE)")
    print("ALL PASSED")


def vec(*xs):
    return "[" + ",".join(str(x) for x in xs) + "]"


def db_block(psycopg):
    from fastapi.testclient import TestClient

    import auth, llm, main

    async def fake_embed(texts):  # every question embeds to the same point; chunk i sits at distance growing with i
        return [[1.0, 0.0, 0.0, 0.0] for _ in texts]

    script = {}

    async def fake_chat(messages):
        script["sent"] = messages
        yield "Respuesta [p. 9]."

    async def fake_rewrite(history, q):
        return q

    llm.embed, llm.chat, llm.rewrite = fake_embed, fake_chat, fake_rewrite

    def events(r):
        out = {}
        for block in r.text.strip().split("\n\n"):
            ev, payload = block.split("\n")
            out.setdefault(ev[7:], []).append(json.loads(payload[6:]))
        return out

    dburl = os.environ["DATABASE_URL"]
    with psycopg.connect(dburl, autocommit=True) as db, TestClient(main.app, base_url="http://testserver") as c:
        # ---- fixture: 40 long chunks about other things (pages 10..49), one short TOC chunk (page 9) the embedding ranks LAST
        doc = db.execute("INSERT INTO documents (name, pages) VALUES ('guitarra.pdf', 49) RETURNING id").fetchone()[0]
        for i in range(40):
            text = f"Capítulo {i}. " + "La técnica de la mano izquierda exige paciencia y repetición diaria con metrónomo. " * 8
            if i >= 10:
                text += " Practica cada acorde despacio."      # 'despacio' in 30 chunks (pages 20..49), none in the vector top 8
            if 20 <= i < 30:
                text += " Este libro recomienda practicar."    # 'libro' in 10 chunks: a generic word
            db.execute("INSERT INTO chunks (document_id, page, content, embedding) VALUES (%s, %s, %s, %s::vector)",
                       (doc, 10 + i, text, vec(1.0, 0.02 * i, 0, 0)))
        db.execute("INSERT INTO chunks (document_id, page, content, embedding) VALUES (%s, 9, %s, %s::vector)", (doc, TOC, vec(0, 0, 0, 1)))
        db.execute("INSERT INTO users (username, password_hash) VALUES ('t', %s)", (auth.hasher.hash("password-12345"),))
        assert c.post("/api/login", json={"username": "t", "password": "password-12345"}).status_code == 200

        def vector_only():  # today's retrieval, verbatim
            return [tuple(r) for r in db.execute(
                "SELECT page, content FROM chunks WHERE document_id = %s ORDER BY embedding <=> %s::vector LIMIT %s",
                (doc, vec(1, 0, 0, 0), main.TOP_K)).fetchall()]

        def ask(question):
            r = c.post("/api/chat", json={"document_id": str(doc), "question": question})
            assert r.status_code == 200, r.text
            ev = events(r)
            assert "error" not in ev, ev
            return [(s["page"], s["text"]) for s in ev["sources"][0]], script["sent"][-1]["content"]

        vec_rows = vector_only()
        assert len(vec_rows) == 8 and all(p != 9 for p, _ in vec_rows)  # the premise: vector search never returns the TOC

        # 1. THE FAILURE: the TOC chunk is reached through the keyword channel
        rows, prompt = ask(Q)
        assert (9, TOC) in rows, [p for p, _ in rows]
        assert "[p. 9]\n9 | Intro | Glue Mágico" in prompt, "the TOC chunk must be in the context the model gets"
        assert len(rows) == 9 and rows[0] == vec_rows[0] and set(vec_rows) <= set(rows)  # 8 vector + the TOC; nothing displaced
        print("ok the TOC chunk (ranked last by the embedding) reaches sources and the model's context; 'libro' (10 chunks) was cut")

        # 2. accents, case
        for q in ("¿Cuál es el ÍNDICE?", "indice", "Índice", "INDICE del libro"):
            rows, _ = ask(q)
            assert (9, TOC) in rows, q
        print("ok ÍNDICE / indice / Índice all find the chunk")

        # 3. no keyword match, or nothing searchable: exactly today's result, same order
        for q in ("¿Qué dice sobre cuerdas de nailon?", "¿Y el de la?", "?!"):
            rows, _ = ask(q)
            assert rows == vec_rows, q
        print("ok no keyword match / only stopwords -> identical to vector-only (same 8, same order)")

        # 4. broad term in 30 chunks: at most 2 more excerpts, the top vector results stay, ties broken by page ascending
        rows, _ = ask("despacio")
        assert len(rows) == 10 <= main.TOP_K + 2, len(rows)
        assert set(vec_rows[:7]) <= set(rows), "top vector results kept"
        assert vec_rows[7] not in rows, "only the weakest vector chunk is displaced"
        extra = sorted(p for p, t in rows if (p, t) not in vec_rows)
        assert extra == [20, 21, 22], extra  # three keyword chunks tie: the three lowest pages; 8 - 1 + 3 = 10 excerpts
        async def search(q):
            async with main.db() as conn:
                return await kw.search(conn, str(doc), q)
        import asyncio
        assert [p for p, _ in asyncio.run(search("despacio"))] == [20, 21, 22]
        assert [p for p, _ in asyncio.run(search("despacio"))] == [20, 21, 22]  # deterministic
        print("ok broad term (30 chunks): 10 excerpts max (+2), vector top kept, tie-break by page ascending: pages 20, 21, 22")

        # 5. documented limit: a plural question does not match the singular word ("indices" vs "Índice")
        rows, _ = ask("indices")
        assert rows == vec_rows
        print("ok limitation documented: 'indices' does not match 'Índice' (no stemming)")

        # ---- Python folding and SQL unaccent agree (ñ ç ü œ, case)
        d2 = db.execute("INSERT INTO documents (name, pages) VALUES ('fold.pdf', 1) RETURNING id").fetchone()[0]
        samples = ["El ESPAÑOL tiene AÑOS de historia", "Le CŒUR et l'œuvre", "Über Größe", "Ça va, François, garçon", "Œdipe, Æsop"]
        for i, s in enumerate(samples):
            db.execute("INSERT INTO chunks (document_id, page, content, embedding) VALUES (%s, %s, %s, %s::vector)", (d2, i + 1, s, vec(1, 0, 0, 0)))
        for q, page in [("español", 1), ("años", 1), ("cœur", 2), ("coeur", 2), ("COEUR", 2), ("œuvre", 2), ("über", 3), ("uber", 3),
                        ("größe", 3), ("grosse", 3), ("ça", 4), ("garçon", 4), ("garcon", 4), ("françois", 4), ("œdipe", 5), ("aesop", 5)]:
            async def one(q=q):
                async with main.db() as conn:
                    return await kw.search(conn, str(d2), q)
            got = [p for p, _ in asyncio.run(one())]
            assert page in got, (q, kw.terms(q), got)
        print("ok Python-folded terms match SQL-unaccented text: español, años, cœur, über, größe, ça, garçon, François, Œdipe")

        # ---- the query selects page/content only and never concatenates terms
        assert "embedding" not in kw.SQL and "SELECT m.page, m.content" in kw.SQL
        hostile = "x'; DROP TABLE chunks; -- \" \\y % %s"
        assert all(t.isascii() and t.isalnum() for t in kw.terms(hostile))
        async def hostile_search():
            async with main.db() as conn:
                return await kw.search(conn, str(doc), hostile)
        asyncio.run(hostile_search())
        assert db.execute("SELECT count(*) FROM chunks WHERE document_id = %s", (doc,)).fetchone()[0] == 41
        print("ok no embedding column in the query; hostile input runs as parameters only")

        # ---- unaccent missing: startup survives, the other statements still run, answers are vector-only
        db.execute("UPDATE documents SET status = 'indexing' WHERE id = %s", (d2,))
        db.execute("ALTER TABLE chat_messages DROP COLUMN ungrounded")
        db.execute("DROP EXTENSION unaccent")

    orig = psycopg.AsyncConnection.execute

    async def deny(self, query, *a, **k):
        if isinstance(query, str) and "unaccent" in query and query.lstrip().upper().startswith("CREATE EXTENSION"):
            raise psycopg.errors.FeatureNotSupported("extension \"unaccent\" is not available")
        return await orig(self, query, *a, **k)

    psycopg.AsyncConnection.execute = deny
    try:
        with TestClient(main.app, base_url="http://testserver") as c2, psycopg.connect(dburl, autocommit=True) as db:  # startup runs here
            assert c2.post("/api/login", json={"username": "t", "password": "password-12345"}).status_code == 200
            cols = {r[0] for r in db.execute("SELECT column_name FROM information_schema.columns WHERE table_name = 'chat_messages'")}
            assert "ungrounded" in cols, "the ALTER TABLE after the failed statement still ran"
            assert db.execute("SELECT status FROM documents WHERE id = %s", (d2,)).fetchone()[0] == "failed", "the UPDATE on documents still ran"
            assert not db.execute("SELECT 1 FROM pg_extension WHERE extname = 'unaccent'").fetchone()
            r = c2.post("/api/chat", json={"document_id": str(doc), "question": Q})
            assert r.status_code == 200
            ev = events(r)
            assert "error" not in ev and [(s["page"], s["text"]) for s in ev["sources"][0]] == vec_rows
            print("ok unaccent unavailable: app starts, ALTER TABLE and UPDATE still run, chat answers vector-only (same 8 as before)")
    finally:
        psycopg.AsyncConnection.execute = orig


if __name__ == "__main__":
    main()
