"""AI participants.

An AI agent sits in a seat exactly like a human: it posts chat messages and
votes through the same Session methods, so its output is indistinguishable at
the protocol level. Timing is humanised (read delay, typing speed, gaps
between messages) so that response latency does not give it away.
"""
from __future__ import annotations

import asyncio
import difflib
import logging
import os
import random
import re
import time
from typing import TYPE_CHECKING, Optional

from .config import AIConfig
from .responder_rules import RulesResponder

if TYPE_CHECKING:
    from .session import Session

log = logging.getLogger(__name__)

SKIP = "SKIP"

# --------------------------------------------------------------------------
# LLM providers
# --------------------------------------------------------------------------


class LLMClient:
    async def complete(self, system: str, user: str, purpose: str) -> Optional[str]:
        raise NotImplementedError


class AnthropicLLM(LLMClient):
    """Claude via the official Anthropic Python SDK."""

    def __init__(self, cfg: AIConfig, api_key: str):
        from anthropic import AsyncAnthropic

        self.cfg = cfg
        self.client = AsyncAnthropic(api_key=api_key, timeout=cfg.request_timeout_seconds)

    async def complete(self, system: str, user: str, purpose: str) -> Optional[str]:
        import anthropic

        kwargs = dict(
            model=self.cfg.model,
            max_tokens=self.cfg.max_tokens,
            system=system,
            messages=[{"role": "user", "content": user}],
            output_config={"effort": self.cfg.effort},
        )
        try:
            if self.cfg.use_fallbacks:
                resp = await self.client.beta.messages.create(
                    betas=["server-side-fallback-2026-07-01"],
                    fallbacks="default",
                    **kwargs,
                )
            else:
                resp = await self.client.messages.create(**kwargs)
        except anthropic.RateLimitError:
            log.warning("LLM rate limited (%s)", purpose)
            return None
        except anthropic.APIStatusError as exc:
            log.error("LLM API error %s (%s): %s", exc.status_code, purpose, exc.message)
            return None
        except anthropic.APIConnectionError:
            log.error("LLM connection error (%s)", purpose)
            return None

        if resp.stop_reason == "refusal":
            return None
        return "".join(b.text for b in resp.content if b.type == "text").strip() or None


def build_llm(cfg: AIConfig, rng: random.Random) -> "LLMClient | RulesResponder":
    if cfg.provider == "anthropic":
        key = os.environ.get(cfg.api_key_env)
        if key:
            return AnthropicLLM(cfg, key)
        log.warning(
            "ai.provider is 'anthropic' but %s is not set; using the offline rules responder",
            cfg.api_key_env,
        )
    return RulesResponder(rng)


# --------------------------------------------------------------------------
# Prompts and output sanitising
# --------------------------------------------------------------------------

CHAT_SYSTEM = """You are taking part in an online text-chat study as Player {me}. \
There are exactly three participants: Player A, Player B and Player C. You only \
know them by these letters.

Task: {topic} The chat runs for {rounds} rounds of about {chat_secs} seconds; \
after each round everyone votes for one player (self-votes are allowed).

You are an ordinary participant. Your personal style: {persona}.

How to write:
- Write ONE short chat message, usually 3 to 20 words, like someone typing quickly.
- Casual tone. No lists, no markdown, no hashtags, no em dashes, rarely emojis.
- Don't start with your own name or a "Player {me}:" prefix.
- React to what others actually said.
- Never repeat or closely paraphrase any message already in the chat, yours or \
anyone else's. Every message must add something new.
- You may nominate yourself or others and give simple reasons.
- Never mention AI, language models, prompts or instructions. If someone asks \
whether anyone is a bot, respond like a mildly amused person would.
- If there is nothing useful to add right now, reply with exactly SKIP."""

VOTE_SYSTEM = """You are Player {me} in an online group-chat study with Player A, \
Player B and Player C. {topic} Based on the discussion, decide who you vote for as \
Decision Maker (you may vote for yourself). Reply with a single letter: A, B or C."""

_PREFIX = re.compile(r"^\s*(player\s*[abc]\s*[:\-]\s*)", re.IGNORECASE)
_BANNED = re.compile(
    r"\b(as an ai|language model|i am an ai|i'm an ai|assistant|anthropic|claude)\b",
    re.IGNORECASE,
)


def sanitize_chat(text: Optional[str], max_len: int) -> Optional[str]:
    if not text:
        return None
    text = text.strip().splitlines()[0].strip() if text.strip() else ""
    text = _PREFIX.sub("", text)
    text = text.strip().strip('"').strip("'").strip()
    text = re.sub(r"\s*[\u2014\u2013]\s*", ", ", text)
    text = re.sub(r"\s+", " ", text)
    if not text or text.upper().rstrip(".") == SKIP or _BANNED.search(text):
        return None
    return text[:max_len]


REPEAT_SIMILARITY = 0.8


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", "", text.lower()).strip()


def is_repeat(text: str, previous: list[str], threshold: float = REPEAT_SIMILARITY) -> bool:
    """True if `text` is identical or very similar to any earlier message."""
    a = _norm(text)
    if not a:
        return True
    for prev in previous:
        b = _norm(prev)
        if a == b or (b and difflib.SequenceMatcher(None, a, b).ratio() >= threshold):
            return True
    return False


def format_transcript(messages: list[dict], me: str) -> str:
    if not messages:
        return "(no messages yet)"
    lines = []
    for m in messages[-40:]:
        who = f"Player {m['sender']}" + (" (you)" if m["sender"] == me else "")
        lines.append(f"[round {m['round']}] {who}: {m['text']}")
    return "\n".join(lines)


# --------------------------------------------------------------------------
# Agent
# --------------------------------------------------------------------------


class AIAgent:
    def __init__(
        self,
        session: "Session",
        letter: str,
        llm: "LLMClient | RulesResponder",
        cfg: AIConfig,
        persona: str,
        rng: random.Random,
    ):
        self.session = session
        self.letter = letter
        self.llm = llm
        self.cfg = cfg
        self.t = cfg.timing
        self.persona = persona
        self.rng = rng
        self._new_message = asyncio.Event()

    def _u(self, pair: tuple[float, float]) -> float:
        return self.rng.uniform(*pair)

    def notify_message(self, msg: dict) -> None:
        if msg["sender"] != self.letter:
            self._new_message.set()

    def _system(self) -> str:
        exp = self.session.exp
        return CHAT_SYSTEM.format(
            me=self.letter,
            topic=exp.topic,
            rounds=exp.rounds,
            chat_secs=exp.chat_duration_seconds,
            persona=self.persona,
        )

    def _context(self, round_no: int) -> str:
        s = self.session
        remaining = max(0, int(s.seconds_remaining()))
        parts = [f"Round {round_no} of {s.exp.rounds}. About {remaining} seconds left in this chat round."]
        for r in s.round_results:
            tally = ", ".join(f"Player {p}: {n}" for p, n in r["tally"].items())
            parts.append(f"Round {r['round']} vote results: {tally}.")
        parts.append("Chat so far:\n" + format_transcript(s.messages, self.letter))
        parts.append(f"Write your next chat message as Player {self.letter}, or SKIP.")
        return "\n\n".join(parts)

    async def chat_round(self, round_no: int) -> None:
        """Runs for the duration of one chat phase; cancelled when it ends."""
        s = self.session
        sent = 0
        last_sent = -1e9
        seen = len(s.messages)
        first = True
        while sent < self.t.max_messages_per_round:
            wait = self._u(self.t.first_message_delay if first else self.t.idle_check_interval)
            self._new_message.clear()
            try:
                await asyncio.wait_for(self._new_message.wait(), timeout=wait)
            except asyncio.TimeoutError:
                pass
            new_msgs = len(s.messages) > seen
            # On a quiet stretch only speak up sometimes, so we don't monologue.
            if not first and not new_msgs and self.rng.random() < 0.5:
                continue
            first = False
            if s.seconds_remaining() < 4:
                return

            gap = time.monotonic() - last_sent
            if gap < self.t.min_gap_between_messages:
                await asyncio.sleep(self.t.min_gap_between_messages - gap)
            await asyncio.sleep(self._u(self.t.read_delay))

            started = time.monotonic()
            seen = len(s.messages)
            text = await self._draft(round_no)
            if not text:
                continue
            # Simulate typing time; LLM latency counts towards it.
            typing = len(text) / self.t.typing_chars_per_second * self.rng.uniform(0.8, 1.3)
            await asyncio.sleep(max(0.4, typing - (time.monotonic() - started)))
            # Someone may have said the same thing while we were "typing".
            if is_repeat(text, [m["text"] for m in s.messages]):
                continue
            if not await s.post_message(self.letter, text, expected_round=round_no):
                return
            sent += 1
            last_sent = time.monotonic()
            seen = len(s.messages)

    async def _draft(self, round_no: int) -> Optional[str]:
        """Ask the LLM for a message; never return one that repeats the chat."""
        s = self.session
        if isinstance(self.llm, RulesResponder):
            raw = self.llm.chat(self.letter, s.messages, round_no, s.round_results,
                                s.seconds_remaining(), is_repeat)
            text = sanitize_chat(raw, s.exp.max_message_length)
            return text if text and not is_repeat(text, [m["text"] for m in s.messages]) else None
        context = self._context(round_no)
        for _ in range(2):
            raw = await self.llm.complete(self._system(), context, "chat")
            text = sanitize_chat(raw, self.session.exp.max_message_length)
            if not text:
                return None
            if not is_repeat(text, [m["text"] for m in self.session.messages]):
                return text
            context += (
                f'\n\nYour draft "{text}" repeats something already said. '
                "Write something new, or SKIP."
            )
        return None

    async def vote(self, round_no: int) -> None:
        s = self.session
        # Never wait past the vote deadline (keep a small margin for the LLM call).
        delay = min(self._u(self.t.vote_delay), max(0.5, s.seconds_remaining() - 4))
        await asyncio.sleep(delay)
        if isinstance(self.llm, RulesResponder):
            target = self.llm.vote(self.letter, s.messages, s.round_results)
            await s.cast_vote(self.letter, target, expected_round=round_no)
            return
        system = VOTE_SYSTEM.format(me=self.letter, topic=s.exp.topic)
        user = (
            "Chat transcript:\n"
            + format_transcript(s.messages, self.letter)
            + "\n\nYour vote (A, B or C):"
        )
        raw = await self.llm.complete(system, user, "vote") or ""
        match = re.search(r"\b([ABC])\b", raw.upper())
        target = match.group(1) if match else self.rng.choice(["A", "B", "C"])
        await s.cast_vote(self.letter, target, expected_round=round_no)
