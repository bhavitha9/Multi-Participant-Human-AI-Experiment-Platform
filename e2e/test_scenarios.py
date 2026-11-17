"""Scenario matrix for the running app (see conftest.py for the harness).

Sections
  1. Each condition end to end (3 humans / 2 humans + 1 AI / 1 human + 2 AI)
  2. Winner rules, driven live with scripted votes
  3. Waiting room
  4. Disconnects and reconnects
  5. Bad or out-of-phase input
  6. Configuration
  7. Robustness (database outage)
"""
from __future__ import annotations

import asyncio
import json
import random
import re
import time
import urllib.error
import urllib.request
import uuid

import pytest

from app.agents import is_repeat
from app.winner import METHOD_RANDOM, compute_winner
from conftest import (BASE_URL, CHAT_KEYS, STATE_KEYS, Client, compose, mongo, run,
                      session_by_nonce, wait_healthy)

CONDITIONS = {"three_humans": 3, "two_humans_one_ai": 2, "one_human_two_ai": 1}


# ================================================================== helpers
async def join(n: int, gap: float = 0.3) -> list[Client]:
    clients = []
    for i in range(n):
        clients.append(await Client(f"h{i}").connect())
        await asyncio.sleep(gap)
    return clients


async def play(clients, plan=None, rounds=2, chat=True) -> str:
    """Drive clients through every round. plan = {round: {letter: target|None}};
    letters not in the plan vote for themselves. Returns the chat nonce used."""
    nonce = uuid.uuid4().hex[:10]
    plan = plan or {}
    for r in range(1, rounds + 1):
        await asyncio.gather(*(c.wait_phase("chat", r, 60) for c in clients))
        if chat:
            for c in clients:
                await c.chat(f"e2e-{nonce} hello from {c.me} in round {r}")
        await asyncio.gather(*(c.wait_phase("vote", r, 30) for c in clients))
        for c in clients:
            target = plan.get(r, {}).get(c.me, c.me)
            if target is not None:
                await c.vote(target)
    await asyncio.gather(*(c.wait_phase("finished", None, 40) for c in clients))
    return nonce


def expected_sequence(rounds: int) -> list[tuple[str, int]]:
    seq = [("starting", 0)]
    for r in range(1, rounds + 1):
        seq += [("chat", r), ("vote", r)]
        if r < rounds:
            seq.append(("results", r))
    return seq + [("finished", rounds)]


def check_finished_session(clients, rounds=2, votes_per_round=3) -> dict:
    """Assertions every finished session must satisfy. Returns the final state."""
    finals = [c.state for c in clients]
    expected_seq = expected_sequence(rounds)
    for c in clients:
        seq = [p for p in c.phase_sequence() if p[0] != "waiting"]
        # a client that reconnected mid-session sees a tail of the sequence
        assert seq and seq == expected_seq[expected_seq.index(seq[0]):], f"{c.me}: phases {seq}"
    first = finals[0]
    for f in finals[1:]:
        assert f["final"] == first["final"], "clients disagree on the final result"
        assert f["round_results"] == first["round_results"]
    assert len(first["round_results"]) == rounds
    for rr in first["round_results"]:
        assert sum(rr["tally"].values()) == votes_per_round, f"round {rr['round']} tally {rr['tally']}"

    # winner follows the rules exactly
    tallies = [rr["tally"] for rr in first["round_results"]]
    expected = compute_winner(tallies, rng=random.Random(0))
    final = first["final"]
    assert final["method"] == expected.method
    if expected.method == METHOD_RANDOM:
        assert final["winner"] in expected.tied_after_totals
    else:
        assert final["winner"] == expected.winner

    for c in clients:
        assert_anonymous(c)
    return first


def assert_anonymous(c: Client) -> None:
    for raw in c.frames:
        assert not re.search(r'"(human|ai)"', raw, re.I), f"participant type leaked: {raw[:200]}"
    for s in c.states:
        assert set(s) == STATE_KEYS, f"unexpected state keys {set(s) ^ STATE_KEYS}"
        for seat in s["seats"]:
            assert set(seat) == {"id", "occupied"}
    for m in c.chats + (c.history or []):
        assert set(m) == CHAT_KEYS
        assert re.fullmatch(r"[0-9a-f]{32}", m["id"])


async def close_all(clients) -> None:
    await asyncio.gather(*(c.close() for c in clients), return_exceptions=True)


# ================================================================== 1. conditions
@pytest.mark.parametrize("condition", list(CONDITIONS))
def test_condition_full_session(cfg, condition):
    """Every condition: seats fill, 2 rounds run, everyone sees the same result,
    nothing reveals types, and everything is stored in the database."""
    humans = CONDITIONS[condition]
    cfg.set(condition)

    async def scenario():
        clients = await join(humans)
        nonce = await play(clients)
        final = check_finished_session(clients)
        letters = {c.me for c in clients}
        assert len(letters) == humans, "two humans got the same seat"
        await close_all(clients)
        return clients, nonce, final

    clients, nonce, final = run(scenario())
    doc = session_by_nonce(nonce)
    types = sorted(v["type"] for v in doc["seats"].values())
    assert doc["condition"] == condition
    assert types.count("human") == humans and types.count("ai") == 3 - humans
    assert {l for l, v in doc["seats"].items() if v["type"] == "human"} == {c.me for c in clients}
    assert doc["status"] == "finished" and doc["result"]["winner"] == final["final"]["winner"]
    assert len(doc["votes"]) == 6
    assert len([r for r in doc["results"] if r["kind"] == "round"]) == 2
    assert len([r for r in doc["results"] if r["kind"] == "final"]) == 1
    assert len(doc["messages"]) == len(clients[0].chats), "every chat message is persisted"

    ai_letters = {l for l, v in doc["seats"].items() if v["type"] == "ai"}
    ai_texts = [m["text"] for m in doc["messages"] if m["sender"] in ai_letters]
    if ai_letters:
        assert ai_texts, "AI seats never said anything"
        assert not any(is_repeat(t, ai_texts[:i]) for i, t in enumerate(ai_texts)), "AI repeated itself"
        assert {v["voter"] for v in doc["votes"]} >= ai_letters, "an AI seat didn't vote"


# ================================================================== 2. winner rules
WINNER_CASES = [
    # name, round-1 votes, round-2 votes, expected method, possible winners
    ("round2_majority", {"A": "A", "B": "A", "C": "B"}, {"A": "B", "B": "B", "C": "A"},
     "final_round_majority", {"B"}),
    ("round2_majority_beats_round1_leader", {"A": "C", "B": "C", "C": "C"}, {"A": "A", "B": "A", "C": "C"},
     "final_round_majority", {"A"}),
    ("three_way_tie_broken_by_totals", {"A": "A", "B": "A", "C": "B"}, {"A": "A", "B": "B", "C": "C"},
     "total_votes_tiebreak", {"A"}),
    ("only_tied_players_count", {"A": "C", "B": "C", "C": "B"}, {"A": "A", "B": "B", "C": None},
     "total_votes_tiebreak", {"B"}),
    ("still_tied_random_excludes_others", {"A": "C", "B": "C", "C": "C"}, {"A": "A", "B": "B", "C": None},
     "random_tiebreak", {"A", "B"}),
    ("everyone_self_votes_both_rounds", {"A": "A", "B": "B", "C": "C"}, {"A": "A", "B": "B", "C": "C"},
     "random_tiebreak", {"A", "B", "C"}),
    ("nobody_votes_at_all", {"A": None, "B": None, "C": None}, {"A": None, "B": None, "C": None},
     "random_tiebreak", {"A", "B", "C"}),
]


@pytest.mark.parametrize("name,r1,r2,method,winners", WINNER_CASES, ids=[c[0] for c in WINNER_CASES])
def test_winner_rules_live(cfg, name, r1, r2, method, winners):
    cfg.set("three_humans")

    async def scenario():
        clients = await join(3)
        t0 = time.time()
        await play(clients, plan={1: r1, 2: r2})
        votes = [sum(v is not None for v in r.values()) for r in (r1, r2)]
        state = clients[0].state
        for rr, n in zip(state["round_results"], votes):
            assert sum(rr["tally"].values()) == n
        final = check_finished_session(clients, votes_per_round=votes[0]) if votes[0] == votes[1] else state
        assert final["final"]["method"] == method, final["final"]
        assert final["final"]["winner"] in winners, final["final"]
        if name == "nobody_votes_at_all":
            # vote phase must wait for its full timer when nobody votes
            assert time.time() - t0 >= 2 * 8, "vote phase ended early without votes"
        if name.startswith("everyone_self"):
            # self-votes are accepted and echoed back to the voter
            assert all(any(s["my_vote"] == c.me for s in c.states) for c in clients)
        await close_all(clients)

    run(scenario())


# ================================================================== 3. waiting room
def test_waiting_room_fills_live_and_waits_for_all_humans(cfg):
    cfg.set("three_humans")

    async def scenario():
        a = await Client("a").connect()
        await a.wait(lambda: a.state is not None, 5, "state")
        assert a.occupied() == 1 and a.state["phase"] == "waiting"
        b = await Client("b").connect()
        await a.wait(lambda: a.occupied() == 2, 5, "second seat to fill")
        await asyncio.sleep(3)
        assert a.state["phase"] == "waiting", "started without all humans"
        c = await Client("c").connect()
        await a.wait_phase("starting", None, 5)
        assert len({a.me, b.me, c.me}) == 3
        await close_all([a, b, c])

    run(scenario())


def test_leaving_waiting_room_frees_the_seat(cfg):
    cfg.set("three_humans")

    async def scenario():
        a = await Client("a").connect()
        b = await Client("b").connect()
        await a.wait(lambda: a.occupied() == 2, 5, "b to join")
        await b.close()
        await a.wait(lambda: a.occupied() == 1, 5, "b's seat to be freed")
        c = await Client("c").connect()
        d = await Client("d").connect()
        await a.wait_phase("starting", None, 5)
        assert len({a.me, c.me, d.me}) == 3
        await close_all([a, c, d])

    run(scenario())


def test_ai_seats_fill_automatically_but_not_instantly(cfg):
    cfg.set("one_human_two_ai")

    async def scenario():
        t0 = time.time()
        h = await Client("h").connect()
        await h.wait(lambda: h.state and h.occupied() >= 2, 10, "first AI seat")
        first_ai = time.time() - t0
        await h.wait(lambda: h.occupied() == 3, 10, "second AI seat")
        await h.wait_phase("starting", None, 10)
        assert first_ai >= 0.8, f"AI seat filled after {first_ai:.2f}s (looks automated)"
        await h.close()

    run(scenario())


def test_seat_letters_are_randomised_between_sessions(cfg):
    cfg.set("one_human_two_ai")

    async def scenario():
        letters = []
        for _ in range(8):
            h = await Client().connect()
            letters.append(h.me)
            await h.close()
        return letters

    letters = run(scenario())
    assert len(set(letters)) >= 2, f"human always got seat {letters[0]}: {letters}"


def test_extra_human_starts_a_separate_session(cfg):
    cfg.set("two_humans_one_ai")

    async def scenario():
        s1 = await join(2)
        late = await Client("late").connect()
        await late.wait(lambda: late.state is not None, 5, "state")
        assert late.state["phase"] == "waiting", "late human was put into a full session"
        partner = await Client("partner").connect()
        s2 = [late, partner]
        n1, n2 = await asyncio.gather(play(s1), play(s2))
        check_finished_session(s1)
        check_finished_session(s2)
        for c in s1:
            assert not any(n2 in m["text"] for m in c.chats), "messages leaked across sessions"
        for c in s2:
            assert not any(n1 in m["text"] for m in c.chats), "messages leaked across sessions"
        await close_all(s1 + s2)

    run(scenario(), timeout=300)


# ================================================================== 4. reconnects
def test_reload_mid_chat_keeps_seat_history_and_result(cfg):
    cfg.set("one_human_two_ai")

    async def scenario():
        h = await Client("h").connect()
        await h.wait_phase("chat", 1)
        await h.chat("e2e-before-reload")
        await h.wait_chat("e2e-before-reload")
        await h.close()

        again = await Client("again").connect(token=h.token)
        assert again.me == h.me, "seat changed after reload"
        await again.wait(lambda: again.history is not None, 5, "history")
        assert any("e2e-before-reload" in m["text"] for m in again.history), "history lost"
        await play([again], plan={1: {again.me: "A"}}, chat=False)
        assert again.state["final"] is not None
        await again.close()

        # reopening after the session finished still shows the final result
        late = await Client("late").connect(token=h.token)
        await late.wait(lambda: late.state is not None, 5, "state")
        assert late.state["phase"] == "finished" and late.state["final"] == again.state["final"]
        await late.close()

    run(scenario())


def test_unknown_token_is_treated_as_new_participant(cfg):
    cfg.set("one_human_two_ai")

    async def scenario():
        h = await Client().connect(token="not-a-real-token")
        assert h.me in "ABC" and h.token != "not-a-real-token"
        await h.close()

    run(scenario())


def test_second_tab_with_same_token_replaces_first(cfg):
    cfg.set("one_human_two_ai")

    async def scenario():
        first = await Client("first").connect()
        second = await Client("second").connect(token=first.token)
        assert second.me == first.me
        await first.wait(lambda: first.close_code is not None, 5, "old tab to be closed")
        assert first.close_code == 4000
        await second.close()

    run(scenario())


def test_all_humans_leave_mid_session_aborts_it(cfg):
    """2 humans + 1 AI: both humans leave during round 1 -> after the grace
    period the session is aborted, the AI stops, and it's stored as aborted."""
    cfg.set("two_humans_one_ai")

    async def scenario():
        a, b = await join(2)
        await a.wait_phase("chat", 1)
        await a.chat("e2e-abort-marker")
        await a.wait_chat("e2e-abort-marker")
        token = a.token
        await close_all([a, b])
        await asyncio.sleep(3 + 2)          # grace period (3 s in e2e config) + margin

        back = await Client("back").connect(token=token)
        await back.wait(lambda: back.state is not None, 5, "state")
        assert back.state["phase"] == "aborted", f"phase is {back.state['phase']}"
        assert back.state["final"] is None, "an aborted session must not elect anyone"
        assert_anonymous(back)
        await back.close()

        fresh = await Client("fresh").connect()
        await fresh.wait(lambda: fresh.state is not None, 5, "state")
        assert fresh.state["phase"] == "waiting", "new arrival dropped into the aborted session"
        await fresh.close()

    run(scenario())
    doc = session_by_nonce("e2e-abort-marker")
    assert doc["status"] == "aborted" and doc["abort_reason"] == "all human participants left"
    assert "result" not in doc and not [r for r in doc["results"] if r["kind"] == "final"]
    ended = doc["ended_at"]["$date"]  # ISO-8601 strings compare chronologically
    late_ai = [m["text"] for m in doc["messages"] if m["ts"]["$date"] > ended]
    assert not late_ai, f"AI kept talking after the abort: {late_ai}"


def test_all_humans_leave_but_one_returns_in_time(cfg):
    """A reload/network blip shorter than the grace period must not abort."""
    cfg.set("two_humans_one_ai")

    async def scenario():
        a, b = await join(2)
        await a.wait_phase("chat", 1)
        token = a.token
        await close_all([a, b])
        await asyncio.sleep(1)               # shorter than the 3 s grace period
        back = await Client("back").connect(token=token)
        await play([back])
        check_finished_session([back], votes_per_round=2)  # b never voted
        await back.close()

    run(scenario())


def test_one_of_two_humans_leaves_session_continues(cfg):
    cfg.set("two_humans_one_ai")

    async def scenario():
        stay, leave = await join(2)
        await stay.wait_phase("chat", 1)
        await leave.close()
        await asyncio.sleep(3 + 1)           # past the grace period
        assert stay.state["phase"] != "aborted", "aborted although a human is still there"
        await play([stay])
        check_finished_session([stay], votes_per_round=2)
        await stay.close()

    run(scenario())


# ================================================================== 5. bad input
def test_bad_and_out_of_phase_input_is_ignored(cfg):
    cfg.set("one_human_two_ai")

    async def scenario():
        bad_first = await Client("bad").connect(join=False)
        await bad_first.send({"type": "chat", "text": "hi"})
        await bad_first.wait(lambda: bad_first.close_code is not None, 5, "close")
        assert bad_first.close_code == 4001, "a non-join first message must be refused"

        h = await Client("h").connect()
        await h.chat("e2e-in-waiting-room")                         # chat before start
        await h.wait_phase("chat", 1)
        await h.vote("A")                                           # vote during chat
        await h.send("{this is not json")                           # malformed frame
        await h.send({"type": "bogus"})                              # unknown type
        await h.send(["not", "an", "object"])                       # wrong shape
        await h.send({"type": "chat", "text": 12345})                # wrong text type
        await asyncio.sleep(0.6)
        await h.chat("   ")                                          # whitespace only
        await asyncio.sleep(0.6)
        await h.chat("<script>alert(1)</script> e2e-html")           # markup
        await asyncio.sleep(0.6)
        await h.chat("e2e-long " + "x" * 1000)                       # too long
        await asyncio.sleep(0.6)
        for i in range(10):                                          # flood
            await h.chat(f"e2e-flood-{i}")

        vote_state = await h.wait_phase("vote", 1)
        assert vote_state["my_vote"] is None, "vote during chat phase was accepted"
        assert h.close_code is None, "connection dropped after bad input"
        await h.chat("e2e-chat-during-vote")
        for bad in ["D", "a", "", None, 5, ["A"]]:
            await h.vote(bad)
        await asyncio.sleep(0.5)
        assert h.state["my_vote"] is None, f"invalid vote accepted: {h.state['my_vote']}"
        await h.vote("B")
        await h.wait(lambda: h.state["my_vote"] == "B", 5, "vote B to register")
        await h.vote("C")                                            # changing vote
        await h.wait_phase("results", 1, 20)
        await h.chat("e2e-chat-during-results")
        await h.vote("C")
        assert all(s["my_vote"] in (None, "B") for s in h.states if s["round"] == 1)
        await h.wait_phase("vote", 2, 30)
        await h.vote("A")
        await h.wait_phase("finished", None, 30)

        texts = [m["text"] for m in h.chats]
        joined = "\n".join(texts)
        for never in ["e2e-in-waiting-room", "e2e-chat-during-vote", "e2e-chat-during-results"]:
            assert never not in joined, f"'{never}' should have been rejected"
        assert not any(t.strip() == "" for t in texts), "empty message accepted"
        assert "<script>alert(1)</script> e2e-html" in texts, "markup must be passed through as plain text"
        long_msg = next(t for t in texts if t.startswith("e2e-long"))
        assert len(long_msg) == 300, f"long message length {len(long_msg)}"
        floods = [t for t in texts if t.startswith("e2e-flood")]
        assert 1 <= len(floods) <= 2, f"flood guard let {len(floods)} through"
        r1 = h.state["round_results"][0]["tally"]
        assert r1["B"] >= 1, "the valid vote (B) was not counted"
        assert sum(r1.values()) == 3, "invalid votes changed the count"
        assert_anonymous(h)
        await h.close()

    run(scenario())


# ================================================================== 6. configuration
def test_invalid_config_is_reported_not_crashing(cfg):
    async def try_join():
        c = await Client().connect()
        await c.wait(lambda: c.close_code is not None or c.welcome, 5, "outcome")
        await c.close()
        return c

    cfg.set("broken", conditions={"broken": ["human", "ai", "ai", "ai"]})
    c = run(try_join())
    assert c.errors and c.welcome is None, "a participant was seated with an invalid config"
    urllib.request.urlopen(BASE_URL + "/api/health", timeout=5)

    cfg.set("does_not_exist")
    c = run(try_join())
    assert c.errors and c.welcome is None

    cfg.raw("condition: [unclosed")
    c = run(try_join())
    assert c.errors and c.welcome is None

    cfg.set("one_human_two_ai")
    c = run(try_join())
    assert c.welcome is not None, "app did not recover after config was fixed"


def test_config_change_applies_to_next_session(cfg):
    cfg.set("three_humans")

    async def first():
        h = await Client().connect()       # waiting room of a 3-human session...
        await asyncio.sleep(1)
        await h.close()                     # ...that is abandoned

    run(first())
    cfg.set("one_human_two_ai")

    async def second():
        h = await Client().connect()
        await h.wait_phase("starting", None, 15)  # only possible with 2 AI seats
        await h.close()

    run(second())


def test_single_round_session(cfg):
    cfg.set("three_humans", experiment={"rounds": 1})

    async def scenario():
        clients = await join(3)
        await play(clients, rounds=1, plan={1: {"A": "B", "B": "B", "C": "A"}})
        final = check_finished_session(clients, rounds=1)
        assert final["final"]["winner"] == "B"
        assert not any(c.saw("results") for c in clients), "no between-round pop-up with 1 round"
        await close_all(clients)

    run(scenario())


def test_three_rounds_tiebreak_sums_all_rounds(cfg):
    cfg.set("three_humans", experiment={"rounds": 3})
    plan = {1: {"A": "A", "B": "A", "C": "B"},           # A2 B1
            2: {"A": "A", "B": "B", "C": "C"},           # A1 B1 C1
            3: {"A": "A", "B": "B", "C": "C"}}           # tie -> totals A4 B3 C2

    async def scenario():
        clients = await join(3)
        await play(clients, rounds=3, plan=plan)
        final = check_finished_session(clients, rounds=3)
        assert final["final"]["method"] == "total_votes_tiebreak"
        assert final["final"]["winner"] == "A"
        assert final["final"]["totals_of_tied"] == {"A": 4, "B": 3, "C": 2}
        await close_all(clients)

    run(scenario(), timeout=300)


def test_env_override_and_admin_export(cfg):
    """EXPERIMENT_CONDITION overrides config.yml; admin export needs the token."""
    cfg.set("one_human_two_ai")
    compose("up", "-d", "backend", env={"EXPERIMENT_CONDITION": "three_humans", "ADMIN_TOKEN": "e2e-secret"})
    wait_healthy()
    try:
        async def scenario():
            h = await Client().connect()
            await h.wait(lambda: h.state is not None, 5, "state")
            before = h.occupied()
            await asyncio.sleep(5)          # AI seats would have filled by now (join delay 1-2 s)
            assert h.occupied() == before and h.state["phase"] == "waiting", \
                "a seat filled by itself despite the three_humans override"
            await h.close()

        run(scenario())

        def get(token=None):
            req = urllib.request.Request(BASE_URL + "/api/admin/sessions")
            if token:
                req.add_header("X-Admin-Token", token)
            try:
                return urllib.request.urlopen(req, timeout=5).status
            except urllib.error.HTTPError as e:
                return e.code

        assert get() == 404 and get("wrong") == 404
        assert get("e2e-secret") == 200
    finally:
        compose("up", "-d", "backend", env={"EXPERIMENT_CONDITION": "", "ADMIN_TOKEN": ""})
        wait_healthy()

    req = urllib.request.Request(BASE_URL + "/api/admin/sessions", headers={"X-Admin-Token": "e2e-secret"})
    with pytest.raises(urllib.error.HTTPError):
        urllib.request.urlopen(req, timeout=5)  # disabled again without ADMIN_TOKEN


# ================================================================== 7. robustness
def test_session_survives_database_outage(cfg):
    cfg.set("one_human_two_ai")

    async def scenario():
        h = await Client().connect()
        await h.wait_phase("chat", 1, 20)
        t0 = time.time()
        await asyncio.to_thread(compose, "stop", "mongo")
        try:
            nonce_holder["nonce"] = await play([h])
            elapsed = time.time() - t0
            check_finished_session([h])
            # 2 chat + 2 vote + 1 results phase ~= 31 s; allow some slack
            assert elapsed < 45, f"session stalled for {elapsed:.0f}s while the database was down"
        finally:
            await asyncio.to_thread(compose, "start", "mongo")
        await h.close()
        return nonce_holder

    nonce_holder = {}
    try:
        run(scenario(), timeout=300)
    finally:
        compose("start", "mongo")
    # writes made while the database was down are retried and land afterwards
    deadline = time.time() + 90
    while True:
        try:
            doc = session_by_nonce(nonce_holder["nonce"])
            if doc and doc.get("status") == "finished" and len(doc["votes"]) == 6:
                break
        except Exception:  # noqa: BLE001 - mongo still starting
            doc = None
        assert time.time() < deadline, f"data from the outage never reached the database: {doc}"
        time.sleep(3)
