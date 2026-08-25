"""Tests for consultative trade skeleton generator."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from trade_scout.consult import (
    ConsultParseError,
    _enrich_roster,
    _find_partner_roster,
    _split_player_list,
    is_consult_command,
    parse_consult_command,
)


@pytest.fixture(autouse=True)
def _two_leagues(monkeypatch):
    """League shortname recognition is config-driven now: the parser only
    accepts tokens that resolve to a configured league. These synthetic env
    entries stand in for a user config with DD and CL leagues."""
    monkeypatch.setenv("SLEEPER_LEAGUE_ID", "1000000000000000001")
    monkeypatch.setenv("SLEEPER_LEAGUE_CL", "2000000000000000002")


class TestIsConsultCommand:
    def test_slash_consult(self):
        assert is_consult_command("/consult CL Mike wants Caleb Williams") is True

    def test_bare_consult(self):
        assert is_consult_command("consult bignick interested in my bijan DD") is True

    def test_wants_my_phrase(self):
        assert is_consult_command("mike wants my Bijan Robinson") is True

    def test_interested_in_my_phrase(self):
        assert is_consult_command("bignick is interested in my CeeDee") is True

    def test_asking_about_my_phrase(self):
        assert is_consult_command("dave asking about my picks") is True

    def test_if_i_give_him_phrase(self):
        assert is_consult_command("if i give him Caleb Williams") is True

    def test_consult_me_on_phrase(self):
        assert is_consult_command("consult me on trades with Mike") is True

    def test_trades_i_could_do_phrase(self):
        assert is_consult_command("trades i could do with Dave") is True

    def test_trade_command_not_consult(self):
        assert is_consult_command("/trade give: CMC | get: Bijan") is False

    def test_random_text_not_consult(self):
        assert is_consult_command("what is CeeDee Lamb's value?") is False

    def test_case_insensitive(self):
        assert is_consult_command("/CONSULT CL Mike wants Bijan") is True


class TestSplitPlayerList:
    def test_single_player(self):
        assert _split_player_list("Caleb Williams") == ["Caleb Williams"]

    def test_and_separator(self):
        assert _split_player_list("RJ Harvey and Caleb Williams") == [
            "RJ Harvey", "Caleb Williams",
        ]

    def test_comma_separator(self):
        assert _split_player_list("RJ Harvey, Caleb Williams") == [
            "RJ Harvey", "Caleb Williams",
        ]

    def test_three_players(self):
        assert _split_player_list("Player A, Player B and Player C") == [
            "Player A", "Player B", "Player C",
        ]

    def test_empty_returns_original(self):
        assert _split_player_list("") == [""]

    def test_picks_and_players(self):
        assert _split_player_list("Bijan and a 2026 1st") == [
            "Bijan", "a 2026 1st",
        ]


class TestParseConsultCommand:
    def test_structured_basic(self):
        result = parse_consult_command("/consult CL Mike wants Caleb Williams")
        assert result.partner_name == "Mike"
        assert result.target_players == ["Caleb Williams"]
        assert result.target_player == "Caleb Williams"
        assert result.league == "CL"

    def test_no_league_named_yields_none_not_a_default(self):
        # None, never "DD". A default equal to a legal value makes "the owner
        # said DD" indistinguishable from "nobody said anything", and the two
        # leagues score differently enough (1QB Standard vs Superflex PPR) that
        # guessing prices the trade in the wrong currency.
        result = parse_consult_command("/consult Mike wants Bijan Robinson")
        assert result.partner_name == "Mike"
        assert result.target_players == ["Bijan Robinson"]
        assert result.league is None

    def test_structured_dd_prefix(self):
        result = parse_consult_command("/consult DD BigNick wants CeeDee Lamb")
        assert result.partner_name == "BigNick"
        assert result.target_players == ["CeeDee Lamb"]
        assert result.league == "DD"

    def test_interested_in_my_pattern(self):
        result = parse_consult_command("consult bignick interested in my Bijan")
        assert result.partner_name == "bignick"
        assert result.target_players == ["Bijan"]
        # No leading league token in this message, so nothing selected a
        # league. Prose that mentions one is not a selection.
        assert result.league is None

    def test_asking_about_my_pattern(self):
        result = parse_consult_command("/consult CL dave asking about my Lamar")
        assert result.partner_name == "dave"
        assert result.target_players == ["Lamar"]
        assert result.league == "CL"

    def test_wants_my_pattern(self):
        result = parse_consult_command("consult mike wants my CeeDee")
        assert result.partner_name == "mike"
        assert result.target_players == ["CeeDee"]
        # No leading league token in this message, so nothing selected a
        # league. Prose that mentions one is not a selection.
        assert result.league is None

    def test_empty_raises(self):
        with pytest.raises(ConsultParseError, match="Empty"):
            parse_consult_command("")

    def test_no_wants_falls_to_claude(self):
        """If structured parse fails, falls to Claude NL parse."""
        with patch("trade_scout.consult.subprocess.run", side_effect=FileNotFoundError):
            with pytest.raises(ConsultParseError, match="Couldn't understand"):
                parse_consult_command("/consult something weird")

    def test_league_code_cl(self):
        result = parse_consult_command("/consult CL TestUser wants Player X")
        assert result.league == "CL"

    def test_league_code_not_prefix_of_name(self):
        """'CL' only matches as a whole token, so 'Clayton' names no league."""
        result = parse_consult_command("/consult Clayton wants Bijan")
        assert result.partner_name == "Clayton"
        assert result.league is None

    def test_multi_paragraph_with_give_him(self):
        """Real-world: context paste + ask at the end."""
        msg = (
            "/consult \n\n"
            "Theonb sent me this in my CL league: what do you think "
            "about doing a trade where you shift some of your positional "
            "value?\n\n"
            "Consult me on trades I could do with theonb "
            "if I give him Caleb Williams"
        )
        result = parse_consult_command(msg)
        assert result.partner_name == "theonb"
        assert result.target_players == ["Caleb Williams"]
        # No leading league token in this message, so nothing selected a
        # league. Prose that mentions one is not a selection.
        assert result.league is None

    def test_league_mentioned_in_body_is_NOT_used(self):
        """Prose is never scanned. Only a leading token counts.

        This test previously asserted the opposite. The scan it protected also
        read a trade PARTNER's pasted words as the owner's league selection,
        and read "DD not CL" as CL.
        """
        msg = "/consult\nMike sent this in my CL league\nMike wants Bijan"
        result = parse_consult_command(msg)
        assert result.league is None

    def test_if_i_give_him_single_line(self):
        msg = "/consult trades I could do with Dave if I give him Lamar"
        result = parse_consult_command(msg)
        assert result.partner_name == "Dave"
        assert result.target_players == ["Lamar"]

    def test_if_i_trade_him_pattern(self):
        msg = "consult: deals I could do with BigNick if I trade him CeeDee"
        result = parse_consult_command(msg)
        assert result.partner_name == "BigNick"
        assert result.target_players == ["CeeDee"]

    def test_multiple_target_players_and(self):
        """Multiple players separated by 'and'."""
        msg = "/consult trades with Mike if I give him RJ Harvey and Caleb Williams"
        result = parse_consult_command(msg)
        assert result.partner_name == "Mike"
        assert result.target_players == ["RJ Harvey", "Caleb Williams"]
        assert result.target_display == "RJ Harvey, Caleb Williams"

    def test_multiple_target_players_comma(self):
        """Multiple players separated by comma."""
        msg = "/consult Mike wants my Bijan, 2026 1st"
        result = parse_consult_command(msg)
        assert result.partner_name == "Mike"
        assert result.target_players == ["Bijan", "2026 1st"]

    def test_multiple_targets_multi_paragraph(self):
        """Real-world multi-paragraph with multiple give players."""
        msg = (
            "/consult\n\n"
            "What should I get back from NinjaChristian "
            "if I give him RJ Harvey and Caleb Williams?"
        )
        result = parse_consult_command(msg)
        assert "RJ Harvey" in result.target_players
        assert "Caleb Williams" in result.target_players

    def test_target_player_property_returns_first(self):
        result = parse_consult_command(
            "/consult trades with Dave if I give him RJ Harvey and Caleb"
        )
        assert result.target_player == "RJ Harvey"


class TestEnrichRoster:
    def _sample_players_map(self):
        return {
            "p1": {"full_name": "Player One", "position": "RB", "age": 24},
            "p2": {"full_name": "Player Two", "position": "WR", "age": 26},
            "p3": {"full_name": "Player Three", "position": "QB", "age": 23},
            "p4": {"full_name": "Player Four", "position": "TE", "age": 28},
        }

    def _sample_values(self):
        return {
            "p1": {"value": 9000, "overall_rank": 1, "source": "FantasyCalc"},
            "p2": {"value": 7000, "overall_rank": 5, "source": "FantasyCalc"},
            "p3": {"value": 6000, "overall_rank": 8, "source": "FantasyCalc"},
            "p4": {"value": 2000, "overall_rank": 30, "source": "FantasyCalc"},
        }

    def test_returns_sorted_by_value(self):
        result = _enrich_roster(
            ["p1", "p2", "p3", "p4"],
            self._sample_players_map(),
            self._sample_values(),
            {},
        )
        assert len(result) == 4
        assert result[0]["name"] == "Player One"
        assert result[0]["value"] == 9000
        assert result[1]["name"] == "Player Two"

    def test_top_n_limit(self):
        result = _enrich_roster(
            ["p1", "p2", "p3", "p4"],
            self._sample_players_map(),
            self._sample_values(),
            {},
            top_n=2,
        )
        assert len(result) == 2
        assert result[0]["value"] == 9000
        assert result[1]["value"] == 7000

    def test_includes_position_and_age(self):
        result = _enrich_roster(
            ["p1"],
            self._sample_players_map(),
            self._sample_values(),
            {},
        )
        assert result[0]["position"] == "RB"
        assert result[0]["age"] == 24

    def test_flock_enrichment(self):
        flock = {
            "player one": {"flock_rank": 3.5, "flock_tier": "S"},
        }
        result = _enrich_roster(
            ["p1"],
            self._sample_players_map(),
            self._sample_values(),
            flock,
        )
        assert result[0]["flock_rank"] == 3.5
        assert result[0]["flock_tier"] == "S"

    def test_missing_player_in_map(self):
        result = _enrich_roster(
            ["unknown_id"],
            self._sample_players_map(),
            self._sample_values(),
            {},
        )
        assert len(result) == 1
        assert result[0]["name"] == "ID:unknown_id"
        assert result[0]["value"] == 0

    def test_correct_shape(self):
        result = _enrich_roster(
            ["p1"],
            self._sample_players_map(),
            self._sample_values(),
            {},
        )
        entry = result[0]
        assert "name" in entry
        assert "position" in entry
        assert "value" in entry


class TestFindPartnerRoster:
    def _make_sleeper_stub(self, users, rosters):
        class Stub:
            def find_user_by_name(self, name):
                for u in users:
                    if u["display_name"].lower() == name.lower():
                        return u
                return None

            def find_user_roster(self, owner_id):
                for r in rosters:
                    if r["owner_id"] == owner_id:
                        return r
                return None

            def get_users(self):
                return users

        return Stub()

    def test_exact_match(self):
        users = [{"user_id": "u1", "display_name": "MikeJones"}]
        rosters = [{"owner_id": "u1", "players": ["p1", "p2"]}]
        sleeper = self._make_sleeper_stub(users, rosters)
        result = _find_partner_roster(sleeper, "MikeJones")
        assert result is not None
        name, pids = result
        assert name == "MikeJones"
        assert pids == ["p1", "p2"]

    def test_partial_match(self):
        users = [{"user_id": "u1", "display_name": "MikeJones23"}]
        rosters = [{"owner_id": "u1", "players": ["p1"]}]
        sleeper = self._make_sleeper_stub(users, rosters)
        result = _find_partner_roster(sleeper, "Mike")
        assert result is not None
        name, _ = result
        assert name == "MikeJones23"

    def test_not_found(self):
        users = [{"user_id": "u1", "display_name": "Dave"}]
        rosters = [{"owner_id": "u1", "players": ["p1"]}]
        sleeper = self._make_sleeper_stub(users, rosters)
        result = _find_partner_roster(sleeper, "ZZZ_NonExistent")
        assert result is None

    def test_case_insensitive_partial(self):
        users = [{"user_id": "u1", "display_name": "BigNick99"}]
        rosters = [{"owner_id": "u1", "players": ["p1"]}]
        sleeper = self._make_sleeper_stub(users, rosters)
        result = _find_partner_roster(sleeper, "bignick")
        assert result is not None
