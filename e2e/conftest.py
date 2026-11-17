"""End-to-end test harness.

Drives the real running stack (`docker compose up`) through nginx, exactly like
browsers do: WebSocket clients join, chat, vote, disconnect, reconnect.
Between scenarios it rewrites config.yml (the backend re-reads it for every new
session) using fast timings, and restores the original file afterwards.

Run from the project root:   python -m pytest e2e -v
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest
import websockets
import yaml

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config.yml"
sys.path.insert(0, str(ROOT / "backend"))

BASE_URL = os.environ.get("E2E_BASE_URL", "http://localhost:3000")
WS_URL = BASE_URL.replace("http", "ws", 1) + "/ws"

# Fast timings so a 2-round session takes ~30 s instead of ~3 min.
FAST = {
    "experiment": {
        "rounds": 2,
        "chat_duration_seconds": 6,
        "vote_duration_seconds": 8,
        "results_display_seconds": 1,
        "start_countdown_seconds": 1,
        "abort_after_all_humans_left_seconds": 3,
    },
    "ai": {
        "provider": "rules",
        "timing": {
            "join_delay": [1.0, 2.0],
            "first_message_delay": [0.5, 1.5],
            "idle_check_interval": [1.0, 2.0],
            "read_delay": [0.2, 0.5],
            "typing_chars_per_second": 60,
            "min_gap_between_messages": 1,
            "max_messages_per_round": 3,
            "vote_delay": [0.5, 2.0],
        },
    },
}

# Exactly what a participant may see; anything more could leak information.
STATE_KEYS = {
    "type", "phase", "round", "total_rounds", "deadline", "server_time", "seats",
    "you", "my_vote", "round_results", "final", "topic", "max_message_length",
}
CHAT_KEYS = {"id", "sender", "text", "round", "ts"}


# ----------------------------------------------------------------- config
def _merge(base: dict, extra: dict) -> dict:
    out = copy.deepcopy(base)
    for k, v in extra.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class ConfigSwitcher:
    def __init__(self, original_text: str):
        self.original_text = original_text
        self.original = yaml.safe_load(original_text)

    def set(self, condition: str | None = None, **overrides) -> None:
        cfg = _merge(self.original, FAST)
        cfg = _merge(cfg, overrides)
        if condition:
            cfg["condition"] = condition
        CONFIG.write_text(yaml.safe_dump(cfg, sort_keys=False))

    def raw(self, text: str) -> None:
        CONFIG.write_text(text)

    def restore(self) -> None:
        CONFIG.write_text(self.original_text)


@pytest.fixture(scope="session")
def config_switcher():
    try:
        urllib.request.urlopen(BASE_URL + "/api/health", timeout=5)
    except Exception as exc:  # noqa: BLE001
        pytest.exit(f"stack not reachable at {BASE_URL} ({exc}); run `docker compose up -d` first")
    original = CONFIG.read_text()
    sw = ConfigSwitcher(original)
    try:
        yield sw
    finally:
        sw.restore()


@pytest.fixture
def cfg(config_switcher):
    yield config_switcher
    config_switcher.set()  # back to fast defaults between tests


# ----------------------------------------------------------------- docker / db
def compose(*args: str, env: dict | None = None, timeout: int = 180) -> str:
    full_env = {**os.environ, **(env or {})}
    res = subprocess.run(["docker", "compose", *args], cwd=ROOT, env=full_env,
                         capture_output=True, text=True, timeout=timeout)
    if res.returncode != 0:
        raise RuntimeError(f"docker compose {' '.join(args)} failed:\n{res.stderr}")
    return res.stdout


def mongo(js: str):
    out = compose("exec", "-T", "mongo", "mongosh", "--quiet", "human_ai_experiment",
                  "--eval", f"EJSON.stringify(({js}), {{relaxed: true}})")
    return json.loads(out.strip().splitlines()[-1])


def session_by_nonce(nonce: str) -> dict:
    """Find the persisted session that contains a chat message with `nonce`."""
    sid = mongo(f'db.messages.findOne({{text: {{$regex: "{nonce}"}}}}).session_id')
    return mongo(f"""(() => {{
        const s = db.sessions.findOne({{_id: "{sid}"}});
        s.messages = db.messages.find({{session_id: "{sid}"}}).toArray();
        s.votes = db.votes.find({{session_id: "{sid}"}}).toArray();
        s.results = db.results.find({{session_id: "{sid}"}}).toArray();
        return s; }})()""")


def wait_healthy(timeout: float = 60) -> None:
    end = time.time() + timeout
    while time.time() < end:
        try:
            urllib.request.urlopen(BASE_URL + "/api/health", timeout=2)
            return
        except Exception:  # noqa: BLE001
            time.sleep(1)
    raise RuntimeError("backend did not become healthy")


# ----------------------------------------------------------------- client
class Client:
    """A scripted participant speaking the same protocol as the React app."""

    def __init__(self, label: str = ""):
        self.label = label
        self.frames: list[str] = []
        self.states: list[dict] = []
        self.chats: list[dict] = []
        self.history: list[dict] | None = None
        self.errors: list[dict] = []
        self.welcome: dict | None = None
        self.close_code: int | None = None
        self.ws = None
        self._cond = asyncio.Condition()
        self._task = None

    # connection ---------------------------------------------------------
    async def connect(self, token: str | None = None, join: bool = True) -> "Client":
        self.ws = await websockets.connect(WS_URL)
        self._task = asyncio.create_task(self._reader())
        if join:
            await self.send({"type": "join", "token": token})
            await self.wait(lambda: self.welcome or self.errors or self.close_code is not None, 10,
                            "welcome")
        return self

    async def _reader(self) -> None:
        try:
            async for raw in self.ws:
                self.frames.append(raw)
                m = json.loads(raw)
                kind = m.get("type")
                if kind == "welcome":
                    self.welcome = m
                elif kind == "state":
                    self.states.append(m)
                elif kind == "chat":
                    self.chats.append(m["message"])
                elif kind == "history":
                    self.history = m["messages"]
                elif kind == "error":
                    self.errors.append(m)
                async with self._cond:
                    self._cond.notify_all()
        except websockets.ConnectionClosed:
            pass
        finally:
            self.close_code = self.ws.close_code if self.ws.close_code is not None else -1
            async with self._cond:
                self._cond.notify_all()

    async def send(self, obj) -> None:
        await self.ws.send(obj if isinstance(obj, str) else json.dumps(obj))

    async def chat(self, text: str) -> None:
        await self.send({"type": "chat", "text": text})

    async def vote(self, target) -> None:
        await self.send({"type": "vote", "target": target})

    async def close(self) -> None:
        if self.ws is not None:
            await self.ws.close()
            await asyncio.sleep(0.2)

    # waiting ------------------------------------------------------------
    async def wait(self, pred, timeout: float, what: str = "condition") -> None:
        try:
            async with self._cond:
                await asyncio.wait_for(self._cond.wait_for(pred), timeout)
        except asyncio.TimeoutError:
            raise AssertionError(
                f"[{self.label or self.me}] timed out after {timeout}s waiting for {what}; "
                f"last state: {self.state and {k: self.state[k] for k in ('phase', 'round')}}"
            ) from None

    def saw(self, phase: str, round_no: int | None = None) -> bool:
        return any(s["phase"] == phase and (round_no is None or s["round"] == round_no) for s in self.states)

    async def wait_phase(self, phase: str, round_no: int | None = None, timeout: float = 40) -> dict:
        await self.wait(lambda: self.saw(phase, round_no), timeout, f"phase={phase} round={round_no}")
        return next(s for s in self.states if s["phase"] == phase and (round_no is None or s["round"] == round_no))

    async def wait_chat(self, needle: str, timeout: float = 10) -> dict:
        await self.wait(lambda: any(needle in c["text"] for c in self.chats), timeout, f"chat '{needle}'")
        return next(c for c in self.chats if needle in c["text"])

    # derived ------------------------------------------------------------
    @property
    def me(self) -> str | None:
        return self.welcome["you"] if self.welcome else None

    @property
    def token(self) -> str | None:
        return self.welcome["token"] if self.welcome else None

    @property
    def state(self) -> dict | None:
        return self.states[-1] if self.states else None

    def occupied(self) -> int:
        return sum(s["occupied"] for s in self.state["seats"]) if self.state else 0

    def phase_sequence(self) -> list[tuple[str, int]]:
        seq: list[tuple[str, int]] = []
        for s in self.states:
            key = (s["phase"], s["round"])
            if not seq or seq[-1] != key:
                seq.append(key)
        return seq


def run(coro, timeout: float = 240):
    """Run an async scenario with an overall safety timeout."""
    return asyncio.run(asyncio.wait_for(coro, timeout))
