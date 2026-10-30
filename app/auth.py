import asyncio
import os
import time
from pathlib import Path
from urllib.parse import urlparse

import psycopg
from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from fastapi import APIRouter, Request
from fastapi.responses import FileResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel, Field

USERS_DDL = """CREATE TABLE IF NOT EXISTS users (
    id bigserial PRIMARY KEY, username text UNIQUE NOT NULL,
    password_hash text NOT NULL, created_at timestamptz NOT NULL DEFAULT now())"""
PUBLIC = {"/login", "/api/login", "/health", "/style.css", "/login.js"}
MAX_FAILS, WINDOW = 5, 15 * 60  # failures per (ip, username), and both the counting window and the lockout

hasher = PasswordHasher()
DUMMY = hasher.hash("timing-parity-dummy")  # verified against when the user doesn't exist
router = APIRouter()
_fails: dict[tuple, list[float]] = {}
_locked: dict[tuple, float] = {}


def _verify(hash_: str, password: str) -> bool:
    try:
        return hasher.verify(hash_, password)
    except (VerificationError, InvalidHashError):
        return False


def _record_fail(key: tuple):
    now = time.time()
    if len(_fails) > 10_000:  # bound memory: drop stale entries
        for k in [k for k, v in _fails.items() if now - v[-1] > WINDOW]:
            del _fails[k]
        for k in [k for k, t in _locked.items() if t < now]:
            del _locked[k]
    recent = [t for t in _fails.get(key, []) if now - t < WINDOW] + [now]
    _fails[key] = recent
    if len(recent) >= MAX_FAILS:
        _locked[key] = now + WINDOW
        del _fails[key]


class LoginIn(BaseModel):
    username: str = Field(max_length=150)
    password: str = Field(max_length=1024)


@router.get("/login")
async def login_page(request: Request):
    if request.session.get("user"):
        return RedirectResponse("/", 303)
    return FileResponse(Path(__file__).parent / "static" / "login.html")


@router.get("/health")
async def health():
    return {"ok": True}


@router.post("/api/login")
async def login(body: LoginIn, request: Request):
    key = (request.client.host if request.client else "?", body.username)
    if _locked.get(key, 0) > time.time():
        return JSONResponse({"detail": "too many attempts, try again later"}, 429)
    async with await psycopg.AsyncConnection.connect(os.environ["DATABASE_URL"]) as conn:
        cur = await conn.execute("SELECT password_hash FROM users WHERE username = %s", (body.username,))
        row = await cur.fetchone()
    valid = await asyncio.to_thread(_verify, row[0] if row else DUMMY, body.password)
    if not (valid and row):
        _record_fail(key)
        return JSONResponse({"detail": "invalid credentials"}, 401)
    _fails.pop(key, None)
    request.session.clear()
    request.session["user"] = body.username
    return {"ok": True}


@router.post("/api/logout")
async def logout(request: Request):
    request.session.clear()
    return {"ok": True}


async def guard(request: Request, call_next):
    """Origin check on state-changing requests, then require a session for everything not public."""
    if request.method not in ("GET", "HEAD", "OPTIONS"):
        origin = request.headers.get("origin")
        # Compare host:port only: behind a proxy the scheme we see may not be the browser's.
        if origin and urlparse(origin).netloc != request.headers.get("host"):
            return JSONResponse({"detail": "bad origin"}, 403)
    path = request.url.path
    if path not in PUBLIC and not request.session.get("user"):
        if path.startswith("/api/"):
            return JSONResponse({"detail": "not authenticated"}, 401)
        return RedirectResponse("/login", 303)
    return await call_next(request)
