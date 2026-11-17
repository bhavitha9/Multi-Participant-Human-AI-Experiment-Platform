import random

from app.agents import is_repeat
from app.responder_rules import RulesResponder


def test_identical_message_is_a_repeat():
    assert is_repeat("yeah agree with that", ["hi all", "yeah agree with that"])


def test_repeat_ignores_case_and_punctuation():
    assert is_repeat("Yeah, agree with that!", ["yeah agree with that"])


def test_near_identical_message_is_a_repeat():
    assert is_repeat("i dont mind doing it but happy to let someone else",
                     ["i don't mind doing it but happy to let somebody else"])


def test_different_message_is_not_a_repeat():
    assert not is_repeat("what does the decision maker actually do", ["hi all", "fair point"])


def test_empty_message_counts_as_repeat():
    assert is_repeat("...", [])


def test_rules_responder_never_repeats_within_a_session():
    rng = random.Random(7)
    bot = RulesResponder(rng)
    msgs = []
    for i in range(60):
        sender = "A" if i % 2 else "B"
        text = bot.chat(sender, msgs, 1 + (i >= 30), [{"round": 1, "tally": {"A": 1, "B": 1, "C": 1}}] if i >= 30 else [],
                        30, is_repeat)
        if text:
            assert not is_repeat(text, [m["text"] for m in msgs]), f"repeated: {text!r}"
            msgs.append({"sender": sender, "text": text, "round": 1 + (i >= 30)})
    assert len(msgs) > 5
