"""DynastyProcess CSV shaping, against small checked-in samples."""

from __future__ import annotations

from unittest.mock import patch

import pytest

from trade_scout.providers.base import LeagueFormat, SourceError
from trade_scout.providers.dynastyprocess import get_dynastyprocess_snapshot

VALUES_CSV = '''\
"player","pos","team","age","draft_year","ecr_1qb","ecr_2qb","ecr_pos","value_1qb","value_2qb","scrape_date","fp_id"
"Ja'Marr Chase","WR","CIN",26.5,2021,1,6.3,1,10256,9055,"2026-08-21","19788"
"Josh Allen","QB","BUF",30.2,2018,20,1,1,4500,11000,"2026-08-21","17298"
"2026 Pick 1.01","PICK",NA,NA,NA,18.2,10.61,NA,6846,8183,"2026-08-21",NA
"2027 Early 1st","PICK",NA,NA,NA,38.45,33.72,NA,4254,4753,"2026-08-21",NA
"Unknown Rookie","WR","FA",22.1,2026,300,300,99,50,40,"2026-08-21","99999"
"No Value Row","WR","FA",22.1,2026,NA,NA,NA,NA,NA,"2026-08-21","88888"
'''

IDS_CSV = '''mfl_id,fantasypros_id,sleeper_id,name,position
1001,19788,7564,Ja'Marr Chase,WR
1002,17298,4984,Josh Allen,QB
1003,55555,NA,Someone Retired,RB
'''


def _patch_csvs(values=VALUES_CSV, ids=IDS_CSV):
    def fake_fetch(url, cache_name, max_age):
        import csv
        import io
        raw = ids if "playerids" in cache_name else values
        return list(csv.DictReader(io.StringIO(raw)))
    return patch("trade_scout.providers.dynastyprocess._fetch_csv",
                 side_effect=fake_fetch)


class TestDynastyProcessShaping:
    def test_superflex_uses_value_2qb_and_joins_on_sleeper_id(self):
        with _patch_csvs():
            snap = get_dynastyprocess_snapshot(LeagueFormat(superflex=True))
        assert snap.join == "sleeper_id"
        assert snap.players["7564"]["value"] == 9055
        assert snap.players["4984"]["value"] == 11000
        assert snap.as_of == "2026-08-21"

    def test_one_qb_uses_value_1qb(self):
        with _patch_csvs():
            snap = get_dynastyprocess_snapshot(LeagueFormat(superflex=False))
        assert snap.players["4984"]["value"] == 4500

    def test_picks_land_on_canonical_keys(self):
        with _patch_csvs():
            snap = get_dynastyprocess_snapshot(LeagueFormat(superflex=True))
        # Canonical keys as produced by picks.PickRef.key()
        assert snap.picks["2026-1.01"] == 8183
        assert snap.picks["2027-1-early"] == 4753

    def test_rows_without_a_crosswalk_entry_are_counted_not_kept(self):
        with _patch_csvs():
            snap = get_dynastyprocess_snapshot(LeagueFormat(superflex=True))
        assert "99999" not in snap.players       # fp_id not in crosswalk
        assert snap.meta["unmatched_rows"] == 1  # Unknown Rookie only
        # The NA-value row is skipped before matching and counts nowhere.

    def test_stale_when_scrape_date_is_old(self):
        old = VALUES_CSV.replace("2026-08-21", "2026-06-01")
        with _patch_csvs(values=old):
            snap = get_dynastyprocess_snapshot(LeagueFormat(superflex=True))
        assert snap.stale is True

    def test_no_joinable_rows_is_an_error_not_an_empty_snapshot(self):
        empty_ids = "mfl_id,fantasypros_id,sleeper_id,name,position\n"
        with _patch_csvs(ids=empty_ids), pytest.raises(SourceError):
            get_dynastyprocess_snapshot(LeagueFormat(superflex=True))
