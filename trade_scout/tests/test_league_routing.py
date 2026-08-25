"""Contract: the league the CALLER selected is the league that gets priced.

These enter at run()'s env seam, NOT at parse_trade_command. That distinction
is the whole point. `test_default_league_is_dd` asserts that text with no token
parses as DD, and it is correct: the text genuinely names no league. The defect
was that nothing above it ever supplied one, so a test one layer lower passes
forever while every console trade is mispriced.

The literal `TRADE_SCOUT_LEAGUE` is pinned here and in the satellite. Either
side alone is half a contract.

Historic failure this locks down: the console league chip reached the satellite
and the transcript but never the engine. A trade built entirely on the CL board
was priced against the DD roster and journaled as CL. The verdict named three
players who were not on the CL roster.
"""

from unittest.mock import MagicMock, patch

import pytest

from trade_scout.config import explicit_league as _explicit_league
from trade_scout.trade import parse_trade_command

# Synthetic. This suite tests ROUTING -- that a request tagged CL reaches the
# CL id and never the DD one -- which real ids would not test any better, and
# a test file is the wrong place to pin a live league to.
DD_ID = "1000000000000000001"
CL_ID = "2000000000000000002"


@pytest.fixture(autouse=True)
def env(monkeypatch):
    monkeypatch.setenv("SLEEPER_LEAGUE_ID", DD_ID)
    monkeypatch.setenv("SLEEPER_LEAGUE_CL", CL_ID)
    monkeypatch.delenv("TRADE_SCOUT_LEAGUE", raising=False)
    return monkeypatch


class TestExplicitLeague:
    def test_absent_is_none_not_a_default(self, env):
        # None, never "DD". A default that equals a legal value makes "it chose
        # DD" indistinguishable from "it fell through".
        assert _explicit_league() is None

    def test_empty_is_none(self, env):
        env.setenv("TRADE_SCOUT_LEAGUE", "")
        assert _explicit_league() is None

    def test_whitespace_is_none(self, env):
        env.setenv("TRADE_SCOUT_LEAGUE", "   ")
        assert _explicit_league() is None

    def test_is_case_insensitive(self, env):
        env.setenv("TRADE_SCOUT_LEAGUE", "cl")
        assert _explicit_league() == "CL"


class TestParsedTradeCarriesWhetherTextNamedALeague:
    def test_no_token_is_not_explicit(self):
        t = parse_trade_command("/trade give: Bijan Robinson | get: Puka Nacua")
        assert t.league is None             # no league named, and it says so
        assert t.league_explicit is False

    def test_a_token_is_explicit(self):
        t = parse_trade_command("/trade CL give: Bijan Robinson | get: Puka Nacua")
        assert t.league == "CL"
        assert t.league_explicit is True


class TestTheDisagreeingCells:
    """Only the two cells that disagree. The agreeing cells pass today for the
    wrong reason, because the default happens to equal one of the legal values."""

    def _run(self, env, text, selector):
        if selector:
            env.setenv("TRADE_SCOUT_LEAGUE", selector)
        env.setenv("TEXT", text)
        env.setenv("CHAT_ID", "test")
        env.setenv("TRADE_SCOUT_LOCAL", "1")
        seen = {}

        def fake_client(league_id=None, *a, **k):
            # Record and hand back a mock. Raising a sentinel here fights run()'s
            # own RuntimeError handler; what this test asserts is the CAPTURED
            # id, not that the run completed.
            seen["league_id"] = league_id
            return MagicMock()

        # compute_trade_math is stubbed out because it reaches FantasyCalc over
        # the network, which this test does not care about and the suite-wide
        # no-network guard now refuses. The league id is already captured by
        # the time it would run. cli.process() imports its collaborators at
        # call time from their home modules, so the patches target the source.
        with patch("trade_scout.sleeper.SleeperClient", side_effect=fake_client), \
                patch("trade_scout.report.check_claude_auth", return_value=True), \
                patch("trade_scout.trade.compute_trade_math",
                      side_effect=RuntimeError("stop: the league id is captured")), \
                patch("trade_scout.delivery.send_message", MagicMock()) as sent:
            from trade_scout.cli import _cmd_run
            # The fake client raises to stop the run the moment the league id
            # is known; everything past that point is Sleeper I/O this test
            # does not care about.
            try:
                _cmd_run()
            except Exception:
                pass
        return seen, sent

    def test_selector_CL_with_no_token_in_text_prices_CL(self, env):
        # THE REGRESSION. This is the exact shape the board emits, and before
        # the fix it resolved to DD.
        seen, _ = self._run(env, "give: Bijan Robinson | get: Puka Nacua", "CL")
        assert seen.get("league_id") == CL_ID

    def test_selector_DD_with_no_token_in_text_prices_DD(self, env):
        seen, _ = self._run(env, "give: Bijan Robinson | get: Puka Nacua", "DD")
        assert seen.get("league_id") == DD_ID

    def test_selector_and_text_disagreeing_is_refused_not_resolved(self, env):
        # A selector that silently loses to text is the same bug facing the
        # other way, so neither wins: it refuses and names both.
        seen, sent = self._run(env, "CL give: Bijan Robinson | get: Puka Nacua", "DD")
        assert "league_id" not in seen, "it priced something despite the conflict"
        msg = " ".join(str(c) for c in sent.call_args_list)
        assert "conflict" in msg.lower()
        assert "DD" in msg and "CL" in msg

    def test_no_selector_still_honours_an_explicit_text_token(self, env):
        # A text-only caller has no selector and must keep working.
        seen, _ = self._run(env, "CL give: Bijan Robinson | get: Puka Nacua", None)
        assert seen.get("league_id") == CL_ID
