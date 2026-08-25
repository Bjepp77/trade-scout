"""The provider layer: registry contract, snapshot resolution, N-source seam.

These tests never touch the network (conftest enforces it); providers are
exercised through monkeypatched transport or canned snapshots.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from trade_scout.config import AppConfig
from trade_scout.engine.resolve import resolve_snapshot
from trade_scout.model import blend_market_n
from trade_scout.name_matcher import NameMatcher
from trade_scout.providers import REGISTRY, REGISTRY_BY_KEY
from trade_scout.providers.base import (
    LeagueFormat,
    SourceError,
    SourceSnapshot,
    ValueSource,
)
from trade_scout.values import get_snapshots

FMT = LeagueFormat(superflex=True)

PLAYERS_MAP = {
    "4034": {"first_name": "Christian", "last_name": "McCaffrey", "position": "RB"},
    "9509": {"first_name": "Bijan", "last_name": "Robinson", "position": "RB"},
    "6794": {"first_name": "Justin", "last_name": "Jefferson", "position": "WR"},
}


def snap(source: str, join: str, players: dict, **kw) -> SourceSnapshot:
    return SourceSnapshot(
        source=source, display_name=source.title(), as_of="2026-08-24",
        fmt=FMT, join=join, players=players, **kw)  # type: ignore[arg-type]


class TestRegistry:
    def test_every_entry_satisfies_the_protocol(self):
        for s in REGISTRY:
            assert isinstance(s, ValueSource), s
            assert s.key and s.display_name
            assert isinstance(s.requires, tuple)

    def test_priority_order_is_pinned(self):
        # The first available source is the reference curve every other
        # source is mapped onto. Reordering this silently re-prices verdicts.
        assert [s.key for s in REGISTRY] == [
            "fantasycalc", "dynastyprocess", "ktc", "dynastydaddy"]

    def test_default_enablement(self):
        cfg = AppConfig()
        on = {s.key for s in REGISTRY if s.enabled(cfg)}
        assert on == {"fantasycalc", "dynastyprocess", "ktc", "dynastydaddy"}


class TestResolveSnapshot:
    def test_sleeper_id_join_passes_through(self):
        s = snap("fantasycalc", "sleeper_id", {"4034": {"value": 5000}})
        assert resolve_snapshot(s, NameMatcher(PLAYERS_MAP)) is s

    def test_name_join_resolves_to_sleeper_ids(self):
        s = snap("ktc", "name", {
            "christian mccaffrey": {"value": 4000, "name": "Christian McCaffrey",
                                    "position": "RB"},
            "bijan robinson": {"value": 9000, "name": "Bijan Robinson",
                               "position": "RB"},
        })
        r = resolve_snapshot(s, NameMatcher(PLAYERS_MAP))
        assert r.join == "sleeper_id"
        assert r.players["4034"]["value"] == 4000
        assert r.players["9509"]["value"] == 9000
        assert r.meta["unmatched_names"] == 0

    def test_unmatched_names_are_dropped_and_counted(self):
        s = snap("ktc", "name", {
            "bijan robinson": {"value": 9000, "name": "Bijan Robinson",
                               "position": "RB"},
            "some rookie nobody": {"value": 100, "name": "Some Rookie Nobody",
                                   "position": "WR"},
        })
        r = resolve_snapshot(s, NameMatcher(PLAYERS_MAP))
        assert set(r.players) == {"9509"}
        assert r.meta["unmatched_names"] == 1

    def test_wrong_position_hint_still_matches_by_name(self):
        # Sources and Sleeper occasionally disagree on a player's position;
        # an unambiguous name must survive that.
        s = snap("ktc", "name", {
            "justin jefferson": {"value": 8000, "name": "Justin Jefferson",
                                 "position": "RB"},   # wrong on purpose
        })
        r = resolve_snapshot(s, NameMatcher(PLAYERS_MAP))
        assert r.players["6794"]["value"] == 8000


class TestGetSnapshots:
    def _league(self):
        return {"num_qbs": 2, "num_teams": 12, "ppr": 1}

    def test_one_failing_source_is_excluded_not_fatal(self):
        good = snap("fantasycalc", "sleeper_id", {"4034": {"value": 5000}})
        with patch.object(REGISTRY_BY_KEY["fantasycalc"], "fetch",
                          return_value=good), \
             patch.object(REGISTRY_BY_KEY["dynastyprocess"], "fetch",
                          side_effect=SourceError("weekly file gone")), \
             patch.object(REGISTRY_BY_KEY["ktc"], "fetch",
                          side_effect=SourceError("page changed")), \
             patch.object(REGISTRY_BY_KEY["dynastydaddy"], "fetch",
                          side_effect=SourceError("api gone")):
            snaps, errors = get_snapshots(self._league(), AppConfig())
        assert set(snaps) == {"fantasycalc"}
        assert set(errors) == {"dynastyprocess", "ktc", "dynastydaddy"}

    def test_registry_order_is_preserved_in_results(self):
        with patch.object(REGISTRY_BY_KEY["fantasycalc"], "fetch",
                          return_value=snap("fantasycalc", "sleeper_id", {})), \
             patch.object(REGISTRY_BY_KEY["dynastyprocess"], "fetch",
                          return_value=snap("dynastyprocess", "sleeper_id", {})), \
             patch.object(REGISTRY_BY_KEY["ktc"], "fetch",
                          return_value=snap("ktc", "name", {})), \
             patch.object(REGISTRY_BY_KEY["dynastydaddy"], "fetch",
                          return_value=snap("dynastydaddy", "sleeper_id", {})):
            snaps, _ = get_snapshots(self._league(), AppConfig())
        assert list(snaps) == [
            "fantasycalc", "dynastyprocess", "ktc", "dynastydaddy"]

    def test_disabled_sources_are_not_fetched(self):
        cfg = AppConfig()
        for key in ("dynastyprocess", "ktc", "dynastydaddy"):
            cfg.sources[key]["enabled"] = False
        with patch.object(REGISTRY_BY_KEY["fantasycalc"], "fetch",
                          return_value=snap("fantasycalc", "sleeper_id", {})), \
             patch.object(REGISTRY_BY_KEY["dynastyprocess"], "fetch") as dp, \
             patch.object(REGISTRY_BY_KEY["ktc"], "fetch") as kt:
            snaps, errors = get_snapshots(self._league(), cfg)
        assert list(snaps) == ["fantasycalc"]
        assert not errors
        dp.assert_not_called()
        kt.assert_not_called()

    def test_matcher_resolves_name_joined_sources(self):
        ktc = snap("ktc", "name", {
            "bijan robinson": {"value": 9000, "name": "Bijan Robinson",
                               "position": "RB"}})
        with patch.object(REGISTRY_BY_KEY["fantasycalc"], "fetch",
                          side_effect=SourceError("down")), \
             patch.object(REGISTRY_BY_KEY["dynastyprocess"], "fetch",
                          side_effect=SourceError("down")), \
             patch.object(REGISTRY_BY_KEY["dynastydaddy"], "fetch",
                          side_effect=SourceError("down")), \
             patch.object(REGISTRY_BY_KEY["ktc"], "fetch", return_value=ktc):
            snaps, _ = get_snapshots(self._league(), AppConfig(),
                                     matcher=NameMatcher(PLAYERS_MAP))
        assert snaps["ktc"].players["9509"]["value"] == 9000


class TestBlendMarketN:
    def test_single_source_is_visibly_single_source(self):
        blended, detail = blend_market_n(
            {"fantasycalc": 1000.0, "ktc": None}, {})
        assert blended == 1000.0
        assert detail["sources"] == ["fantasycalc"]
        assert detail["disagreement_pct"] is None

    def test_weighted_blend_and_worst_pairwise_gap(self):
        blended, detail = blend_market_n(
            {"a": 1000.0, "b": 2000.0, "c": 1500.0},
            {"a": 1.0, "b": 1.0, "c": 2.0})
        assert blended == pytest.approx((1000 + 2000 + 3000) / 4)
        assert detail["disagreement_pct"] == pytest.approx(
            (2000 - 1000) / blended * 100, abs=0.1)

    def test_zero_weight_excludes_a_source_from_the_number(self):
        blended, detail = blend_market_n(
            {"a": 1000.0, "b": 9999.0}, {"b": 0.0})
        assert blended == 1000.0
        assert detail["sources"] == ["a"]
        # ...but its value stays visible in the detail.
        assert detail["per_source"]["b"] == 9999.0

    def test_no_sources_is_none(self):
        blended, detail = blend_market_n({"a": None}, {})
        assert blended is None
        assert detail["sources"] == []
