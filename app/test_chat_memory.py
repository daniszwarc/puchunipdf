"""Conversation memory against the real Postgres of the compose stack, with the NaN API stubbed at the HTTP level.

Run (db must be up; uses a scratch database `chatpdf_test`, created and dropped here):
    docker compose run --rm --no-deps app python test_chat_memory.py
"""
import json
import math
import os
import re
import sys
import types
from urllib.parse import urlsplit, urlunsplit

TOPICS = ["falcon", "dragon", "stage"]


def vec(text: str) -> list[float]:
    """Keyword embedding: one axis per topic word plus a constant 'other' axis."""
    t = text.lower()
    return [float(w in t) for w in TOPICS] + [0.1]


def cos_dist(a, b):
    d = sum(x * y for x, y in zip(a, b))
    return 1 - d / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


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


def run(psycopg):
    import fitz
    import httpx
    from fastapi.testclient import TestClient

    import auth, llm, main

    # ---- NaN stub (real llm.py code runs on top of it) ----
    calls = {"embed": [], "rewrite": [], "chat": []}
    mode = {"rewrite": "ok", "chat": "ok"}
    REWRITTEN = "What is the second stage of the Falcon rocket?"

    class Cut(httpx.AsyncByteStream):  # connection drops after the first token
        async def __aiter__(self):
            yield b'data: {"choices":[{"delta":{"content":"Half an ans"},"finish_reason":null}]}\n\n'
            raise httpx.ReadError("connection cut")

    def sse(*parts, finish="stop"):
        chunks = [{"choices": [{"delta": {"content": p}, "finish_reason": None}]} for p in parts]
        chunks.append({"choices": [{"delta": {}, "finish_reason": finish}]})
        return httpx.Response(200, content="".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n")

    def handler(req):
        body = json.loads(req.content)
        if req.url.path.endswith("/embeddings"):
            calls["embed"].append(body["input"])
            return httpx.Response(200, json={"data": [{"index": i, "embedding": vec(t)} for i, t in enumerate(body["input"])]})
        if not body["stream"]:  # the question rewrite
            calls["rewrite"].append(body)
            if mode["rewrite"] == "fail":
                return httpx.Response(500)
            if mode["rewrite"] == "empty":
                return httpx.Response(200, json={"choices": [{"message": {"content": "  "}}]})
            return httpx.Response(200, json={"choices": [{"message": {"content": REWRITTEN}}]})
        calls["chat"].append(body)
        if mode["chat"] == "cut":
            return httpx.Response(200, stream=Cut())
        if mode["chat"] == "length":
            return sse("Partial answer [p. 1] that got trunc", finish="length")
        return sse("The second stage uses a vacuum engine ", "[p. 1].")

    real_client = httpx.AsyncClient
    llm.httpx.AsyncClient = lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw)

    async def no_sleep(_):
        pass
    llm.asyncio = types.SimpleNamespace(sleep=no_sleep)  # llm only uses asyncio.sleep (retry backoff on the stubbed 500)

    # ---- a two-page document: page 1 about Falcon stages, page 2 about Dragon ----
    pdf = fitz.open()
    pdf.new_page().insert_textbox(fitz.Rect(50, 50, 550, 300), "The Falcon rocket has two stages. The first stage burns kerosene, "
                                  "the second stage uses a vacuum engine.", fontsize=12)
    pdf.new_page().insert_textbox(fitz.Rect(50, 50, 550, 300), "The Dragon capsule carries crew and cargo to orbit.", fontsize=12)
    data = pdf.tobytes()

    def events(r):
        out = {}
        for block in r.text.strip().split("\n\n"):
            ev, payload = block.split("\n")
            out.setdefault(ev[7:], []).append(json.loads(payload[6:]))
        return out

    with psycopg.connect(os.environ["DATABASE_URL"], autocommit=True) as db, \
            TestClient(main.app, base_url="http://testserver") as a, TestClient(main.app, base_url="http://testserver") as b:
        for name, client in (("alice", a), ("bob", b)):
            db.execute("INSERT INTO users (username, password_hash) VALUES (%s, %s)", (name, auth.hasher.hash("password-12345")))
            assert client.post("/api/login", json={"username": name, "password": "password-12345"}).status_code == 200
        doc = a.post("/api/documents", params={"name": "falcon.pdf"}, content=data).json()["id"]
        assert db.execute("SELECT status FROM documents WHERE id = %s", (doc,)).fetchone()[0] == "ready"

        def ask(client, q):
            return client.post("/api/chat", json={"document_id": doc, "question": q})

        def stored(client):
            return client.get(f"/api/documents/{doc}/messages").json()

        # 1. first question: no history, no rewrite call, prompt = system + question (unchanged behaviour)
        r = ask(a, "How many stages does the Falcon have?")
        ev = events(r)
        assert "".join(ev["token"]) == "The second stage uses a vacuum engine [p. 1]." and "error" not in ev
        assert calls["rewrite"] == [], "no rewrite without history"
        assert [m["role"] for m in calls["chat"][0]["messages"]] == ["system", "user"]
        assert calls["embed"][-1] == ["How many stages does the Falcon have?"]
        msgs = stored(a)
        assert [m["role"] for m in msgs] == ["user", "assistant"], msgs
        assert msgs[1]["content"] == "The second stage uses a vacuum engine [p. 1]." and msgs[1]["citations"][0]["page"] == 1
        assert "Falcon" in msgs[1]["citations"][0]["text"]
        print("ok turn 1: no rewrite, no history in the prompt; question + answer + citations stored")

        # 2. pronoun follow-up: rewritten for retrieval only, original wording answered, history in the prompt
        followup = "and the second one?"
        pages = [vec(t) for t in ("Falcon stages", "Dragon capsule")]  # stand-ins to rank pages by the stub embedding
        assert cos_dist(vec(followup), pages[1]) < cos_dist(vec(followup), pages[0]), "test is vacuous: raw follow-up already finds page 1"
        ev = events(ask(a, followup))
        rw = calls["rewrite"][-1]
        assert (rw["stream"], rw["reasoning_effort"], rw["temperature"], rw["max_tokens"]) == (False, "none", 0, 256), rw
        assert calls["embed"][-1] == [REWRITTEN], "retrieval must embed the rewritten question"
        assert ev["sources"][0][0]["page"] == 1, f"follow-up should retrieve the first turn's topic first: {ev['sources'][0]}"
        sent = calls["chat"][-1]["messages"]
        assert [m["role"] for m in sent] == ["system", "user", "assistant", "user"], [m["role"] for m in sent]
        assert sent[1]["content"] == "How many stages does the Falcon have?" and "<context>" not in sent[1]["content"]
        assert sent[-1]["content"].endswith(f"Question: {followup}") and "<context>" in sent[-1]["content"]
        assert len(stored(a)) == 4
        print("ok turn 2: raw follow-up would rank Dragon first; rewritten retrieval puts the Falcon page first; "
              "history sent as prior messages; answer prompt keeps the original wording")

        # 3. rewrite failure / empty result: silent fallback to the original question
        for m in ("fail", "empty"):
            mode["rewrite"] = m
            ev = events(ask(a, "any other detail?"))
            assert calls["embed"][-1] == ["any other detail?"] and "error" not in ev, m
        mode["rewrite"] = "ok"
        print("ok rewrite failure (500) and empty rewrite fall back to the original question, no error shown")

        # 4. history limit
        main.HISTORY_TURNS = 1
        ask(a, "one more?")
        assert [m["role"] for m in calls["chat"][-1]["messages"]] == ["system", "user", "assistant", "user"]
        main.HISTORY_TURNS = 8
        print("ok CHAT_HISTORY_TURNS limits the prior turns sent")

        # 5. failed / cut turns store nothing
        n = len(stored(a))
        for m in ("cut", "length"):
            mode["chat"] = m
            ev = events(ask(a, "will this fail?"))
            assert "error" in ev, (m, ev)
            assert len(stored(a)) == n, f"{m}: a failed turn was stored"
        mode["chat"] = "ok"
        print("ok connection cut and token-limit truncation: error shown, nothing stored")

        # 6. privacy: bob sees none of alice's conversation; clearing is per user
        assert stored(b) == []
        ask(b, "Tell me about the Falcon")
        assert len(stored(b)) == 2
        assert a.delete(f"/api/documents/{doc}/messages").status_code == 204
        assert stored(a) == [] and len(stored(b)) == 2
        assert calls["chat"][-1]["messages"][1:-1] == [], "bob's first question must not carry alice's history"
        print("ok conversations are private per user; New chat clears only your own")

        # 7. guards: auth + Origin on the new endpoints
        anon = TestClient(main.app, base_url="http://testserver")
        assert anon.get(f"/api/documents/{doc}/messages").status_code == 401
        assert b.delete(f"/api/documents/{doc}/messages", headers={"Origin": "https://evil.example"}).status_code == 403
        assert len(stored(b)) == 2
        print("ok unauthenticated GET -> 401, foreign Origin DELETE -> 403 (nothing deleted)")

        # 8. deleting the document deletes its messages (all users)
        assert db.execute("SELECT count(*) FROM chat_messages WHERE document_id = %s", (doc,)).fetchone()[0] == 2
        assert b.delete(f"/api/documents/{doc}").status_code == 204
        assert db.execute("SELECT count(*) FROM chat_messages").fetchone()[0] == 0
        print("ok deleting a document deletes its messages")
    print("ALL PASSED")


if __name__ == "__main__":
    sys.exit(main())
