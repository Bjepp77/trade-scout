"""Tests for the Sleeper API client, HTTP mocked."""

from unittest.mock import patch

import pytest

from trade_scout.sleeper import SleeperClient


def _mock_league_data():
    """Realistic Sleeper league response shape."""
    return {
        "league_id": "2000000000000000002",
        "total_rosters": 12,
        "roster_positions": [
            "QB", "RB", "RB", "WR", "WR", "TE", "SUPER_FLEX", "FLEX",
            "FLEX", "FLEX", "BN", "BN", "BN", "BN", "BN", "BN", "BN",
            "BN", "BN", "BN", "BN", "BN", "BN", "IR", "IR",
        ],
        "scoring_settings": {"rec": 1.0, "pass_td": 4.0, "rush_yd": 0.1},
        "season": "2026",
    }


class TestGetLeagueSettings:
    @patch.object(SleeperClient, "_get")
    def test_superflex_detected(self, mock_get):
        mock_get.return_value = _mock_league_data()
        client = SleeperClient(league_id="test123")
        settings = client.get_league_settings()

        assert settings["num_qbs"] == 2  # SUPER_FLEX present
        assert settings["num_teams"] == 12
        assert settings["ppr"] == 1.0
        assert settings["is_dynasty"] is True

    @patch.object(SleeperClient, "_get")
    def test_1qb_league(self, mock_get):
        data = _mock_league_data()
        data["roster_positions"] = ["QB", "RB", "RB", "WR", "WR", "TE", "FLEX"]
        mock_get.return_value = data
        client = SleeperClient(league_id="test123")
        settings = client.get_league_settings()

        assert settings["num_qbs"] == 1

    @patch.object(SleeperClient, "_get")
    def test_half_ppr(self, mock_get):
        data = _mock_league_data()
        data["scoring_settings"]["rec"] = 0.5
        mock_get.return_value = data
        client = SleeperClient(league_id="test123")
        settings = client.get_league_settings()

        assert settings["ppr"] == 0.5

    @patch.object(SleeperClient, "_get")
    def test_two_qb_slots(self, mock_get):
        data = _mock_league_data()
        data["roster_positions"] = ["QB", "QB", "RB", "RB", "WR", "WR", "TE", "FLEX"]
        mock_get.return_value = data
        client = SleeperClient(league_id="test123")
        settings = client.get_league_settings()

        assert settings["num_qbs"] == 2


class TestGetRosters:
    @patch.object(SleeperClient, "_get")
    def test_returns_list(self, mock_get):
        mock_get.return_value = [
            {"roster_id": 1, "owner_id": "user1", "players": ["123", "456"]},
            {"roster_id": 2, "owner_id": "user2", "players": ["789"]},
        ]
        client = SleeperClient(league_id="test123")
        rosters = client.get_rosters()
        assert len(rosters) == 2


class TestGetPlayersMap:
    @pytest.fixture(autouse=True)
    def _no_disk_cache(self, monkeypatch):
        # The players map is cached to disk for a day. Without this the unit
        # tests read whatever 15 MB file happens to be on the developer's
        # machine, and "does it fetch once" stops being a question about the
        # code.
        monkeypatch.setenv("TRADE_SCOUT_CACHE_DIR", "")

    @patch.object(SleeperClient, "_get")
    def test_caches_players(self, mock_get):
        mock_get.return_value = {"123": {"first_name": "Bijan", "last_name": "Robinson"}}
        client = SleeperClient(league_id="test123")
        p1 = client.get_players_map()
        p2 = client.get_players_map()
        assert p1 is p2  # Same object, cached
        assert mock_get.call_count == 1  # Only fetched once

    @patch.object(SleeperClient, "_get")
    def test_the_disk_cache_survives_a_new_client(self, mock_get, tmp_path, monkeypatch):
        # The reason this cache exists: every request is a fresh PROCESS, so an
        # in-process cache saved nothing and the 14.6 MB map was pulled every
        # time.
        monkeypatch.setenv("TRADE_SCOUT_CACHE_DIR", str(tmp_path))
        mock_get.return_value = {"123": {"full_name": "Bijan Robinson"}}
        first = SleeperClient(league_id="L").get_players_map()
        second = SleeperClient(league_id="L").get_players_map()
        assert first == second
        assert mock_get.call_count == 1, "the second client refetched 14.6 MB"

    @patch.object(SleeperClient, "_get")
    def test_a_stale_cache_is_refetched(self, mock_get, tmp_path, monkeypatch):
        import os
        import time
        monkeypatch.setenv("TRADE_SCOUT_CACHE_DIR", str(tmp_path))
        mock_get.return_value = {"123": {"full_name": "Bijan Robinson"}}
        SleeperClient(league_id="L").get_players_map()
        f = tmp_path / "sleeper-players.json"
        old = time.time() - (30 * 3600)
        os.utime(f, (old, old))
        SleeperClient(league_id="L").get_players_map()
        assert mock_get.call_count == 2

    @patch.object(SleeperClient, "_get")
    def test_a_corrupt_cache_refetches_rather_than_failing(self, mock_get, tmp_path, monkeypatch):
        # A cache that can break the caller is worse than no cache.
        monkeypatch.setenv("TRADE_SCOUT_CACHE_DIR", str(tmp_path))
        (tmp_path / "sleeper-players.json").write_text("{not json")
        mock_get.return_value = {"123": {"full_name": "Bijan Robinson"}}
        got = SleeperClient(league_id="L").get_players_map()
        assert got == {"123": {"full_name": "Bijan Robinson"}}
