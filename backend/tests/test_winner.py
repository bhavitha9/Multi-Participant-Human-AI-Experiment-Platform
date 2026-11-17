import random

import pytest

from app.winner import (
    METHOD_FINAL_ROUND,
    METHOD_RANDOM,
    METHOD_TOTAL_VOTES,
    compute_winner,
    tally_votes,
)


def T(a=0, b=0, c=0):
    return {"A": a, "B": b, "C": c}


# --- tally ---------------------------------------------------------------
def test_tally_counts_votes_and_ignores_missing_or_invalid():
    votes = {"A": "B", "B": "B", "C": None}
    assert tally_votes(votes) == T(0, 2, 0)
    assert tally_votes({"A": "Z", "B": "C"}) == T(0, 0, 1)


# --- Case 1: round 2 decides ----------------------------------------------
def test_round2_majority_wins():
    res = compute_winner([T(3, 0, 0), T(1, 2, 0)])
    assert res.winner == "B"
    assert res.method == METHOD_FINAL_ROUND


def test_round2_majority_wins_even_if_round1_strongly_favoured_someone_else():
    # A has more total votes (3+1=4 vs 2) but round 2 alone decides.
    res = compute_winner([T(3, 0, 0), T(1, 2, 0)])
    assert res.winner == "B"


def test_round2_single_vote_beats_zero_votes():
    # Two players abstained; one vote is still a strict majority of the tally.
    res = compute_winner([T(0, 0, 0), T(0, 0, 1)])
    assert res.winner == "C"
    assert res.method == METHOD_FINAL_ROUND


# --- Case 2: round-2 tie broken by totals among tied players only -----------
def test_round2_tie_broken_by_total_votes():
    # Round 2: A=1, B=1, C=1 (three-way tie). Totals: A=1+2=3, B=1+1=2, C=1+0=1
    res = compute_winner([T(2, 1, 0), T(1, 1, 1)])
    assert res.winner == "A"
    assert res.method == METHOD_TOTAL_VOTES
    assert res.tied_after_final_round == ["A", "B", "C"]
    assert res.totals_of_tied == {"A": 3, "B": 2, "C": 1}


def test_tiebreak_only_considers_players_tied_in_round2():
    # Round 2: A and B tie at 1; C has 0 but dominated round 1.
    # C must NOT be considered. Totals among tied: A=1+0=1, B=1+1=2 -> B wins.
    res = compute_winner([T(0, 1, 2), T(1, 1, 0)])
    assert res.winner == "B"
    assert res.method == METHOD_TOTAL_VOTES
    assert res.tied_after_final_round == ["A", "B"]
    assert "C" not in res.totals_of_tied


# --- Case 3: still tied -> random among the still-tied ---------------------
def test_still_tied_picks_randomly_among_tied_players():
    # Round 2: A=1, B=1, C=1. Round 1: A=1, B=1, C=1. Totals all 2 -> random.
    tallies = [T(1, 1, 1), T(1, 1, 1)]
    winners = {compute_winner(tallies, rng=random.Random(seed)).winner for seed in range(200)}
    assert winners == {"A", "B", "C"}  # every tied player can win
    res = compute_winner(tallies, rng=random.Random(0))
    assert res.method == METHOD_RANDOM
    assert res.tied_after_totals == ["A", "B", "C"]


def test_random_tiebreak_never_picks_a_player_eliminated_earlier():
    # Round 2: A=1, B=1, C=0 -> tied A,B. Round 1: A=1, B=1, C=1. Totals A=2, B=2.
    # C is excluded at rule 2, so random must choose only A or B.
    tallies = [T(1, 1, 1), T(1, 1, 0)]
    winners = {compute_winner(tallies, rng=random.Random(seed)).winner for seed in range(200)}
    assert winners == {"A", "B"}
    assert compute_winner(tallies, rng=random.Random(1)).method == METHOD_RANDOM


def test_random_tiebreak_uses_supplied_rng():
    class FixedRng:
        def choice(self, seq):
            return seq[-1]

    res = compute_winner([T(1, 1, 1), T(1, 1, 0)], rng=FixedRng())
    assert res.winner == "B"


def test_everyone_abstains_is_full_random_tie():
    res = compute_winner([T(0, 0, 0), T(0, 0, 0)], rng=random.Random(3))
    assert res.method == METHOD_RANDOM
    assert res.winner in {"A", "B", "C"}


def test_requires_at_least_one_round():
    with pytest.raises(ValueError):
        compute_winner([])
