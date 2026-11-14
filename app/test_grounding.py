"""Name check (grounding.py): unit tests, then a DB-backed block for the SSE event, the stored field and the reload.

Run:
    unit only (stdlib, Python 3.10+):  python app/test_grounding.py --unit
    everything (db must be up; creates and drops the scratch database `chatpdf_test`):
        docker compose run --rm --no-deps -T -v "$PWD/app:/srv:ro" -e PYTHONDONTWRITEBYTECODE=1 app python -W ignore test_grounding.py
"""
import json
import os
import sys
from urllib.parse import urlsplit, urlunsplit

import grounding as g

BIB = "Bibliographie. ILLOUZ E. (dir.) (2019), Les marchandises émotionnelles. L'authenticité au temps du capitalisme."


def unit():
    # ---- the real case: surname + initial in the excerpts, a full first name in the answer
    assert g.ungrounded("Las ideas de Eva Illouz [p. 3] muestran que las emociones se venden.", [BIB]) == ["Eva Illouz"]
    assert g.ungrounded("Según Eva Illouz, las emociones se mercantilizan [p. 3].", [BIB]) == ["Eva Illouz"]  # "Según" is an opener, not a name
    # written as in the excerpts: not flagged, and the surname really was checked (the test is not vacuous)
    ok = "La autora Illouz (E.) sostiene que las emociones se venden [p. 3]."
    assert g.names(ok) == ["Illouz"] and g.ungrounded(ok, [BIB]) == []
    # a full name that is in the excerpts
    assert g.ungrounded("Las ideas de Eva Illouz son claras.", ["Eva Illouz, Les marchandises émotionnelles"]) == []
    print("ok real case: 'Eva Illouz' flagged against 'ILLOUZ E.', 'Illouz (E.)' and a full name present are not")

    # ---- accents and case
    assert g.ungrounded("Las tesis de Álvaro García.", ["ÁLVARO GARCÍA (2001)"]) == []
    assert g.ungrounded("Las tesis de Álvaro García.", ["alvaro garcia"]) == []
    assert g.ungrounded("Las tesis de Álvaro García.", ["Alvaro Gomez"]) == ["Álvaro García"]
    print("ok accents and case ignored; a different surname is flagged")

    # ---- a name that only the user wrote: not flagged (the user introduced it)
    ans = "Eso lo plantea Eva Illouz, pero el texto no lo dice."
    assert g.ungrounded(ans, [BIB], question="¿Qué dice Eva Illouz?") == []
    assert g.ungrounded(ans, [BIB]) == ["Eva Illouz"]
    print("ok name only in the question is not flagged (it is flagged without the question)")

    # ---- no proper names, figures and dates are not checked
    assert g.ungrounded("Las emociones se venden en el mercado [p. 3].", [BIB]) == []
    assert g.ungrounded("En 2019 se vendieron 4.000 unidades el 12 de mayo [p. 3].", [BIB]) == []
    assert g.names("[p. 3] [p. 4]") == []
    print("ok no proper names -> nothing; dates and figures are out of scope")

    # ---- empty excerpts: nothing can ground a name
    assert g.ungrounded("Las ideas de Eva Illouz.", []) == ["Eva Illouz"]
    assert g.ungrounded("Las emociones se venden.", []) == []
    print("ok empty excerpts: names flagged, an answer without names is not")

    # ---- accepted limitation: translated proper nouns are over-flagged
    assert g.ungrounded("El estudio se hizo en Francia [p. 2].", ["L'étude a été menée en France."]) == ["Francia"]
    print("ok limitation documented: 'Francia' vs 'France' is flagged by mistake")

    # ---- accepted limitation: a lone capitalized word at the start of a sentence is skipped
    assert g.ungrounded("Illouz sostiene que las emociones se venden.", ["texto sin autores"]) == []
    assert g.ungrounded("Eva Illouz sostiene que las emociones se venden.", ["texto sin autores"]) == ["Eva Illouz"]
    assert g.ungrounded("Las emociones, dice Illouz, se venden.", ["texto sin autores"]) == ["Illouz"]  # same word mid-sentence is checked
    print("ok limitation documented: lone sentence-initial word skipped, two words or mid-sentence checked")

    # ---- other shapes
    assert g.ungrounded("Lo dice el Ministerio de Educación.", ["el ministerio de educacion publico"]) == []
    assert g.ungrounded("Lo dice el Ministerio de Cultura.", ["el ministerio de educacion publico"]) == ["Ministerio de Cultura"]
    assert g.ungrounded("Informe de la UNESCO y de la ONU.", ["informe de la UNESCO"]) == ["ONU"]
    assert g.names("- Eva Smith\n- Pedro Gómez") == ["Eva Smith", "Pedro Gómez"]  # a new line ends the run
    assert g.names("Lo dijo «Pedro Gómez», Eva Smith y otros.") == ["Pedro Gómez", "Eva Smith"]  # commas, quotes end it too
    assert g.ungrounded("Lo dice Smith, luego Smith y Smith.", ["otro texto"]) == ["Smith"]  # no duplicates
    assert g.ungrounded("Lo dijo O'Neil y d'Alembert.", ["O'Neil, d'Alembert"]) == []
    assert g.ungrounded("Lo dijo en January sobre el English.", ["x"]) == []  # months, days and languages are not names
    print("ok connectors, acronyms, lists, quotes, duplicates, apostrophes")


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


def db_block(psycopg):
    import fitz
    from fastapi.testclient import TestClient

    import auth, llm, main

    async def fake_embed(texts):
        return [[0.1, 0.2, 0.3, 0.4] for _ in texts]

    script = {"answer": ""}

    async def fake_chat(messages):
        script["sent"] = messages
        for tok in script["answer"].replace(" ", " \x00").split("\x00"):
            yield tok

    async def fake_rewrite(history, q):
        return q

    llm.embed, llm.chat, llm.rewrite = fake_embed, fake_chat, fake_rewrite

    pdf = fitz.open()
    pdf.new_page().insert_textbox(fitz.Rect(50, 50, 560, 300), BIB + " Les emotions se vendent sur le marche.", fontsize=11)
    data = pdf.tobytes()

    def events(r):
        out = {}
        for block in r.text.strip().split("\n\n"):
            ev, payload = block.split("\n")
            out.setdefault(ev[7:], []).append(json.loads(payload[6:]))
        return out

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as db, TestClient(main.app, base_url="http://testserver") as c:
        db.execute("INSERT INTO users (username, password_hash) VALUES ('t', %s)", (auth.hasher.hash("password-12345"),))
        assert c.post("/api/login", json={"username": "t", "password": "password-12345"}).status_code == 200
        doc = c.post("/api/documents", params={"name": "illouz.pdf"}, content=data).json()["id"]
        assert db.execute("SELECT status FROM documents WHERE id = %s", (doc,)).fetchone()[0] == "ready"

        def ask(answer, question="Qué dice la autora?"):
            script["answer"] = answer
            r = c.post("/api/chat", json={"document_id": doc, "question": question})
            return r, events(r)

        # 1. the SYSTEM prompt carries the rule, and it is what the model is sent
        r, ev = ask("Las ideas de Eva Illouz muestran que las emociones se venden [p. 1].")
        assert "ONLY if they appear in the excerpts" in script["sent"][0]["content"] and "Never complete or expand a name" in script["sent"][0]["content"]
        print("ok SYSTEM prompt has the grounding rule")

        # 2. event after the answer, once, after citations and before done; stored next to the citations
        assert ev["grounding"] == [["Eva Illouz"]] and "error" not in ev, ev
        assert r.text.index("event: citations") < r.text.index("event: grounding") < r.text.index("event: done")
        msgs = c.get(f"/api/documents/{doc}/messages").json()
        assert msgs[1]["ungrounded"] == ["Eva Illouz"] and msgs[1]["citations"][0]["page"] == 1 and msgs[0]["ungrounded"] == []
        print("ok grounding event emitted once after citations; stored and returned by GET /messages (reload shows the note again)")

        # 3. clean answer: empty list, nothing stored; name written as in the excerpts is not flagged
        r, ev = ask("La autora Illouz (E.) dirige la obra [p. 1].")
        assert ev["grounding"] == [[]]
        assert c.get(f"/api/documents/{doc}/messages").json()[3]["ungrounded"] == []
        print("ok no note when every name is in the excerpts")

        # 4. a name that only the question contains is not flagged
        r, ev = ask("Eso lo plantea Eva Illouz, pero el texto no lo dice.", question="¿Qué dice Eva Illouz?")
        assert ev["grounding"] == [[]]
        print("ok name only in the question is not flagged")

        # 5. messages stored before this feature (NULL) read as an empty list
        db.execute("UPDATE chat_messages SET ungrounded = NULL")
        assert all(m["ungrounded"] == [] for m in c.get(f"/api/documents/{doc}/messages").json())
        print("ok old messages (NULL) -> []")

        # 6. a failed turn stores nothing and emits no grounding event
        n = len(c.get(f"/api/documents/{doc}/messages").json())

        async def boom(messages):
            raise llm.NaNError("nope")
            yield
        llm.chat = boom
        r, ev = ask("x")
        assert "error" in ev and "grounding" not in ev and len(c.get(f"/api/documents/{doc}/messages").json()) == n
        print("ok failed turn: error shown, no grounding event, nothing stored")


if __name__ == "__main__":
    main()
