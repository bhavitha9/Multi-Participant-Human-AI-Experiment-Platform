"""The offline responder must react to what was actually said."""
import random
import re

import pytest

from app.agents import is_repeat
from app.responder_rules import T, RulesResponder, analyse, mentioned_players


def msg(sender, text, round_no=1):
    return {"sender": sender, "text": text, "round": round_no}


def came_from(reply, *keys):
    """True if `reply` was produced by one of the template groups `keys`."""
    for key in keys:
        for tmpl in T[key]:
            pattern = "^" + re.sub(r"\\\{\w+\\\}", ".+", re.escape(tmpl)) + "$"
            if re.match(pattern, reply):
                return True
    return False


def responder(me, disposition, seed=1):
    r = RulesResponder(random.Random(seed))
    r.mind(me).disposition = disposition
    r.mind(me).opened_round = 1  # skip the round opener
    r.mind(me).greeted = True
    return r


def reply(r, me, messages, round_results=(), seconds_left=40):
    return r.chat(me, messages, max(m["round"] for m in messages), list(round_results), seconds_left, is_repeat)


# --- understanding messages ----------------------------------------------
@pytest.mark.parametrize("text,expected", [
    ("I think B should do it", ["B"]),
    ("player c?", ["C"]),
    ("what about b and c", ["B", "C"]),
    ("A lot of options here", []),
    ("A should do it", ["A"]),
    ("c'mon guys", []),
])
def test_mentioned_players(text, expected):
    assert mentioned_players(text) == expected


def test_detects_self_nomination():
    assert analyse(msg("A", "hey all, I could be decision maker if nobody minds")).self_nom
    assert analyse(msg("A", "pick me lol")).self_nom
    assert not analyse(msg("A", "who wants to do it?")).self_nom


def test_question_to_a_player_is_not_a_nomination():
    s = analyse(msg("A", "player b what do you think?"))
    assert s.addressed == ["B"] and s.nominates is None


def test_detects_nomination_of_someone_else():
    assert analyse(msg("A", "i'd vote for player c")).nominates == "C"


# --- replying to the conversation ------------------------------------------
def test_answers_a_question_aimed_at_it():
    r = responder("B", "careful")
    out = reply(r, "B", [msg("A", "I'll do it if nobody minds"), msg("C", "player b what do you think?")])
    assert came_from(out, "state_support", "state_support_self", "state_undecided")


def test_deflects_bot_question():
    r = responder("C", "easygoing")
    out = reply(r, "C", [msg("A", "wait are any of you bots?")])
    assert came_from(out, "bot")


def test_reacts_to_being_nominated():
    r = responder("C", "keen")
    out = reply(r, "C", [msg("A", "I think C should do it")])
    assert came_from(out, "thanks_nominated")


def test_reacts_to_volunteer_by_name():
    for seed in range(10):
        r = responder("B", "easygoing", seed)
        out = reply(r, "B", [msg("A", "I could be decision maker if nobody minds")])
        assert came_from(out, "support_other_vol")
        assert "a" in out.lower().split() or "player a" in out.lower() or "someone" in out


def test_explains_the_role_when_asked():
    r = responder("B", "careful")
    out = reply(r, "B", [msg("A", "what does the decision maker actually do?")])
    assert came_from(out, "role")


def test_usually_lets_the_addressed_player_answer():
    answered = 0
    for seed in range(40):
        r = responder("C", "careful", seed)
        if reply(r, "C", [msg("A", "player b what do you think?")]) is not None:
            answered += 1
    assert answered < 20


def test_round2_opener_mentions_last_round_leader():
    r = RulesResponder(random.Random(3))
    r.mind("B").disposition = "careful"
    r.mind("B").opened_round = 1
    out = r.chat("B", [msg("A", "hi", 1)], 2, [{"round": 1, "tally": {"A": 0, "B": 0, "C": 2}}], 60, is_repeat)
    assert came_from(out, "open_r2_top") and "c" in out.lower()


# --- votes follow the conversation -----------------------------------------
def test_vote_follows_volunteer_and_support():
    r = RulesResponder(random.Random(0))
    r.mind("C").disposition = "easygoing"
    chat = [msg("A", "I can do it, I'm fine being decision maker"), msg("B", "i'd vote for player a")]
    assert r.vote("C", chat, []) == "A"


def test_vote_avoids_player_who_declined():
    r = RulesResponder(random.Random(0))
    r.mind("C").disposition = "easygoing"
    chat = [msg("A", "not me, i'd rather not"), msg("B", "i'll do it")]
    assert r.vote("C", chat, []) == "B"


def test_answers_why_with_a_reason():
    r = responder("B", "easygoing")
    chat = [msg("A", "I'll do it"), msg("B", "ok player a wants it, i'm fine with that"), msg("A", "why do you think that?")]
    r.mind("B").last_seen = 2
    out = reply(r, "B", chat)
    assert came_from(out, "reason_support", "reason_self")


def test_question_that_arrived_while_typing_is_answered_later():
    r = responder("B", "careful")
    chat = [msg("A", "I could be decision maker if nobody minds")]
    reply(r, "B", chat)  # busy reacting to the volunteer
    chat += [msg("A", "player b what do you think?"), msg("A", "wait are any of you bots?")]
    out = reply(r, "B", chat)  # both the bot question and the direct question get answered
    head, _, tail = out.partition(". ")
    assert came_from(head, "bot")
    assert came_from(tail, "state_support", "state_support_self", "state_undecided")
    assert r.mind("B").pending == []


def test_offering_to_do_it_counts_as_self_nomination():
    assert analyse(msg("C", "i'd be up for it too tbh")).self_nom
    assert analyse(msg("C", "i could also do it, but happy to vote")).self_nom


def test_old_round_questions_are_not_answered_in_a_new_round():
    r = responder("B", "careful")
    r.mind("B").pending = [analyse(msg("A", "wait are any of you bots?"))]
    out = r.chat("B", [msg("A", "bots?", 1), msg("A", "so who are you voting for this time?", 2)], 2,
                 [{"round": 1, "tally": {"A": 1, "B": 1, "C": 1}}], 50, is_repeat)
    assert not came_from(out, "bot")


def test_two_ai_seats_never_use_the_same_wording():
    r = RulesResponder(random.Random(5))
    for me in ("B", "C"):
        r.mind(me).disposition = "easygoing"
        r.mind(me).opened_round = 1
        r.mind(me).greeted = True
    chat = [msg("A", "I'll do it")]
    b = reply(r, "B", chat)  # both draft before either posts
    c = reply(r, "C", chat)
    assert b and c and b != c


def test_bot_question_and_direct_question_answered_together():
    r = responder("B", "careful")
    chat = [msg("A", "I'll do it"), msg("A", "player b what do you think?"), msg("A", "are any of you bots?")]
    out = reply(r, "B", chat)
    head, _, tail = out.partition(". ")
    assert came_from(head, "bot") and came_from(tail, "state_support", "state_support_self", "state_undecided")
