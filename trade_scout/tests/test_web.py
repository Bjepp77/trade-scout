"""The web UI, hermetically: providers and Sleeper are patched at their seams.

The suite-wide no-network guard stays on; these tests prove the app's
patching seams are real (if a route slipped past them, httpx would explode).
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from trade_scout.config import AppConfig, LeagueEntry
from trade_scout.providers.base import LeagueFormat, SourceSnapshot
from trade_scout.web.app import create_app

FMT = LeagueFormat(superflex=True)

PLAYERS_MAP = {
    "4034": {"first_name": "Christian", "last_name": "McCaffrey",
             "full_name": "Christian McCaffrey", "position": "RB", "age": 30},
    "9509": {"first_name": "Bijan", "last_name": "Robinson",
             "full_name": "Bijan Robinson", "position": "RB", "age": 24},
}

LEAGUE_SETTINGS = {"num_qbs": 2, "num_teams": 12, "ppr": 1,
                   "format_str": "SF / 12-team / PPR"}


@pytest.fixture
def client():
    return TestClient(create_app(), follow_redirects=False)


@pytest.fixture
def configured(tmp_path, monkeypatch):
    """A saved config with one league, isolated to this test."""
    monkeypatch.setenv("TRADE_SCOUT_CONFIG", str(tmp_path / "config.json"))
    cfg = AppConfig()
    cfg.sleeper_username = "TestUser"
    cfg.leagues = [LeagueEntry(short="TL", league_id="123", name="Test League",
                               owner_display_name="TestUser")]
    cfg.default_league = "TL"
    cfg.save()
    return cfg


def fake_sleeper_client(*a, **k):
    m = MagicMock()
    m.get_league_settings.return_value = dict(LEAGUE_SETTINGS)
    m.get_players_map.return_value = PLAYERS_MAP
    return m


SNAPS = {
    "a": SourceSnapshot(source="a", display_name="A", as_of="2026-08-24",
                        fmt=FMT, join="sleeper_id",
                        players={"4034": {"value": 1000},
                                 "9509": {"value": 2000}}),
}


class TestSetupFlow:
    def test_home_redirects_to_setup_when_unconfigured(self, client):
        r = client.get("/")
        assert r.status_code == 303 and r.headers["location"] == "/setup"

    def test_lookup_and_save(self, client, tmp_path, monkeypatch):
        monkeypatch.setenv("TRADE_SCOUT_CONFIG", str(tmp_path / "config.json"))
        with patch("trade_scout.sleeper.get_user",
                   return_value={"user_id": "u1", "display_name": "TestUser"}), \
             patch("trade_scout.sleeper.get_user_leagues",
                   return_value=[{"league_id": "123", "name": "Test League"}]):
            r = client.post("/setup/lookup", data={"username": "TestUser"})
        assert r.status_code == 200 and "Test League" in r.text

        r = client.post("/setup/save", data={
            "username": "TestUser", "display_name": "TestUser",
            "league_id": "123", "name_123": "Test League", "short_123": "TL"})
        assert r.status_code == 303
        saved = json.loads((tmp_path / "config.json").read_text())
        assert saved["leagues"][0]["short"] == "TL"
        assert saved["default_league"] == "TL"

    def test_home_redirects_to_default_league_when_configured(
            self, client, configured):
        r = client.get("/")
        assert r.status_code == 303
        assert r.headers["location"] == "/league/TL"


class TestTradeEval:
    def test_trade_post_renders_source_rows_and_consensus(
            self, client, configured):
        with patch("trade_scout.web.app.get_league_config",
                   return_value={"league_id": "123", "display_name": "TestUser",
                                 "league_name": "Test League", "short": "TL"}), \
             patch("trade_scout.sleeper.SleeperClient",
                   side_effect=fake_sleeper_client), \
             patch("trade_scout.engine.evaluate.get_snapshots",
                   return_value=(SNAPS, {"ktc": "page changed"})), \
             patch("trade_scout.engine.evaluate._try_flock_rankings",
                   return_value={}), \
             patch("trade_scout.engine.evaluate.build_roster_context",
                   return_value=None):
            r = client.post("/league/TL/trade", data={
                "give": "Christian McCaffrey", "get": "Bijan Robinson"})
        assert r.status_code == 200
        assert "Per-source verdicts" in r.text
        assert "unavailable: page changed" in r.text
        assert "WIN" in r.text
        assert "Consensus" in r.text or "wins under" in r.text

    def test_trade_post_with_unknown_league_redirects_to_setup(self, client):
        r = client.post("/league/ZZ/trade", data={"give": "A", "get": "B"})
        assert r.status_code == 303 and r.headers["location"] == "/setup"

    def test_parse_refusal_is_shown_not_crashed(self, client, configured):
        r = client.post("/league/TL/trade", data={"give": "", "get": "X"})
        assert r.status_code == 200 and "required" in r.text


class TestConsultJob:
    def test_consult_runs_as_a_job_and_result_is_polled(
            self, client, configured):
        def fake_run_consult(chat_id, text, explicit=None):
            from trade_scout.delivery import send_message
            assert explicit == "TL"
            send_message(chat_id, "OPTION ONE: give X get Y")

        with patch("trade_scout.consult.run_consult",
                   side_effect=fake_run_consult):
            r = client.post("/league/TL/consult", data={"text": "Mike wants X"})
            assert r.status_code == 200 and "/jobs/" in r.text
            job_id = r.text.split("/jobs/")[1].split('"')[0]

            # The single-worker executor has finished by the time we poll
            # (TestClient runs the thread pool for real).
            import time
            for _ in range(50):
                r = client.get(f"/jobs/{job_id}")
                if "OPTION ONE" in r.text:
                    break
                time.sleep(0.05)
        assert "OPTION ONE" in r.text


class TestSources:
    def test_sources_page_shows_live_failed_and_disabled(
            self, client, configured, monkeypatch):
        with patch("trade_scout.sleeper.SleeperClient",
                   side_effect=fake_sleeper_client), \
             patch("trade_scout.values.get_snapshots",
                   return_value=(SNAPS, {"ktc": "boom"})):
            r = client.get("/sources")
        assert r.status_code == 200
        assert "disabled" in r.text     # dynastydaddy default-off
        assert "not configured" in r.text  # flock creds absent in tests
