"""FastAPI entry point: WebSocket game channel + small REST surface."""
from __future__ import annotations

import json
import logging
import os
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, WebSocket, WebSocketDisconnect

from .config import load_config
from .db import Database
from .session import SessionManager

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("app")

db = Database()
manager = SessionManager(db)


@asynccontextmanager
async def lifespan(_: FastAPI):
    cfg = load_config()  # fail fast on an invalid config.yml
    log.info("config ok: condition=%s participants=%s provider=%s",
             cfg.active_condition(), cfg.participants(), cfg.ai.provider)
    await db.connect()
    yield
    await db.close()


app = FastAPI(title="human_ai_experiment", lifespan=lifespan)


@app.get("/api/health")
async def health():
    return {"ok": True}


# ---------------------------------------------------------------- admin/export
# Researcher-only endpoints. They expose participant types, so they are
# disabled unless ADMIN_TOKEN is set, and require the X-Admin-Token header.
def _require_admin(token: str | None) -> None:
    expected = os.environ.get("ADMIN_TOKEN")
    if not expected or not token or not secrets.compare_digest(token, expected):
        raise HTTPException(status_code=404)


@app.get("/api/admin/sessions")
async def admin_sessions(x_admin_token: str | None = Header(default=None)):
    _require_admin(x_admin_token)
    return await db.list_sessions()


@app.get("/api/admin/sessions/{session_id}")
async def admin_session(session_id: str, x_admin_token: str | None = Header(default=None)):
    _require_admin(x_admin_token)
    doc = await db.session_detail(session_id)
    if doc is None:
        raise HTTPException(status_code=404)
    return doc


# ------------------------------------------------------------------ websocket
def _parse(text: str):
    """Decode a client frame; malformed JSON is ignored instead of dropping the connection."""
    try:
        return json.loads(text)
    except ValueError:
        return None


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    session = letter = None
    try:
        hello = _parse(await ws.receive_text())
        if not isinstance(hello, dict) or hello.get("type") != "join":
            await ws.close(code=4001)
            return
        token = hello.get("token") if isinstance(hello.get("token"), str) else None
        try:
            session, letter, token = await manager.join(ws, token)
        except Exception:  # noqa: BLE001 - e.g. invalid config.yml
            log.exception("could not place participant")
            await ws.send_json({"type": "error", "message": "The study is not available right now."})
            await ws.close(code=1011)
            return
        await ws.send_json({"type": "welcome", "token": token, "you": letter})
        await session.send_history(letter)
        await session.after_join()

        while True:
            msg = _parse(await ws.receive_text())
            if not isinstance(msg, dict):
                continue
            kind = msg.get("type")
            if kind == "chat" and isinstance(msg.get("text"), str):
                await session.human_message(letter, msg["text"])
            elif kind == "vote" and isinstance(msg.get("target"), str):
                await session.cast_vote(letter, msg["target"])
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        log.exception("websocket error")
    finally:
        if session is not None and letter is not None:
            await session.detach(letter, ws)
