"""Winner calculation for the Decision Maker election.

Pure functions with no I/O so they can be unit-tested in isolation.

Rules (implemented exactly as specified):
1. The final round decides: the player with the most final-round votes wins.
2. If the final round is tied: among ONLY the tied players, sum their votes
   from all rounds (round 1 + round 2). The highest total wins.
3. If still tied: pick randomly among the (still) tied players.
"""
from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

PLAYERS: tuple[str, ...] = ("A", "B", "C")

METHOD_FINAL_ROUND = "final_round_majority"
METHOD_TOTAL_VOTES = "total_votes_tiebreak"
METHOD_RANDOM = "random_tiebreak"


def tally_votes(
    votes: Mapping[str, Optional[str]], players: Sequence[str] = PLAYERS
) -> dict[str, int]:
    """Count votes. `votes` maps voter -> target (None = abstained/no vote).

    Votes for unknown targets are ignored.
    """
    counts = {p: 0 for p in players}
    for target in votes.values():
        if target in counts:
            counts[target] += 1
    return counts


@dataclass
class WinnerResult:
    winner: str
    method: str
    final_round_tally: dict[str, int]
    tied_after_final_round: list[str] = field(default_factory=list)
    totals_of_tied: dict[str, int] = field(default_factory=dict)
    tied_after_totals: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "winner": self.winner,
            "method": self.method,
            "final_round_tally": self.final_round_tally,
            "tied_after_final_round": self.tied_after_final_round,
            "totals_of_tied": self.totals_of_tied,
            "tied_after_totals": self.tied_after_totals,
        }


def _leaders(scores: Mapping[str, int]) -> list[str]:
    best = max(scores.values())
    return sorted(p for p, s in scores.items() if s == best)


def compute_winner(
    round_tallies: Sequence[Mapping[str, int]],
    players: Sequence[str] = PLAYERS,
    rng: Optional[random.Random] = None,
) -> WinnerResult:
    """Determine the Decision Maker from per-round vote tallies.

    `round_tallies` is ordered by round; the last entry is the deciding round.
    """
    if not round_tallies:
        raise ValueError("at least one round of votes is required")
    rng = rng or random.Random()

    final = {p: int(round_tallies[-1].get(p, 0)) for p in players}
    tied = _leaders(final)
    if len(tied) == 1:
        return WinnerResult(tied[0], METHOD_FINAL_ROUND, final)

    # Rule 2: only the tied players are considered; sum their votes over all rounds.
    totals = {p: sum(int(t.get(p, 0)) for t in round_tallies) for p in tied}
    still_tied = _leaders(totals)
    if len(still_tied) == 1:
        return WinnerResult(
            still_tied[0], METHOD_TOTAL_VOTES, final, tied, totals, still_tied
        )

    # Rule 3: random choice among those still tied.
    winner = rng.choice(still_tied)
    return WinnerResult(winner, METHOD_RANDOM, final, tied, totals, still_tied)
