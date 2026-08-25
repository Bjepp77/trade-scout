"""LLM optionality: everything essential works with no `claude` CLI."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from trade_scout import llm
from trade_scout.consult import ConsultParseError, parse_consult_command
from trade_scout.report import ClaudeVerdictError
from trade_scout.trade import TradeParseError, parse_trade_command


class TestLlmAvailable:
    def test_env_off_wins_without_probing(self, monkeypatch):
        monkeypatch.setenv("TRADE_SCOUT_LLM", "off")
        llm.reset()
        with patch("shutil.which") as which:
            assert llm.llm_available() is False
            which.assert_not_called()

    def test_missing_cli_means_no(self, monkeypatch):
        monkeypatch.setenv("TRADE_SCOUT_LLM", "auto")
        llm.reset()
        with patch("shutil.which", return_value=None):
            assert llm.llm_available() is False

    def test_present_and_authed_means_yes_and_is_memoised(self, monkeypatch):
        monkeypatch.setenv("TRADE_SCOUT_LLM", "auto")
        llm.reset()
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("trade_scout.report.check_claude_auth",
                   return_value=True) as probe:
            assert llm.llm_available() is True
            assert llm.llm_available() is True
            assert probe.call_count == 1   # memoised

    def test_present_but_unauthenticated_means_no(self, monkeypatch):
        from trade_scout.report import ClaudeAuthError
        monkeypatch.setenv("TRADE_SCOUT_LLM", "auto")
        llm.reset()
        with patch("shutil.which", return_value="/usr/bin/claude"), \
             patch("trade_scout.report.check_claude_auth",
                   side_effect=ClaudeAuthError("not logged in")):
            assert llm.llm_available() is False


class TestDegradation:
    """conftest forces TRADE_SCOUT_LLM=off: this is the no-CLI world."""

    def test_structured_trade_parses_without_llm(self):
        t = parse_trade_command("/trade give: CMC | get: Bijan Robinson")
        assert t.give == ["CMC"]

    def test_nl_trade_degrades_to_a_grammar_hint(self):
        with pytest.raises(TradeParseError, match="give: Player1"):
            parse_trade_command("what about my CMC for his Bijan")

    def test_nl_consult_degrades_to_a_grammar_hint(self):
        with pytest.raises(ConsultParseError, match="/consult"):
            parse_consult_command("thinking about moving some pieces around")

    def test_skeleton_generation_refuses_cleanly(self):
        from trade_scout.consult import ConsultRequest, generate_skeletons
        with pytest.raises(ClaudeVerdictError, match="no LLM"):
            generate_skeletons(
                ConsultRequest("Mike", ["Player"]), [], [], {}, {}, False)
