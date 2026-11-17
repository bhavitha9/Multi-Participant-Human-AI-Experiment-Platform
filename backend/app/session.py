"""Session engine: waiting room, chat/vote rounds, results.

Anonymity guarantees enforced here:
- Seats are exposed only as {id, occupied}; the participant type never leaves
  the server (it is stored in the database for analysis only).
- Human and AI messages/votes go through the same code path and produce
  identical payloads, with server-side timestamps and random IDs.
- Seat letters are randomly mapped to configured slots, so "AI is always C"
  cannot be learned across sessions.
- AI seats fill after a random delay instead of instantly.
"""
from __future__ import annotations

import asyncio
import logging
import random
import secrets
import time
import uuid
from typing import Optional

from fastapi import WebSocket

from .agents import AIAgent, build_llm
from .config import AppConfig, load_config
from .db import Database, utcnow
from .winner import PLAYERS, compute_winner, tally_votes

log = logging.getLogger(__name__)

WAITING, STARTING, CHAT, VOTE, RESULTS, FINISHED, ABORTED = (
    "waiting", "starting", "chat", "vote", "results", "finished", "aborted",
)
RUNNING = (STARTING, CHAT, VOTE, RESULTS)


def now_ms() -> int:
    return int(time.time() * 1000)


class Seat:
    def __init__(self, letter: str, kind: str):
        self.letter = letter
        self.kind = kind  # "human" | "ai" - server-side only
        self.occupied = False
        self.token: Optional[str] = None
        self.ws: Optional[WebSocket] = None
        self.agent: Optional[AIAgent] = None


class Session:
    def __init__(self, cfg: AppConfig, db: Database, manager: "SessionManager"):
        self.id = uuid.uuid4().hex
        self.cfg = cfg
        self.exp = cfg.experiment
        self.db = db
        self.manager = manager
        self.rng = random.Random()
        self.condition = cfg.active_condition()

        kinds = cfg.participants()
        letters = list(PLAYERS)
        if self.exp.randomize_seats:
            self.rng.shuffle(letters)
        self.seats: dict[str, Seat] = {
            letter: Seat(letter, kind) for letter, kind in zip(letters, kinds)
        }
        self.seats = dict(sorted(self.seats.items()))

        self.phase = WAITING
        self.round = 0
        self.deadline_ms: Optional[int] = None
        self.messages: list[dict] = []
        self.votes: dict[int, dict[str, Optional[str]]] = {}
        self.round_results: list[dict] = []
        self.final: Optional[dict] = None

        self._lock = asyncio.Lock()
        self._humans_ready = asyncio.Event()
        self._votes_complete = asyncio.Event()
        self._tasks: set[asyncio.Task] = set()
        self._last_human_msg: dict[str, float] = {}

        llm = build_llm(cfg.ai, self.rng)
        personas = list(cfg.ai.personas) or ["neutral"]
        self.rng.shuffle(personas)
        for i, seat in enumerate(s for s in self.seats.values() if s.kind == "ai"):
            seat.agent = AIAgent(self, seat.letter, llm, cfg.ai, personas[i % len(personas)], self.rng)

    # ------------------------------------------------------------------ utils
    def _spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return task

    def seconds_remaining(self) -> float:
        if self.deadline_ms is None:
            return 0.0
        return max(0.0, (self.deadline_ms - now_ms()) / 1000)

    def free_human_seats(self) -> list[str]:
        return [s.letter for s in self.seats.values() if s.kind == "human" and not s.occupied]

    def all_occupied(self) -> bool:
        return all(s.occupied for s in self.seats.values())

    # --------------------------------------------------------------- lifecycle
    async def init(self) -> None:
        await self.db.create_session({
            "_id": self.id,
            "created_at": utcnow(),
            "status": WAITING,
            "condition": self.condition,
            "seats": {l: {"type": s.kind} for l, s in self.seats.items()},
            "config": self.cfg.model_dump(mode="json"),
        })
        for seat in self.seats.values():
            if seat.kind == "ai":
                self._spawn(self._ai_join(seat))

    async def _ai_join(self, seat: Seat) -> None:
        delay = self.rng.uniform(*self.cfg.ai.timing.join_delay)
        try:
            await asyncio.wait_for(self._humans_ready.wait(), timeout=delay)
            # All humans arrived first: still join with a short, human-like lag.
            await asyncio.sleep(self.rng.uniform(0.8, 3.0))
        except asyncio.TimeoutError:
            pass
        async with self._lock:
            seat.occupied = True
        await self.db.update_session(self.id, {f"seats.{seat.letter}.joined_at": utcnow()})
        await self.broadcast_state()
        await self._maybe_start()

    async def add_human(self, ws: WebSocket) -> tuple[str, str]:
        async with self._lock:
            free = self.free_human_seats()
            letter = self.rng.choice(free)
            seat = self.seats[letter]
            seat.occupied, seat.ws, seat.token = True, ws, secrets.token_urlsafe(24)
            if not self.free_human_seats():
                self._humans_ready.set()
        await self.db.update_session(self.id, {f"seats.{letter}.joined_at": utcnow()})
        return letter, seat.token

    async def attach(self, letter: str, ws: WebSocket) -> None:
        seat = self.seats[letter]
        old, seat.ws = seat.ws, ws
        if old is not None and old is not ws:
            try:
                await old.close(code=4000)
            except Exception:  # noqa: BLE001
                pass

    async def detach(self, letter: str, ws: WebSocket) -> None:
        seat = self.seats[letter]
        if seat.ws is not ws:
            return  # a newer connection already replaced this one
        seat.ws = None
        if self.phase == WAITING:
            # Free the seat so someone else can take it.
            async with self._lock:
                seat.occupied = False
                self.manager.forget_token(seat.token)
                seat.token = None
                self._humans_ready.clear()
            await self.db.update_session(self.id, {f"seats.{letter}.joined_at": None})
            await self.broadcast_state()
        elif self.phase in RUNNING:
            await self.db.update_session(self.id, {f"seats.{letter}.left_at": utcnow()})
            if not self.humans_connected():
                self._spawn(self._abort_if_still_empty())

    def humans_connected(self) -> bool:
        return any(s.ws is not None for s in self.seats.values() if s.kind == "human")

    async def _abort_if_still_empty(self) -> None:
        """All humans left mid-session: give them a grace period to come back
        (page reload, network blip), otherwise abort the session."""
        await asyncio.sleep(self.exp.abort_after_all_humans_left_seconds)
        if self.phase in RUNNING and not self.humans_connected():
            await self.abort("all human participants left")

    async def abort(self, reason: str) -> None:
        self.phase = ABORTED
        self.deadline_ms = None
        current = asyncio.current_task()
        for task in list(self._tasks):
            if task is not current:
                task.cancel()  # stops the round loop and every AI agent
        await self.db.update_session(self.id, {
            "status": ABORTED, "abort_reason": reason, "round": self.round, "ended_at": utcnow(),
        })
        await self.broadcast_state()
        log.info("aborted session %s: %s", self.id, reason)

    async def after_join(self) -> None:
        await self.broadcast_state()
        await self._maybe_start()

    def has_humans(self) -> bool:
        return any(s.occupied for s in self.seats.values() if s.kind == "human")

    async def abandon(self) -> None:
        """Discard a waiting session nobody is in (e.g. everyone closed the page)."""
        self.phase = FINISHED
        for task in list(self._tasks):
            task.cancel()
        await self.db.update_session(self.id, {"status": "abandoned", "ended_at": utcnow()})
        log.info("abandoned empty waiting session %s", self.id)

    async def _maybe_start(self) -> None:
        async with self._lock:
            if self.phase != WAITING or not self.all_occupied():
                return
            self.phase = STARTING
        self.manager.session_started(self)
        self._spawn(self._run())

    async def _set_phase(self, phase: str, seconds: Optional[float]) -> None:
        self.phase = phase
        self.deadline_ms = now_ms() + int(seconds * 1000) if seconds else None
        await self.db.update_session(self.id, {"status": phase, "round": self.round})
        await self.broadcast_state()

    async def _run(self) -> None:
        try:
            await self.db.update_session(self.id, {"started_at": utcnow()})
            await self._set_phase(STARTING, self.exp.start_countdown_seconds)
            await asyncio.sleep(self.exp.start_countdown_seconds)

            for r in range(1, self.exp.rounds + 1):
                self.round = r
                self.votes[r] = {l: None for l in self.seats}
                self._votes_complete.clear()

                # Chat phase
                await self._set_phase(CHAT, self.exp.chat_duration_seconds)
                chat_tasks = [self._spawn(s.agent.chat_round(r)) for s in self.seats.values() if s.agent]
                await asyncio.sleep(self.exp.chat_duration_seconds)
                for t in chat_tasks:
                    t.cancel()

                # Vote phase
                await self._set_phase(VOTE, self.exp.vote_duration_seconds)
                vote_tasks = [self._spawn(s.agent.vote(r)) for s in self.seats.values() if s.agent]
                try:
                    await asyncio.wait_for(self._votes_complete.wait(), timeout=self.exp.vote_duration_seconds)
                except asyncio.TimeoutError:
                    pass
                for t in vote_tasks:
                    t.cancel()

                tally = tally_votes(self.votes[r])
                result = {"round": r, "tally": tally}
                self.round_results.append(result)
                await self.db.insert_result({"session_id": self.id, "kind": "round", **result, "ts": utcnow()})

                if r < self.exp.rounds:
                    await self._set_phase(RESULTS, self.exp.results_display_seconds)
                    await asyncio.sleep(self.exp.results_display_seconds)

            outcome = compute_winner([rr["tally"] for rr in self.round_results], rng=self.rng)
            self.final = outcome.to_dict()
            await self.db.insert_result({"session_id": self.id, "kind": "final", "round": None, **self.final, "ts": utcnow()})
            await self.db.update_session(self.id, {"ended_at": utcnow(), "result": self.final})
            await self._set_phase(FINISHED, None)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            log.exception("session %s crashed", self.id)
        finally:
            self.manager.session_finished(self)

    # ------------------------------------------------------------- actions
    async def post_message(self, letter: str, text: str, expected_round: Optional[int] = None) -> bool:
        if self.phase != CHAT or (expected_round is not None and expected_round != self.round):
            return False
        text = " ".join(text.split())[: self.exp.max_message_length]
        if not text:
            return False
        msg = {
            "id": uuid.uuid4().hex,
            "sender": letter,
            "text": text,
            "round": self.round,
            "ts": now_ms(),
        }
        self.messages.append(msg)
        await self.broadcast({"type": "chat", "message": msg})
        await self.db.insert_message({"session_id": self.id, **msg, "ts": utcnow()})
        for seat in self.seats.values():
            if seat.agent:
                seat.agent.notify_message(msg)
        return True

    async def human_message(self, letter: str, text: str) -> None:
        now = time.monotonic()
        if now - self._last_human_msg.get(letter, 0) < 0.5:  # basic flood guard
            return
        self._last_human_msg[letter] = now
        await self.post_message(letter, str(text))

    async def cast_vote(self, letter: str, target: str, expected_round: Optional[int] = None) -> bool:
        if self.phase != VOTE or target not in self.seats:
            return False
        if expected_round is not None and expected_round != self.round:
            return False
        if self.votes[self.round].get(letter) is not None:
            return False  # one vote per round, no changes
        self.votes[self.round][letter] = target
        await self.db.upsert_vote({
            "session_id": self.id, "round": self.round, "voter": letter,
            "target": target, "ts": utcnow(),
        })
        await self.send_state(letter)
        if all(v is not None for v in self.votes[self.round].values()):
            self._votes_complete.set()
        return True

    # ----------------------------------------------------------- outbound
    def public_state(self, letter: Optional[str]) -> dict:
        my_vote = self.votes.get(self.round, {}).get(letter) if letter else None
        return {
            "type": "state",
            "phase": self.phase,
            "round": self.round,
            "total_rounds": self.exp.rounds,
            "deadline": self.deadline_ms,
            "server_time": now_ms(),
            "seats": [{"id": l, "occupied": s.occupied} for l, s in self.seats.items()],
            "you": letter,
            "my_vote": my_vote,
            "round_results": self.round_results,
            "final": self.final,
            "topic": self.exp.topic,
            "max_message_length": self.exp.max_message_length,
        }

    async def _send(self, seat: Seat, payload: dict) -> None:
        if seat.ws is None:
            return
        try:
            await seat.ws.send_json(payload)
        except Exception:  # noqa: BLE001 - dead socket; cleanup happens in the ws handler
            pass

    async def send_state(self, letter: str) -> None:
        await self._send(self.seats[letter], self.public_state(letter))

    async def send_history(self, letter: str) -> None:
        await self._send(self.seats[letter], {"type": "history", "messages": self.messages})

    async def broadcast_state(self) -> None:
        await asyncio.gather(*(self.send_state(l) for l in self.seats))

    async def broadcast(self, payload: dict) -> None:
        await asyncio.gather(*(self._send(s, payload) for s in self.seats.values()))


class SessionManager:
    """Routes arriving participants into a waiting session (creating one lazily)."""

    def __init__(self, db: Database):
        self.db = db
        self.open_session: Optional[Session] = None
        self.tokens: dict[str, tuple[Session, str]] = {}
        self.active: dict[str, Session] = {}
        self._lock = asyncio.Lock()

    def forget_token(self, token: Optional[str]) -> None:
        if token:
            self.tokens.pop(token, None)

    def session_started(self, session: Session) -> None:
        if self.open_session is session:
            self.open_session = None
        self.active[session.id] = session

    def session_finished(self, session: Session) -> None:
        self.active.pop(session.id, None)
        if self.open_session is session:
            self.open_session = None
        # Keep tokens for a while so participants can reload and see results.
        asyncio.get_running_loop().call_later(600, self._drop_tokens, session)

    def _drop_tokens(self, session: Session) -> None:
        for tok in [t for t, (s, _) in self.tokens.items() if s is session]:
            self.tokens.pop(tok, None)

    async def join(self, ws: WebSocket, token: Optional[str]) -> tuple[Session, str, str]:
        async with self._lock:
            if token and token in self.tokens:
                session, letter = self.tokens[token]
                await session.attach(letter, ws)
                return session, letter, token

            session = self.open_session
            if session is not None and session.phase == WAITING and not session.has_humans():
                # Nobody is waiting in it: start fresh so the current config.yml and a
                # new random seat assignment apply.
                await session.abandon()
                session = self.open_session = None
            if session is None or session.phase != WAITING or not session.free_human_seats():
                session = Session(load_config(), self.db, self)
                await session.init()
                self.open_session = session
                log.info("created session %s (condition=%s)", session.id, session.condition)
            letter, new_token = await session.add_human(ws)
            self.tokens[new_token] = (session, letter)
            return session, letter, new_token
