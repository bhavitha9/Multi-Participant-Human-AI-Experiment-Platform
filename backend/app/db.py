"""MongoDB persistence (sessions, messages, votes, results).

Writes never block a live session: they go onto an in-memory queue that a
single background worker applies in order. If MongoDB is unreachable the worker
keeps retrying, so data written during a short outage is saved once the
database is back; only after a long outage (see WRITE_RETRY_SECONDS) is a write
given up and logged.
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from datetime import datetime, timezone
from typing import Any, Optional

from pymongo import ASCENDING, AsyncMongoClient

log = logging.getLogger(__name__)

WRITE_RETRY_SECONDS = float(os.environ.get("DB_WRITE_RETRY_SECONDS", "300"))


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, uri: Optional[str] = None, name: Optional[str] = None):
        self.uri = uri or os.environ.get("MONGO_URI", "mongodb://mongo:27017")
        self.name = name or os.environ.get("MONGO_DB", "human_ai_experiment")
        self.client: Optional[AsyncMongoClient] = None
        self.db = None
        self._queue: asyncio.Queue = asyncio.Queue()
        self._worker: Optional[asyncio.Task] = None

    async def connect(self) -> None:
        self.client = AsyncMongoClient(
            self.uri, tz_aware=True,
            serverSelectionTimeoutMS=3000, connectTimeoutMS=3000, socketTimeoutMS=5000,
        )
        self.db = self.client[self.name]
        await self.db.messages.create_index([("session_id", ASCENDING), ("ts", ASCENDING)])
        await self.db.votes.create_index(
            [("session_id", ASCENDING), ("round", ASCENDING), ("voter", ASCENDING)],
            unique=True,
        )
        await self.db.results.create_index([("session_id", ASCENDING), ("round", ASCENDING)])
        log.info("connected to MongoDB %s/%s", self.uri, self.name)
        self._worker = asyncio.create_task(self._drain())

    async def close(self) -> None:
        if self._worker is not None:
            try:  # give pending writes a moment to land
                await asyncio.wait_for(self._queue.join(), timeout=5)
            except asyncio.TimeoutError:
                log.warning("closing with %d unsaved writes", self._queue.qsize())
            self._worker.cancel()
        if self.client is not None:
            await self.client.close()

    async def _drain(self) -> None:
        while True:
            coro_factory = await self._queue.get()
            started = time.monotonic()
            delay = 1.0
            while True:
                try:
                    await coro_factory()
                    break
                except Exception:  # noqa: BLE001 - keep retrying, never crash the worker
                    if time.monotonic() - started > WRITE_RETRY_SECONDS:
                        log.exception("giving up on a database write after %.0fs", WRITE_RETRY_SECONDS)
                        break
                    log.warning("database write failed, retrying (%d queued)", self._queue.qsize())
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 10.0)
            self._queue.task_done()

    async def _safe(self, coro_factory) -> Any:
        """Queue a write and return immediately."""
        if self.db is None:
            return None
        self._queue.put_nowait(coro_factory)
        return None

    # --- writes ---------------------------------------------------------
    async def create_session(self, doc: dict) -> None:
        await self._safe(lambda: self.db.sessions.insert_one(doc))

    async def update_session(self, session_id: str, fields: dict) -> None:
        await self._safe(
            lambda: self.db.sessions.update_one({"_id": session_id}, {"$set": fields})
        )

    async def insert_message(self, doc: dict) -> None:
        await self._safe(lambda: self.db.messages.insert_one(dict(doc)))

    async def upsert_vote(self, doc: dict) -> None:
        key = {k: doc[k] for k in ("session_id", "round", "voter")}
        await self._safe(
            lambda: self.db.votes.update_one(key, {"$set": doc}, upsert=True)
        )

    async def insert_result(self, doc: dict) -> None:
        await self._safe(lambda: self.db.results.insert_one(dict(doc)))

    # --- reads (admin/export only) --------------------------------------
    async def list_sessions(self, limit: int = 100) -> list[dict]:
        cur = self.db.sessions.find({}, sort=[("created_at", -1)], limit=limit)
        return [d async for d in cur]

    async def session_detail(self, session_id: str) -> Optional[dict]:
        session = await self.db.sessions.find_one({"_id": session_id})
        if session is None:
            return None
        q = {"session_id": session_id}
        session["messages"] = [
            d async for d in self.db.messages.find(q, {"_id": 0}, sort=[("ts", 1)])
        ]
        session["votes"] = [
            d async for d in self.db.votes.find(q, {"_id": 0}, sort=[("round", 1)])
        ]
        session["results"] = [
            d async for d in self.db.results.find(q, {"_id": 0}, sort=[("round", 1)])
        ]
        return session
