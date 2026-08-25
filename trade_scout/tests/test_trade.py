"""Tests for trade command parsing and math."""

import pytest

from trade_scout.trade import (
    TradeAsset,
    TradeParseError,
    _build_asset_dict,
    _classify_team_posture,
    build_roster_context,
    parse_trade_command,
)


@pytest.fixture(autouse=True)
def _two_leagues(monkeypatch):
    """League shortname recognition is config-driven now: the parser only
    accepts tokens that resolve to a configured league. These synthetic env
    entries stand in for a user config with DD and CL leagues."""
    monkeypatch.setenv("SLEEPER_LEAGUE_ID", "1000000000000000001")
    monkeypatch.setenv("SLEEPER_LEAGUE_CL", "2000000000000000002")


class TestParseTradeCommand:
    def test_basic_trade(self):
        result = parse_trade_command("/trade give: CMC | get: Bijan Robinson")
        assert result.give == ["CMC"]
        assert result.get == ["Bijan Robinson"]

    def test_multiple_assets(self):
        result = parse_trade_command("/trade give: CMC, 2026 1st | get: Bijan Robinson")
        assert result.give == ["CMC", "2026 1st"]
        assert result.get == ["Bijan Robinson"]

    def test_with_override(self):
        result = parse_trade_command(
            "/trade give: CMC | get: Bijan Robinson | flock: Bijan Robinson=8200"
        )
        assert result.overrides == {"Bijan Robinson": 8200}

    def test_multiple_overrides(self):
        result = parse_trade_command(
            "/trade give: CMC | get: Bijan | flock: Bijan=8200, CMC=6000"
        )
        assert result.overrides == {"Bijan": 8200, "CMC": 6000}

    def test_with_partner(self):
        result = parse_trade_command(
            "/trade give: CMC | get: Bijan | partner: TeamMike"
        )
        assert result.partner == "TeamMike"

    def test_without_slash_trade(self):
        result = parse_trade_command("give: CMC | get: Bijan Robinson")
        assert result.give == ["CMC"]
        assert result.get == ["Bijan Robinson"]

    def test_empty_raises(self):
        with pytest.raises(TradeParseError, match="Empty"):
            parse_trade_command("")

    def test_empty_after_slash_without_llm_hints_the_grammar(self):
        """'/trade' with no body falls to the NL path, which without an LLM
        answers with the structured-grammar hint."""
        with pytest.raises(TradeParseError, match="give: Player1"):
            parse_trade_command("/trade")

    def test_empty_after_slash_with_a_broken_cli_still_errors(self):
        from unittest.mock import patch

        with patch("trade_scout.llm.llm_available", return_value=True), \
                patch("trade_scout.trade.subprocess.run",
                      side_effect=FileNotFoundError):
            with pytest.raises(TradeParseError, match="Couldn't understand"):
                parse_trade_command("/trade")

    def test_bad_override_value_raises(self):
        with pytest.raises(TradeParseError, match="Invalid override"):
            parse_trade_command("/trade give: CMC | get: Bijan | flock: Bijan=notanumber")


class TestLeagueParsing:
    def test_no_league_named_yields_none(self):
        # Was: asserts the default is DD. That default is what let every
        # console trade be priced against the wrong league for a day. Text
        # with no token names no league, and says so.
        result = parse_trade_command("/trade give: CMC | get: Bijan")
        assert result.league is None
        assert result.league_explicit is False

    def test_league_section(self):
        result = parse_trade_command("/trade give: CMC | get: Bijan | league: CL")
        assert result.league == "CL"

    def test_league_section_case_insensitive(self):
        result = parse_trade_command("/trade give: CMC | get: Bijan | league: cl")
        assert result.league == "CL"

    def test_bare_cl_prefix(self):
        result = parse_trade_command("/trade CL give: CMC | get: Bijan")
        assert result.league == "CL"
        assert result.give == ["CMC"]

    def test_bare_dd_prefix(self):
        result = parse_trade_command("/trade DD give: CMC | get: Bijan")
        assert result.league == "DD"
        assert result.give == ["CMC"]

    def test_unknown_league_section_is_not_silently_dd(self):
        # "league: XYZ" means the sender tried to name one and got it wrong.
        # Reading that as DD is how a typo becomes a confidently wrong verdict.
        result = parse_trade_command("/trade give: CMC | get: Bijan | league: XYZ")
        assert result.league is None


class TestEnrichedAssetDict:
    def test_player_asset_includes_rank_fields(self):
        asset = TradeAsset(
            raw_name="Bijan Robinson",
            player_id="123",
            value=9500,
            overall_rank=3,
            position_rank=2,
            trend_30day=1.5,
        )
        players_map = {
            "123": {
                "position": "RB", "full_name": "Bijan Robinson",
                "age": 23, "years_exp": 2,
            },
        }
        d = _build_asset_dict(asset, players_map)
        assert d["name"] == "Bijan Robinson"
        assert d["value"] == 9500
        assert d["overall_rank"] == 3
        assert d["position_rank"] == 2
        assert d["trend_30day"] == 1.5
        assert d["position"] == "RB"
        assert d["is_pick"] is False
        assert d["age"] == 23
        assert d["years_exp"] == 2

    def test_player_with_injury(self):
        asset = TradeAsset(raw_name="Injured Guy", player_id="789", value=5000)
        players_map = {
            "789": {
                "position": "WR", "full_name": "Injured Guy",
                "age": 27, "years_exp": 5, "injury_status": "IR",
            },
        }
        d = _build_asset_dict(asset, players_map)
        assert d["injury_status"] == "IR"
        assert d["age"] == 27

    def test_pick_asset_no_position(self):
        asset = TradeAsset(
            raw_name="2026 1st",
            is_pick=True,
            value=7000,
        )
        d = _build_asset_dict(asset, {})
        assert d["is_pick"] is True
        assert d["value"] == 7000
        assert "position" not in d
        assert "age" not in d

    def test_asset_without_ranks_omits_fields(self):
        asset = TradeAsset(raw_name="NoRank Guy", player_id="456", value=1000)
        players_map = {"456": {"position": "WR", "full_name": "NoRank Guy"}}
        d = _build_asset_dict(asset, players_map)
        assert "overall_rank" not in d
        assert "position_rank" not in d
        assert "trend_30day" not in d
        assert "flock_rank" not in d
        assert "flock_tier" not in d
        assert "flock_pos_rank" not in d
        assert "age" not in d
        assert d["position"] == "WR"

    def test_asset_with_flock_data(self):
        asset = TradeAsset(
            raw_name="CeeDee Lamb",
            player_id="789",
            value=8500,
            overall_rank=5,
            flock_rank=4.25,
            flock_tier="S",
            flock_pos_rank=1,
        )
        players_map = {
            "789": {"position": "WR", "full_name": "CeeDee Lamb", "age": 25},
        }
        d = _build_asset_dict(asset, players_map)
        assert d["flock_rank"] == 4.25
        assert d["flock_tier"] == "S"
        assert d["flock_pos_rank"] == 1
        assert d["overall_rank"] == 5
        assert d["position"] == "WR"


class TestRosterComposition:
    def _make_sleeper_stub(self, user, roster):
        """Build a minimal sleeper stub with find_user_by_name and find_user_roster."""

        class Stub:
            def find_user_by_name(self, name):
                return user

            def find_user_roster(self, owner_id):
                return roster

        return Stub()

    def test_builds_position_summary(self):
        user = {"user_id": "owner1", "display_name": "TestOwner"}
        roster = {"owner_id": "owner1", "players": ["p1", "p2", "p3"]}
        players_map = {
            "p1": {"position": "RB", "full_name": "Player One", "age": 24},
            "p2": {"position": "RB", "full_name": "Player Two", "age": 26},
            "p3": {"position": "WR", "full_name": "Player Three", "age": 23},
        }
        values_data = {
            "p1": {"value": 9000, "overall_rank": 1},
            "p2": {"value": 5000, "overall_rank": 10},
            "p3": {"value": 7000, "overall_rank": 5},
        }
        give = [TradeAsset(raw_name="Player One", player_id="p1")]
        get = [TradeAsset(raw_name="Player Three", player_id="p3")]

        sleeper = self._make_sleeper_stub(user, roster)
        ctx = build_roster_context(sleeper, "TestOwner", players_map, values_data, give, get)

        assert ctx is not None
        assert ctx["positions"]["RB"]["count"] == 2
        assert ctx["positions"]["WR"]["count"] == 1
        assert "losing 1 RB" in ctx["trade_impact"]
        assert "gaining 1 WR" in ctx["trade_impact"]

    def test_returns_none_when_user_not_found(self):
        sleeper = self._make_sleeper_stub(None, None)
        ctx = build_roster_context(sleeper, "Nobody", {}, {}, [], [])
        assert ctx is None

    def test_swap_same_position(self):
        user = {"user_id": "o1", "display_name": "TestOwner"}
        roster = {"owner_id": "o1", "players": ["p1", "p2"]}
        players_map = {
            "p1": {"position": "WR", "full_name": "WR1"},
            "p2": {"position": "WR", "full_name": "WR2"},
        }
        values_data = {
            "p1": {"value": 8000, "overall_rank": 2},
            "p2": {"value": 3000, "overall_rank": 20},
        }
        give = [TradeAsset(raw_name="WR1", player_id="p1")]
        get = [TradeAsset(raw_name="WR2", player_id="p2")]

        sleeper = self._make_sleeper_stub(user, roster)
        ctx = build_roster_context(sleeper, "TestOwner", players_map, values_data, give, get)

        assert ctx is not None
        assert "swapping 1 WR for 1 WR" in ctx["trade_impact"]

    def test_roster_context_includes_team_posture(self):
        user = {"user_id": "o1", "display_name": "TestOwner"}
        roster = {"owner_id": "o1", "players": ["p1", "p2", "p3"]}
        players_map = {
            "p1": {"position": "RB", "full_name": "Young RB", "age": 22},
            "p2": {"position": "WR", "full_name": "Young WR", "age": 23},
            "p3": {"position": "QB", "full_name": "Young QB", "age": 24},
        }
        values_data = {
            "p1": {"value": 8000, "overall_rank": 3},
            "p2": {"value": 7000, "overall_rank": 5},
            "p3": {"value": 6000, "overall_rank": 8},
        }
        sleeper = self._make_sleeper_stub(user, roster)
        ctx = build_roster_context(sleeper, "TestOwner", players_map, values_data, [], [])
        assert ctx is not None
        assert "team_posture" in ctx
        assert ctx["team_posture"]["label"] in ("contending", "retooling", "rebuilding")

    def test_roster_entries_include_age(self):
        user = {"user_id": "o1", "display_name": "TestOwner"}
        roster = {"owner_id": "o1", "players": ["p1"]}
        players_map = {"p1": {"position": "RB", "full_name": "Bijan", "age": 23}}
        values_data = {"p1": {"value": 9500, "overall_rank": 1}}
        sleeper = self._make_sleeper_stub(user, roster)
        ctx = build_roster_context(sleeper, "TestOwner", players_map, values_data, [], [])
        assert ctx is not None
        assert ctx["positions"]["RB"]["players"][0]["age"] == 23


class TestTeamPosture:
    def test_contending_roster(self):
        """Roster of aging high-value players should be 'contending'."""
        positions = {
            "RB": [
                {"name": "RB1", "value": 8000, "rank": 2, "age": 28, "pos": "RB"},
                {"name": "RB2", "value": 6000, "rank": 8, "age": 27, "pos": "RB"},
            ],
            "WR": [
                {"name": "WR1", "value": 7500, "rank": 4, "age": 30, "pos": "WR"},
                {"name": "WR2", "value": 7000, "rank": 5, "age": 31, "pos": "WR"},
            ],
            "QB": [
                {"name": "QB1", "value": 9000, "rank": 1, "age": 34, "pos": "QB"},
            ],
        }
        result = _classify_team_posture(positions)
        assert result["label"] == "contending"

    def test_rebuilding_roster(self):
        """Roster of young players should be 'rebuilding'."""
        positions = {
            "RB": [
                {"name": "RB1", "value": 7000, "rank": 5, "age": 21, "pos": "RB"},
                {"name": "RB2", "value": 5000, "rank": 12, "age": 22, "pos": "RB"},
            ],
            "WR": [
                {"name": "WR1", "value": 8000, "rank": 3, "age": 22, "pos": "WR"},
                {"name": "WR2", "value": 6000, "rank": 7, "age": 23, "pos": "WR"},
            ],
            "QB": [
                {"name": "QB1", "value": 6500, "rank": 6, "age": 24, "pos": "QB"},
            ],
        }
        result = _classify_team_posture(positions)
        assert result["label"] == "rebuilding"

    def test_retooling_mixed_roster(self):
        """Mixed age roster should be 'retooling'."""
        positions = {
            "RB": [
                {"name": "RB1", "value": 8000, "rank": 2, "age": 28, "pos": "RB"},
            ],
            "WR": [
                {"name": "WR1", "value": 7500, "rank": 4, "age": 23, "pos": "WR"},
                {"name": "WR2", "value": 7000, "rank": 5, "age": 24, "pos": "WR"},
            ],
            "QB": [
                {"name": "QB1", "value": 9000, "rank": 1, "age": 25, "pos": "QB"},
            ],
        }
        result = _classify_team_posture(positions)
        assert result["label"] == "retooling"

    def test_empty_roster(self):
        result = _classify_team_posture({})
        assert result["label"] == "unknown"

    def test_no_age_data(self):
        positions = {
            "RB": [{"name": "RB1", "value": 8000, "rank": 2}],
        }
        result = _classify_team_posture(positions)
        assert result["label"] == "unknown"
