"""The per-source evaluator: rows, scales, consensus, degradation."""

from __future__ import annotations

from unittest.mock import MagicMock, patch

import pytest

from trade_scout.config import AppConfig
from trade_scout.engine.evaluate import evaluate_trade, format_source_table
from trade_scout.providers.base import LeagueFormat, SourceSnapshot
from trade_scout.trade import TradeCommand
from trade_scout.values import ValuesError

FMT = LeagueFormat(superflex=True)

PLAYERS_MAP = {
    "4034": {"first_name": "Christian", "last_name": "McCaffrey",
             "full_name": "Christian McCaffrey", "position": "RB", "age": 30},
    "9509": {"first_name": "Bijan", "last_name": "Robinson",
             "full_name": "Bijan Robinson", "position": "RB", "age": 24},
    "6794": {"first_name": "Justin", "last_name": "Jefferson",
             "full_name": "Justin Jefferson", "position": "WR", "age": 27},
}

LEAGUE = {"num_qbs": 2, "num_teams": 12, "ppr": 1,
          "format_str": "SF / 12-team / PPR"}


def snap(key: str, values: dict[str, float], picks: dict[str, float] | None = None,
         stale: bool = False, as_of: str = "2026-08-24") -> SourceSnapshot:
    return SourceSnapshot(
        source=key, display_name=key.title(), as_of=as_of, fmt=FMT,
        join="sleeper_id",
        players={pid: {"value": v} for pid, v in values.items()},
        picks=picks or {}, stale=stale)


def run_eval(snapshots: dict, errors: dict | None = None,
             cfg: AppConfig | None = None,
             trade: TradeCommand | None = None,
             flock: dict | None = None) -> dict:
    trade = trade or TradeCommand(give=["Christian McCaffrey"],
                                  get=["Bijan Robinson"])
    with patch("trade_scout.engine.evaluate.get_snapshots",
               return_value=(snapshots, errors or {})), \
         patch("trade_scout.engine.evaluate._try_flock_rankings",
               return_value=flock or {}), \
         patch("trade_scout.engine.evaluate.build_roster_context",
               return_value=None):
        return evaluate_trade(trade, LEAGUE, PLAYERS_MAP, MagicMock(),
                              cfg=cfg or AppConfig())


# Three sources that deliberately disagree: a wins, b loses, c is inside the
# even band. The whole point of the layer is that all three stay visible.
THREE = {
    "a": snap("a", {"4034": 1000, "9509": 2000}),          # get wins big
    "b": snap("b", {"4034": 5000, "9509": 4000}),          # give wins
    "c": snap("c", {"4034": 3000, "9509": 3050}),          # within 3% -> even
}


class TestPerSourceRows:
    def test_each_source_prices_on_its_own_scale(self):
        result = run_eval(THREE)
        rows = result["sources"]
        assert rows["a"]["give_total"] == 1000
        assert rows["a"]["get_total"] == 2000
        assert rows["a"]["winner"] == "GET"
        assert rows["b"]["winner"] == "GIVE"
        assert rows["c"]["winner"] == "EVEN"

    def test_per_asset_breakdown_carries_raw_per_source_values(self):
        result = run_eval(THREE)
        give = result["give_side"][0]
        assert give["per_source"] == {"a": 1000, "b": 5000, "c": 3000}

    def test_a_source_that_cannot_price_a_player_says_so(self):
        partial = {
            "a": snap("a", {"4034": 1000, "9509": 2000}),
            "b": snap("b", {"4034": 5000}),   # no Bijan
        }
        result = run_eval(partial)
        assert result["sources"]["b"]["unmatched"] == ["Bijan Robinson"]
        assert result["sources"]["a"]["unmatched"] == []

    def test_source_errors_are_carried_through(self):
        result = run_eval({"a": snap("a", {"4034": 1, "9509": 2})},
                          errors={"ktc": "page changed"})
        assert result["source_errors"] == {"ktc": "page changed"}

    def test_no_sources_at_all_raises(self):
        with pytest.raises(ValuesError):
            run_eval({}, errors={"fantasycalc": "down"})


class TestConsensus:
    def test_majority_gate(self):
        result = run_eval(THREE)  # 1 win, 1 loss, 1 even of 3
        c = result["consensus"]
        assert (c["wins"], c["losses"], c["even"]) == (1, 1, 1)
        assert c["gate"] == "majority"
        assert c["passes"] is False   # 1 of 3 is not a majority

    def test_all_gate(self):
        cfg = AppConfig()
        cfg.consensus["gate"] = "all"
        wins = {
            "a": snap("a", {"4034": 1000, "9509": 2000}),
            "b": snap("b", {"4034": 4000, "9509": 5000}),
        }
        assert run_eval(wins, cfg=cfg)["consensus"]["passes"] is True
        assert run_eval(THREE, cfg=cfg)["consensus"]["passes"] is False

    def test_any_gate(self):
        cfg = AppConfig()
        cfg.consensus["gate"] = "any"
        assert run_eval(THREE, cfg=cfg)["consensus"]["passes"] is True

    def test_stale_sources_do_not_vote_but_still_show(self):
        mixed = {
            "a": snap("a", {"4034": 1000, "9509": 2000}),
            "b": snap("b", {"4034": 5000, "9509": 4000}, stale=True),
        }
        result = run_eval(mixed)
        assert result["consensus"]["eligible"] == 1
        assert result["consensus"]["wins"] == 1
        assert result["sources"]["b"]["stale"] is True   # visible anyway

    def test_stale_sources_vote_when_configured_in(self):
        cfg = AppConfig()
        cfg.consensus["exclude_stale"] = False
        mixed = {
            "a": snap("a", {"4034": 1000, "9509": 2000}),
            "b": snap("b", {"4034": 5000, "9509": 4000}, stale=True),
        }
        assert run_eval(mixed, cfg=cfg)["consensus"]["eligible"] == 2

    def test_even_band_is_configurable(self):
        cfg = AppConfig()
        cfg.consensus["even_band_pct"] = 25.0
        result = run_eval(THREE, cfg=cfg)
        # b's 20% edge now falls inside the band.
        assert result["sources"]["b"]["winner"] == "EVEN"


class TestComposite:
    def test_single_source_composite_equals_that_source(self):
        result = run_eval({"a": snap("a", {"4034": 1000, "9509": 2000})})
        assert result["give_total"] == 1000
        assert result["get_total"] == 2000
        assert result["winner"] == "GET"

    def test_composite_normalises_before_blending(self):
        # Two sources with identical ORDERINGS but wildly different scales
        # must produce the same composite as either alone (rank-mapped onto
        # the reference curve), not an average of currencies.
        result = run_eval({
            "a": snap("a", {"4034": 1000, "9509": 2000, "6794": 3000}),
            "b": snap("b", {"4034": 9000, "9509": 18000, "6794": 27000}),
        })
        assert result["give_total"] == 1000
        assert result["get_total"] == 2000

    def test_zero_weight_source_keeps_its_row_but_not_its_vote_in_the_blend(self):
        cfg = AppConfig()
        cfg.sources["b"] = {"enabled": True, "weight": 0.0}
        result = run_eval({
            "a": snap("a", {"4034": 1000, "9509": 2000}),
            "b": snap("b", {"4034": 100000, "9509": 1}),
        }, cfg=cfg)
        assert result["give_total"] == 1000        # composite ignores b
        assert result["sources"]["b"]["winner"] == "GIVE"   # row remains


class TestPicksAndOverrides:
    def test_picks_price_per_source_from_that_sources_pick_sheet(self):
        snaps = {
            "fantasycalc": snap("fantasycalc", {"4034": 1000, "9509": 2000},
                                picks={"2027-1-early": 4000}),
            "ktc": snap("ktc", {"4034": 900, "9509": 1800},
                        picks={"2027-1-early": 7000}),
        }
        trade = TradeCommand(give=["Christian McCaffrey"],
                             get=["2027 Early 1st"])
        result = run_eval(snaps, trade=trade)
        get_row = result["get_side"][0]
        assert get_row["per_source"]["fantasycalc"] == 4000
        assert get_row["per_source"]["ktc"] == 7000

    def test_manual_override_wins_under_every_source(self):
        trade = TradeCommand(give=["Christian McCaffrey"],
                             get=["Bijan Robinson"],
                             overrides={"Bijan Robinson": 7777})
        result = run_eval(THREE, trade=trade)
        for row in result["get_side"][0]["per_source"].values():
            assert row == 7777

    def test_unmatched_player_is_surfaced_not_zeroed_silently(self):
        trade = TradeCommand(give=["Christian McCaffrey"],
                             get=["Nonexistent Player"])
        result = run_eval(THREE, trade=trade)
        assert "Nonexistent Player" in result["unmatched"]


class TestFormatSourceTable:
    def test_table_has_a_line_per_source_plus_consensus(self):
        text = format_source_table(run_eval(THREE))
        lines = text.splitlines()
        assert len(lines) == 4
        assert lines[-1].startswith("Consensus:")
        assert "WIN" in lines[0] and "LOSS" in lines[1] and "EVEN" in lines[2]

    def test_failed_sources_appear_as_unavailable(self):
        result = run_eval({"a": snap("a", {"4034": 1, "9509": 2})},
                          errors={"ktc": "page changed"})
        assert "unavailable: page changed" in format_source_table(result)
