"""The consult gap-closer: candidates are ENGINE-priced, never self-priced."""

from __future__ import annotations

from unittest.mock import MagicMock

from trade_scout.consult import _enumerate_candidates, _reprice_and_rank
from trade_scout.providers.base import LeagueFormat, SourceSnapshot

FMT = LeagueFormat(superflex=True)

PLAYERS_MAP = {
    "1": {"first_name": "Target", "last_name": "Player", "position": "WR"},
    "2": {"first_name": "Big", "last_name": "Star", "position": "WR"},
    "3": {"first_name": "Fair", "last_name": "Return", "position": "RB"},
    "4": {"first_name": "Small", "last_name": "Piece", "position": "RB"},
}

LEAGUE = {"num_qbs": 2, "num_teams": 12, "ppr": 1,
          "format_str": "SF / 12-team / PPR"}

SNAPSHOTS = {
    "a": SourceSnapshot(
        source="a", display_name="A", as_of="2026-08-24", fmt=FMT,
        join="sleeper_id",
        players={"1": {"value": 5000}, "2": {"value": 7000},
                 "3": {"value": 5100}, "4": {"value": 1500}}),
}


def reprice(cands):
    from trade_scout.name_matcher import NameMatcher
    return _reprice_and_rank(
        cands, league=LEAGUE, players_map=PLAYERS_MAP, sleeper=MagicMock(),
        snapshots=SNAPSHOTS, source_errors={},
        matcher=NameMatcher(PLAYERS_MAP), flock_data={})


class TestEnumerateCandidates:
    ROSTER = [
        {"name": "Big Star", "value": 7000},
        {"name": "Fair Return", "value": 5100},
        {"name": "Small Piece", "value": 1500},
    ]

    def test_singles_and_pairs_inside_the_band(self):
        cands = _enumerate_candidates(["Target Player"], 5000, self.ROSTER)
        gets = [tuple(c["brandon_gets"]) for c in cands]
        assert ("Fair Return",) in gets                     # 5100 in band
        assert ("Fair Return", "Small Piece") in gets       # 6600 in band
        assert ("Big Star", "Fair Return") not in gets      # 12100 way out

    def test_no_llm_numbers_anywhere(self):
        for c in _enumerate_candidates(["Target Player"], 5000, self.ROSTER):
            assert "value_delta" not in c

    def test_zero_target_value_yields_nothing(self):
        assert _enumerate_candidates(["X"], 0, self.ROSTER) == []


class TestRepriceAndRank:
    def test_deltas_come_from_the_engine_not_the_candidate(self):
        # The candidate LIES about its delta; the pipeline must ignore it.
        cands = [{"brandon_gives": ["Target Player"],
                  "brandon_gets": ["Fair Return"],
                  "value_delta": 999999, "rationale": "r"}]
        out = reprice(cands)
        assert len(out) == 1
        assert out[0]["evaluation"]["delta"] == 100   # 5100 - 5000, measured
        assert "value_delta" not in out[0]

    def test_unmatched_assets_kill_a_candidate(self):
        cands = [
            {"brandon_gives": ["Target Player"],
             "brandon_gets": ["Nonexistent Guy"], "rationale": "r"},
            {"brandon_gives": ["Target Player"],
             "brandon_gets": ["Fair Return"], "rationale": "r"},
        ]
        out = reprice(cands)
        assert [o["brandon_gets"] for o in out] == [["Fair Return"]]

    def test_labels_follow_measured_deltas(self):
        cands = [
            {"brandon_gives": ["Target Player"],
             "brandon_gets": ["Fair Return"], "rationale": "even-ish"},   # +100
            {"brandon_gives": ["Target Player"],
             "brandon_gets": ["Big Star"], "rationale": "big win"},       # +2000
            {"brandon_gives": ["Target Player"],
             "brandon_gets": ["Fair Return", "Small Piece"],
             "rationale": "package"},                                     # +1600
        ]
        out = reprice(cands)
        by_label = {o["label"]: o for o in out}
        assert by_label["STRETCH"]["evaluation"]["delta"] == 2000
        assert by_label["FAIR DEAL"]["evaluation"]["delta"] == 100
        assert by_label["BEST DEAL"]["evaluation"]["delta"] == 1600

    def test_wild_losers_are_dropped(self):
        cands = [
            {"brandon_gives": ["Target Player"],
             "brandon_gets": ["Small Piece"], "rationale": "donation"},  # -70%
            {"brandon_gives": ["Target Player"],
             "brandon_gets": ["Fair Return"], "rationale": "fine"},
        ]
        out = reprice(cands)
        assert all(o["brandon_gets"] != ["Small Piece"] for o in out)

    def test_duplicate_candidates_collapse(self):
        c = {"brandon_gives": ["Target Player"],
             "brandon_gets": ["Fair Return"], "rationale": "r"}
        assert len(reprice([c, dict(c)])) == 1
