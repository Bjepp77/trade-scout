"""Dynasty Daddy response shaping, against a canned sample."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from trade_scout.providers.base import LeagueFormat, SourceError
from trade_scout.providers.dynastydaddy import get_dynastydaddy_snapshot

ROWS = [
    {"full_name": "Ja'Marr Chase", "position": "WR", "sleeper_id": "7564",
     "age": 26, "trade_value": 9995, "sf_trade_value": 9999,
     "overall_rank": 1, "sf_overall_rank": 2, "position_rank": 1,
     "sf_position_rank": 1, "date": "2026-08-24T08:04:46.875Z"},
    {"full_name": "2027 Early 1st", "position": "PI", "sleeper_id": None,
     "trade_value": 7316, "sf_trade_value": 6913,
     "date": "2026-08-24T08:04:46.875Z"},
    {"full_name": "George Pickens", "position": "WR", "sleeper_id": "8137",
     "age": 25, "trade_value": 6633, "sf_trade_value": 6246,
     "date": "2026-08-24T08:04:46.875Z"},
    {"full_name": "Retired Guy", "position": "RB", "sleeper_id": None,
     "trade_value": 100, "sf_trade_value": 90,
     "date": "2026-08-24T08:04:46.875Z"},
    {"full_name": "Zero Guy", "position": "RB", "sleeper_id": "42",
     "trade_value": 0, "sf_trade_value": 0,
     "date": "2026-08-24T08:04:46.875Z"},
]


def _patched(rows=ROWS):
    return patch("trade_scout.providers.dynastydaddy._fetch_rows",
                 return_value=rows)


class TestDynastyDaddyShaping:
    def test_superflex_uses_sf_trade_value(self):
        with _patched():
            snap = get_dynastydaddy_snapshot(LeagueFormat(superflex=True))
        assert snap.join == "sleeper_id"
        assert snap.players["7564"]["value"] == 9999
        assert snap.players["7564"]["overall_rank"] == 2

    def test_one_qb_uses_trade_value(self):
        with _patched():
            snap = get_dynastydaddy_snapshot(LeagueFormat(superflex=False))
        assert snap.players["7564"]["value"] == 9995
        assert snap.players["7564"]["overall_rank"] == 1

    def test_pi_rows_become_canonical_picks_not_players(self):
        # And a player whose NAME contains "pick" (George Pickens) stays a
        # player: the filter is the position code, never the name.
        with _patched():
            snap = get_dynastydaddy_snapshot(LeagueFormat(superflex=True))
        assert snap.picks["2027-1-early"] == 6913
        assert snap.players["8137"]["value"] == 6246

    def test_rows_without_sleeper_id_or_value_are_dropped_and_counted(self):
        with _patched():
            snap = get_dynastydaddy_snapshot(LeagueFormat(superflex=True))
        assert "42" not in snap.players           # zero value
        assert snap.meta["unmatched_rows"] == 1   # Retired Guy only

    def test_staleness_follows_the_data_date(self):
        old = [dict(r, date="2020-01-01T00:00:00.000Z") for r in ROWS]
        with _patched(rows=old):
            snap = get_dynastydaddy_snapshot(LeagueFormat(superflex=True))
        assert snap.stale is True
        assert snap.as_of == "2020-01-01"

    def test_shape_change_is_an_error_not_an_empty_snapshot(self):
        with _patched(rows=[{"totally": "different"}]), \
             pytest.raises(SourceError):
            get_dynastydaddy_snapshot(LeagueFormat(superflex=True))
