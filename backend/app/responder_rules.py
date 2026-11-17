"""Offline, context-aware responder for AI seats (no API key needed).

It reads the actual chat and replies to it: who volunteered, who nominated
whom, questions aimed at this player, greetings, "are you a bot?", disagreement,
and last round's vote results. Each AI seat keeps its own state (a disposition,
who it currently supports, what it has already said), and its vote follows the
opinion it expressed in the chat.

It is template-based, so it will never be as natural as a real LLM; use
`ai.provider: anthropic` with an API key for real experiments.
"""
from __future__ import annotations

import random
import re
from dataclasses import dataclass, field
from typing import Optional

PLAYERS = ("A", "B", "C")
DISPOSITIONS = ("keen", "easygoing", "careful")

# ---------------------------------------------------------------- detection
_GREETING = re.compile(r"^\s*(hi+|hey+|hello|hiya|yo|sup|howdy|good (morning|afternoon|evening))\b")
_BOT = re.compile(r"\b(bots?|robots?|ai|a\.i\.?|chat ?gpt|gpt|llm|human|humans|real (person|people)|computer|automated)\b")
_SELF_NOM = [
    re.compile(r"\bi('ll| will| can| could|'d| would)( happily| gladly| definitely)? (do|be|take) (it|that|this|the job|the role|dm|decision maker|the decision maker)\b"),
    re.compile(r"\bi (really )?(want|wanna|would like|'d like) (to )?(do it|be (the )?(decision maker|dm|it)|take it)\b"),
    re.compile(r"\b(pick|vote( for)?|choose|elect) me\b"),
    re.compile(r"\bi volunteer\b"),
    re.compile(r"\bi('m| am) (happy|willing|up for|keen|ok|okay|fine)( to)? (do(ing)? it|be(ing)? (the )?(decision maker|dm|it)|it)\b"),
    re.compile(r"\bi('d| would) be (good|great|decent|fine) (at it|as (the )?(decision maker|dm))\b"),
    re.compile(r"\bi('d| would|'m| am)( also)? (be )?up for (it|doing it|being)\b"),
    re.compile(r"\bi (could|can) (also )?do it\b"),
]
_DECLINE = re.compile(r"\b(i (don'?t|do not|dont) (really )?want (it|to)|not me|i'?d rather not|i pass|count me out|i'?m not (really )?(interested|keen|good at)|not for me)\b")
_ASK_ROLE = re.compile(r"what (does|do|will|would|is) (the )?(decision maker|dm|they|it|that)\b|what('?s| is) the (decision maker|role|job|point)")
_ASK_HOW = re.compile(r"how (do|should|will|are|can) we (decide|choose|pick|do this|vote|go about)|how does (this|it) work")
_ASK_OPINION = re.compile(
    r"(what|who) (do|would|did) (you|u|y'?all|everyone|you guys|you all|others) (think|pick|vote|want|prefer|reckon)"
    r"|\bthoughts\b|any(one)? (ideas|thoughts|opinions)|who (should|do we|are we|would) (it be|be it|pick|choose|vote|do it|go)"
    r"|what about you|and you\b|\byou\?"
)
_WHY = re.compile(r"\bwhy\b")
_AGREE = re.compile(r"^\s*(yeah|yes|yep|yup|agreed|agree|true|exactly|same|ok|okay|sure|sounds good|fair|makes sense|right|cool|nice|good idea|\+1)\b")
_DISAGREE = re.compile(r"^\s*(no|nah|nope)\b|\b(disagree|not sure about (that|this|him|her|them)|i don'?t think so)\b")
_REASON = re.compile(r"\b(because|since|cause|cuz|bc|as i|reason)\b")
_SUPPORT_WORDS = re.compile(r"\b(vote|voting|pick|choose|go with|should|trust|support|leaning|back|think|for|like|prefer|be good|be great)\b")
_LOWER_LETTER = re.compile(r"(?<![\w'])([bc])(?![\w'])")
_PLAYER_X = re.compile(r"\bplayer\s*([abc])\b")
_UPPER_LETTER = re.compile(r"\b([ABC])\b")
_A_ARTICLE = re.compile(r"^A (lot|few|good|bit|little|great|decision|quick|fair|nice|real)\b")


def mentioned_players(raw: str) -> list[str]:
    """Player letters named in a message, in order of appearance (deduplicated)."""
    found: list[tuple[int, str]] = []
    low = raw.lower()
    found += [(m.start(), m.group(1).upper()) for m in _PLAYER_X.finditer(low)]
    found += [(m.start(), m.group(1).upper()) for m in _LOWER_LETTER.finditer(low)]
    for m in _UPPER_LETTER.finditer(raw):
        if m.group(1) == "A" and m.start() == 0 and _A_ARTICLE.match(raw):
            continue
        found.append((m.start(), m.group(1)))
    out: list[str] = []
    for _, p in sorted(found):
        if p not in out:
            out.append(p)
    return out


@dataclass
class Signals:
    sender: str
    greeting: bool = False
    bot_q: bool = False
    question: bool = False
    self_nom: bool = False
    declines: bool = False
    mentions: list[str] = field(default_factory=list)
    nominates: Optional[str] = None
    addressed: list[str] = field(default_factory=list)
    asks_role: bool = False
    asks_how: bool = False
    asks_opinion: bool = False
    why: bool = False
    agree: bool = False
    disagree: bool = False
    reason: bool = False


def analyse(msg: dict) -> Signals:
    raw = msg["text"]
    t = raw.lower().strip()
    s = Signals(sender=msg["sender"])
    s.greeting = bool(_GREETING.search(t))
    s.bot_q = bool(_BOT.search(t)) and ("?" in t or bool(re.search(r"\b(are|is|who'?s|anyone|any of)\b", t)))
    s.question = "?" in t or bool(re.match(r"^(who|what|why|how|should|do|does|are|is|can|would|will|any)\b", t))
    s.self_nom = any(p.search(t) for p in _SELF_NOM)
    s.declines = bool(_DECLINE.search(t))
    s.mentions = [p for p in mentioned_players(raw) if p != s.sender]
    s.asks_role = bool(_ASK_ROLE.search(t))
    s.asks_how = bool(_ASK_HOW.search(t))
    s.asks_opinion = bool(_ASK_OPINION.search(t))
    s.why = bool(_WHY.search(t))
    s.agree = bool(_AGREE.search(t))
    s.disagree = bool(_DISAGREE.search(t))
    s.reason = bool(_REASON.search(t))
    if s.mentions:
        if s.question and not _SUPPORT_WORDS.search(t.split("?")[0]) or s.asks_opinion or s.why:
            # "player b what do you think?" -> a question to B, not a nomination
            s.addressed = list(s.mentions)
        elif _SUPPORT_WORDS.search(t) and not s.disagree:
            s.nominates = s.mentions[0]
        else:
            s.addressed = list(s.mentions)
    return s


# ---------------------------------------------------------------- templates
T: dict[str, list[str]] = {
    "open_r1": [
        "hi all", "hey everyone", "hello", "hey, so who wants to be decision maker",
        "hi! how do we want to decide this", "hey hey",
    ],
    "greet_back": [
        "hey {s}", "hi {s}", "hey!", "hello {s}", "hi :)",
    ],
    "open_r2_top": [
        "so {top} got the most votes last time, are we sticking with that?",
        "{top} was ahead last round, anyone want to change?",
        "ok {top} is leading, i'm fine with that",
    ],
    "open_r2_me_top": [
        "oh thanks for the votes haha", "didn't expect to get votes lol, happy to do it though",
    ],
    "open_r2_tie": [
        "it was a tie last round lol, we need to sort that out",
        "so nobody really won last round, should we agree on someone?",
    ],
    "bot": [
        "lol no", "haha no, just here for the study like everyone", "pretty sure we're all people here",
        "lol why, do i type weird", "nope, just a person on a laptop", "if i was a bot i'd type faster haha",
        "no lol, are you?",
    ],
    "support_other_vol": [
        "ok {s} wants it, i'm fine with that", "sounds good {s}, i'd vote for you",
        "{s} volunteering is good enough for me", "nice, at least someone wants to do it lol",
        "cool, i'm happy with {s}",
    ],
    "question_vol": [
        "why do you want to do it {s}?", "what would you do as decision maker {s}?",
        "{s} have you done anything like this before?", "ok {s}, how would you make the decisions?",
    ],
    "compete_vol": [
        "i'd be up for it too tbh", "i wouldn't mind doing it either", "i could also do it, but happy to vote",
    ],
    "thanks_nominated": [
        "oh thanks {s}, i could do it", "haha thanks, i'm up for it if you both are",
        "thanks {s}! i don't mind doing it", "oh ok, sure i can do it",
    ],
    "decline_nominated": [
        "thanks {s} but i'd rather not tbh", "haha i'm not sure i'm the best pick",
        "oh no, i'd honestly prefer someone else did it", "that's nice of you {s} but i'll pass",
        "hmm i don't really want it tbh",
    ],
    "agree_nomination": [
        "yeah {x} seems like a good choice", "i'd go with {x} too", "{x} works for me",
        "agree, {x} makes sense",
    ],
    "ask_nominee": [
        "{x} would you want to do it?", "what do you think {x}, are you up for it?",
        "{x}? you up for it?",
    ],
    "declined_reply": [
        "fair enough {s}", "ok no worries {s}", "that's fine, someone else then",
        "ok so not {s} then", "no problem, one of us can do it",
    ],
    "state_support": [
        "i'm leaning towards {c}", "probably {c} for me", "i'd say {c} honestly",
        "{c} so far", "i think {c} would be good",
    ],
    "state_support_self": [
        "honestly i'd be up for it", "i could do it if that's ok with you two", "i don't mind being decision maker",
    ],
    "state_undecided": [
        "not sure yet tbh", "i don't have a strong opinion yet", "still deciding, what about you two?",
    ],
    "reason_support": [
        "{c} has been the clearest so far", "{c} seems pretty level headed", "{c} actually explained why, i like that",
        "{c} seems keen and that matters", "seems like {c} would take it seriously",
    ],
    "reason_self": [
        "i'm decent at making calls quickly", "i don't mind the responsibility", "i'd just try to be fair to everyone",
    ],
    "role": [
        "i think they just make the choices for the group in the next part",
        "from the instructions it sounds like they decide for all of us",
        "they make the calls for the group next round i guess",
    ],
    "how": [
        "maybe whoever actually wants to do it?", "we could each say if we want it and go from there",
        "i'd say whoever explains their reasoning best", "let's just see who volunteers",
    ],
    "ack": ["yeah", "true", "fair", "makes sense", "ok cool", "same", "agreed"],
    "disagree_reply": [
        "fair, who would you pick then {s}?", "ok what's your pick then {s}?", "hm ok, who do you think {s}?",
    ],
    "good_point": [
        "good point {s}", "that's fair {s}", "yeah that makes sense {s}",
    ],
    "nudge_quiet": [
        "{x} what do you think?", "{x} you've been quiet, any thoughts?", "{x} who would you pick?",
    ],
    "hurry": [
        "running out of time, i'm going with {c}", "ok time's nearly up, {c} for me",
    ],
    "hurry_self": [
        "not much time left, i'm happy to do it if you vote for me",
        "ok nearly out of time, i'll put myself forward",
    ],
}


def name(p: str, rng: random.Random) -> str:
    return rng.choice([f"player {p.lower()}", f"player {p.lower()}", p])


@dataclass
class Mind:
    disposition: str
    used: set[str] = field(default_factory=set)
    last_seen: int = 0
    greeted: bool = False
    opened_round: int = 0
    announced: Optional[str] = None  # candidate we last told the group about
    pending: list = field(default_factory=list)  # things aimed at us, not yet answered


class RulesResponder:
    """Shared by all AI seats of one session; keeps separate state per seat."""

    def __init__(self, rng: random.Random):
        self.rng = rng
        self.minds: dict[str, Mind] = {}
        self.used: set[str] = set()  # wordings already used by any AI seat this session

    def mind(self, me: str) -> Mind:
        if me not in self.minds:
            self.minds[me] = Mind(disposition=self.rng.choice(DISPOSITIONS))
        return self.minds[me]

    # ---------------------------------------------------------- opinions
    def candidate(self, me: str, messages: list[dict], round_results: list[dict]) -> Optional[str]:
        """Who this seat currently supports, based on what was said and voted."""
        mind = self.mind(me)
        score = {p: 0.0 for p in PLAYERS}
        evidence = False
        for m in messages:
            s = analyse(m)
            if s.self_nom:
                score[s.sender] += 2.0; evidence = True
            if s.declines:
                score[s.sender] -= 3.0; evidence = True
            if s.nominates:
                score[s.nominates] += 1.5; evidence = True
            if s.reason:
                score[s.sender] += 0.5
        for r in round_results:
            for p, n in r["tally"].items():
                score[p] += 0.8 * n
                evidence = evidence or n > 0
        if mind.disposition == "keen":
            score[me] += 2.0; evidence = True
        elif mind.disposition == "easygoing":
            score[me] -= 1.0
        if not evidence:
            return None
        best = max(score.values())
        tied = sorted(p for p, v in score.items() if v == best)
        if mind.announced in tied:
            return mind.announced  # stay consistent with what we said
        return self.rng.choice(tied)

    def vote(self, me: str, messages: list[dict], round_results: list[dict]) -> str:
        c = self.candidate(me, messages, round_results)
        if c is None:
            c = self.rng.choice([p for p in PLAYERS if p != me])
        return c

    # ---------------------------------------------------------- talking
    def _say(self, me: str, key: str, messages: list[dict], is_repeat, **kw) -> Optional[str]:
        mind = self.mind(me)
        options = [t for t in T[key] if t not in mind.used and t not in self.used]
        self.rng.shuffle(options)
        previous = [m["text"] for m in messages]
        for tmpl in options:
            text = tmpl.format(**kw)
            if not is_repeat(text, previous):
                mind.used.add(tmpl)
                self.used.add(tmpl)
                return text
        return None

    def chat(
        self,
        me: str,
        messages: list[dict],
        round_no: int,
        round_results: list[dict],
        seconds_left: float,
        is_repeat,
    ) -> Optional[str]:
        rng = self.rng
        mind = self.mind(me)
        new = [m for m in messages[mind.last_seen:] if m["sender"] != me and m["round"] == round_no]
        mind.last_seen = len(messages)
        cand = self.candidate(me, messages, round_results)
        say = lambda key, **kw: self._say(me, key, messages, is_repeat, **kw)  # noqa: E731

        def support_line() -> Optional[str]:
            if cand is None:
                return say("state_undecided")
            mind.announced = cand
            if cand == me:
                return say("state_support_self")
            return say("state_support", c=name(cand, rng))

        # Start of a round: open, or react to last round's results.
        if mind.opened_round != round_no:
            mind.pending = []  # don't answer last round's questions in a new round
        if mind.opened_round != round_no and not [m for m in new if m["round"] == round_no]:
            mind.opened_round = round_no
            if round_no == 1:
                mind.greeted = True
                return say("open_r1")
            last = round_results[-1]["tally"] if round_results else {}
            if last:
                best = max(last.values())
                tops = [p for p, n in last.items() if n == best]
                if len(tops) > 1:
                    return say("open_r2_tie")
                if tops[0] == me:
                    return say("open_r2_me_top")
                return say("open_r2_top", top=name(tops[0], rng))
        mind.opened_round = round_no

        if not new:
            # Quiet moment: nudge things along occasionally.
            if seconds_left < 15 and cand:
                mind.announced = cand
                return say("hurry_self") if cand == me else say("hurry", c=name(cand, rng))
            if mind.announced is None and rng.random() < 0.6:
                return support_line()
            spoken = {m["sender"] for m in messages if m["round"] == round_no}
            quiet = [p for p in PLAYERS if p != me and p not in spoken]
            if quiet and rng.random() < 0.5:
                return say("nudge_quiet", x=name(rng.choice(quiet), rng))
            return None

        # Pick what to respond to: something aimed at me first (including things
        # that arrived while we were typing last time), then the latest message.
        sigs = [analyse(m) for m in new]
        aimed = mind.pending + [s for s in sigs if me in s.addressed or me == s.nominates or s.bot_q]
        is_aimed = bool(aimed)
        if aimed:
            sig = aimed.pop()
            mind.pending = aimed[-2:]
        else:
            sig = sigs[-1]
        spk = name(sig.sender, rng)

        # Questions or nominations aimed at someone else: usually let them answer.
        if not is_aimed and sig.addressed and me not in sig.addressed and rng.random() < 0.75:
            return None
        # A group question someone else already answered: often just let it be.
        if me not in sig.addressed and sig.sender != me and (sig.question or sig.why):
            idx = max(i for i, m in enumerate(messages) if m["sender"] == sig.sender)
            answered_by_other = any(m["sender"] not in (me, sig.sender) for m in messages[idx + 1:])
            if answered_by_other and rng.random() < 0.6:
                return None

        if sig.bot_q:
            answer = say("bot")
            asked = [p for p in mind.pending if me in p.addressed and not p.bot_q]
            if answer and asked:
                mind.pending = [p for p in mind.pending if p not in asked]
                extra = support_line()
                if extra:
                    return f"{answer}. {extra}"
            return answer
        if sig.nominates == me:
            if mind.disposition == "easygoing" and rng.random() < 0.5:
                return say("decline_nominated", s=spk)
            mind.announced = me
            return say("thanks_nominated", s=spk)
        if sig.why and (me in sig.addressed or not sig.mentions):
            if cand == me:
                return say("reason_self")
            if cand:
                return say("reason_support", c=name(cand, rng))
        if me in sig.addressed:
            if sig.why:
                if cand == me:
                    return say("reason_self")
                if cand:
                    return say("reason_support", c=name(cand, rng))
            return support_line()
        if sig.self_nom:
            if mind.disposition == "keen" and cand == me and rng.random() < 0.5:
                mind.announced = me
                return say("compete_vol")
            if mind.disposition == "careful" and rng.random() < 0.6:
                return say("question_vol", s=spk)
            mind.announced = sig.sender
            return say("support_other_vol", s=spk)
        if sig.nominates:
            x = name(sig.nominates, rng)
            if cand in (None, sig.nominates) or rng.random() < 0.4:
                if cand is None or cand == sig.nominates:
                    mind.announced = sig.nominates
                return say("agree_nomination", x=x)
            return say("ask_nominee", x=x)
        if sig.declines:
            return say("declined_reply", s=spk)
        if sig.greeting and not mind.greeted:
            mind.greeted = True
            return say("greet_back", s=spk)
        if sig.asks_role:
            return say("role")
        if sig.asks_how:
            return say("how")
        if sig.asks_opinion or (sig.question and not sig.mentions):
            return support_line()
        if sig.disagree:
            return say("disagree_reply", s=spk)
        if sig.reason and rng.random() < 0.6:
            return say("good_point", s=spk)
        if sig.agree:
            return say("ack") if rng.random() < 0.3 else None
        if sig.greeting:
            return None
        if mind.announced is None:
            return support_line()
        return say("ack") if rng.random() < 0.3 else None
