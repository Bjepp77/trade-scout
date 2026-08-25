"""Tests for Flock Fantasy rankings provider."""

import inspect
from unittest.mock import patch

from trade_scout.providers.flock import TIER_MAP, FlockError, _parse_rows
from trade_scout.trade import TradeAsset, _build_asset_dict


class TestTierMapping:
    def test_s_tier(self):
        assert TIER_MAP[1] == "S"

    def test_a_tier(self):
        assert TIER_MAP[2] == "A"

    def test_r_tier(self):
        assert TIER_MAP[19] == "R"

    def test_all_19_tiers(self):
        assert len(TIER_MAP) == 19
        for i in range(1, 20):
            assert i in TIER_MAP

    def test_tier_values_are_unique(self):
        assert len(set(TIER_MAP.values())) == 19


class TestParseRows:
    def test_basic_parsing(self):
        rows = [
            {
                "playerName": "Josh Allen",
                "position": "QB",
                "team": "BUF",
                "age": 28,
                "experience": 7,
                "overallAverageRank": 2.13,
                "averagePositionalRank": 1,
                "averageTier": 1,
                "rankDelta": 0,
                "previousYearPprAverage": "22.3",
                "overallRanks": {"Expert1": 1, "Expert2": 3},
                "fantasyCalcId": 123,
            },
        ]
        result = _parse_rows(rows)
        assert "josh allen" in result
        entry = result["josh allen"]
        assert entry["flock_rank"] == 2.13
        assert entry["flock_tier"] == "S"
        assert entry["flock_pos_rank"] == 1
        assert entry["rank_delta"] == 0
        assert entry["position"] == "QB"

    def test_multiple_players(self):
        rows = [
            {
                "playerName": "Bijan Robinson",
                "position": "RB",
                "overallAverageRank": 1.0,
                "averagePositionalRank": 1,
                "averageTier": 1,
                "rankDelta": 0,
            },
            {
                "playerName": "Jahmyr Gibbs",
                "position": "RB",
                "overallAverageRank": 5.5,
                "averagePositionalRank": 3,
                "averageTier": 2,
                "rankDelta": -2,
            },
        ]
        result = _parse_rows(rows)
        assert len(result) == 2
        assert result["bijan robinson"]["flock_tier"] == "S"
        assert result["jahmyr gibbs"]["flock_tier"] == "A"
        assert result["jahmyr gibbs"]["rank_delta"] == -2

    def test_missing_player_name_skipped(self):
        rows = [
            {"position": "QB", "overallAverageRank": 10.0, "averageTier": 3},
        ]
        result = _parse_rows(rows)
        assert len(result) == 0

    def test_unknown_tier(self):
        rows = [
            {
                "playerName": "Unknown Tier",
                "position": "WR",
                "overallAverageRank": 400.0,
                "averagePositionalRank": 100,
                "averageTier": 99,
                "rankDelta": 0,
            },
        ]
        result = _parse_rows(rows)
        assert result["unknown tier"]["flock_tier"] == "?"

    def test_missing_tier_field(self):
        rows = [
            {
                "playerName": "No Tier",
                "position": "TE",
                "overallAverageRank": 50.0,
                "averagePositionalRank": 5,
            },
        ]
        result = _parse_rows(rows)
        assert result["no tier"]["flock_tier"] == "?"

    def test_case_insensitive_key(self):
        rows = [
            {
                "playerName": "CeeDee Lamb",
                "position": "WR",
                "overallAverageRank": 3.0,
                "averagePositionalRank": 1,
                "averageTier": 1,
            },
        ]
        result = _parse_rows(rows)
        assert "ceedee lamb" in result


class TestAssetDictFlockFields:
    def test_flock_fields_included_when_present(self):
        asset = TradeAsset(
            raw_name="Bijan Robinson",
            player_id="123",
            value=9500,
            overall_rank=3,
            flock_rank=1.0,
            flock_tier="S",
            flock_pos_rank=1,
        )
        players_map = {
            "123": {"position": "RB", "full_name": "Bijan Robinson", "age": 23},
        }
        d = _build_asset_dict(asset, players_map)
        assert d["flock_rank"] == 1.0
        assert d["flock_tier"] == "S"
        assert d["flock_pos_rank"] == 1

    def test_flock_fields_omitted_when_none(self):
        asset = TradeAsset(
            raw_name="NoFlock Guy",
            player_id="456",
            value=5000,
        )
        players_map = {"456": {"position": "WR", "full_name": "NoFlock Guy"}}
        d = _build_asset_dict(asset, players_map)
        assert "flock_rank" not in d
        assert "flock_tier" not in d
        assert "flock_pos_rank" not in d

    def test_pick_asset_no_flock_fields(self):
        asset = TradeAsset(raw_name="2026 1st", is_pick=True, value=7000)
        d = _build_asset_dict(asset, {})
        assert "flock_rank" not in d
        assert "flock_tier" not in d


class TestFlockFallback:
    def test_try_flock_returns_empty_on_import_error(self):
        from trade_scout.trade import _try_flock_rankings

        with patch(
            "trade_scout.providers.flock.get_flock_rankings",
            side_effect=FlockError("no playwright"),
        ):
            result = _try_flock_rankings(True)
            assert result == {}

    def test_try_flock_returns_empty_on_exception(self):
        from trade_scout.trade import _try_flock_rankings

        with patch(
            "trade_scout.providers.flock.get_flock_rankings",
            side_effect=RuntimeError("boom"),
        ):
            result = _try_flock_rankings(True)
            assert result == {}

    def test_try_flock_returns_data_on_success(self):
        from trade_scout.trade import _try_flock_rankings

        mock_data = {
            "josh allen": {
                "flock_rank": 2.13,
                "flock_tier": "S",
                "flock_pos_rank": 1,
            },
        }
        with patch(
            "trade_scout.providers.flock.get_flock_rankings",
            return_value=mock_data,
        ):
            result = _try_flock_rankings(True)
            assert result == mock_data


class TestFlockFormatReachesTheProvider:
    """The board a league is judged against must match the league.

    Flock publishes a different board per format and this call used to take a
    superflex default in both leagues. Measured against the live site: the
    superflex top ten holds four QBs led by Josh Allen, the 1QB top ten holds
    zero and is led by Jahmyr Gibbs. Since the prompt mandates flagging
    Flock-versus-market divergence, the wrong board did not degrade quietly, it
    manufactured a standing buy-QB signal in the 1QB league.

    FantasyCalc and KTC were both already format-switched. Flock was the only
    source that was not, which is why nothing else caught it.
    """

    def test_superflex_asks_for_the_superflex_board(self):
        from trade_scout.trade import _try_flock_rankings

        with patch("trade_scout.providers.flock.get_flock_rankings",
                   return_value={"x": {}}) as m:
            _try_flock_rankings(True)
        assert m.call_args[0][0] == "superflex"

    def test_one_qb_asks_for_the_oneqb_board(self):
        from trade_scout.trade import _try_flock_rankings

        with patch("trade_scout.providers.flock.get_flock_rankings",
                   return_value={"x": {}}) as m:
            _try_flock_rankings(False)
        assert m.call_args[0][0] == "oneqb", "the 1QB league got the wrong board"

    def test_the_two_formats_never_ask_for_the_same_board(self):
        from trade_scout.trade import FLOCK_FORMATS

        assert FLOCK_FORMATS[True] != FLOCK_FORMATS[False]

    def test_the_format_argument_is_required(self):
        """No default. A default is what caused the bug."""
        import inspect

        from trade_scout.trade import _try_flock_rankings

        sig = inspect.signature(_try_flock_rankings)
        assert sig.parameters["superflex"].default is inspect.Parameter.empty

    def test_the_roster_board_passes_its_own_format(self):
        """rosters.py computes superflex; it must hand that to Flock, not guess."""
        import trade_scout.rosters as R

        src = inspect.getsource(R.build_rosters)
        assert "_try_flock_rankings(superflex)" in src
