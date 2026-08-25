"""Tests for dated value snapshots.

The whole point of this module is that it accrues quietly for months and is
then trusted. That is the worst possible failure profile: nobody looks at it
until the day they need six months of it, and by then a silent bug has cost
data that cannot be recovered. So the tests care most about (a) it never breaks
its caller and (b) a day that was written is readable back exactly.
"""

import json
import pathlib

import pytest

from trade_scout import history


@pytest.fixture(autouse=True)
def _tmp_history(tmp_path, monkeypatch):
    monkeypatch.setenv("TRADE_SCOUT_HISTORY_DIR", str(tmp_path / "h"))


ROWS = [{"id": "1", "name": "Josh Allen", "pos": "QB", "value": 9000, "rank": 1},
        {"id": "2", "name": "Bijan Robinson", "pos": "RB", "value": 8000, "rank": 2}]


class TestRoundTrip:
    def test_a_written_day_reads_back_identically(self):
        assert history.record("fc", ROWS, day="2026-08-06") == 2
        assert history.read_day("fc", "2026-08-06") == ROWS

    def test_days_are_kept_separately(self):
        history.record("fc", ROWS[:1], day="2026-08-06")
        history.record("fc", ROWS, day="2026-08-07")
        assert len(history.read_day("fc", "2026-08-06")) == 1
        assert len(history.read_day("fc", "2026-08-07")) == 2

    def test_sources_are_kept_separately(self):
        history.record("fc", ROWS, day="2026-08-06")
        history.record("ktc", ROWS[:1], day="2026-08-06")
        assert len(history.read_day("fc", "2026-08-06")) == 2
        assert len(history.read_day("ktc", "2026-08-06")) == 1

    def test_available_days_is_the_backtest_index(self):
        for d in ("2026-08-07", "2026-08-05", "2026-08-06"):
            history.record("fc", ROWS, day=d)
        assert history.available_days("fc") == ["2026-08-05", "2026-08-06", "2026-08-07"]

    def test_an_absent_day_is_empty_not_an_error(self):
        assert history.read_day("fc", "1999-01-01") == []

    def test_an_unrecorded_source_has_no_days(self):
        assert history.available_days("nope") == []


class TestItCannotBreakItsCaller:
    """A snapshot is a nice-to-have. A board build must never fail for one."""

    def test_recording_to_an_unwritable_place_returns_zero(self, monkeypatch):
        monkeypatch.setenv("TRADE_SCOUT_HISTORY_DIR", "/proc/nope/cannot/write")
        assert history.record("fc", ROWS, day="2026-08-06") == 0

    def test_disabled_recording_is_silent(self, monkeypatch):
        monkeypatch.setenv("TRADE_SCOUT_HISTORY_DIR", "")
        assert history.record("fc", ROWS, day="2026-08-06") == 0
        assert history.read_day("fc", "2026-08-06") == []
        assert history.available_days("fc") == []

    def test_unserialisable_rows_do_not_raise(self):
        assert history.record("fc", [{"bad": object()}], day="2026-08-06") == 0

    def test_a_junk_source_name_does_not_escape_the_directory(self):
        history.record("../../etc", ROWS, day="2026-08-06")
        # Sanitised to "etc", so nothing is written outside the history dir.
        assert history.read_day("etc", "2026-08-06") == ROWS

    def test_a_source_of_only_junk_is_refused_not_written_to_the_root(self):
        assert history.record("../..", ROWS, day="2026-08-06") == 0

    def test_empty_and_non_list_input_is_a_no_op(self):
        assert history.record("fc", [], day="2026-08-06") == 0
        assert history.record("fc", None, day="2026-08-06") == 0
        assert history.record("fc", {"not": "a list"}, day="2026-08-06") == 0

    def test_a_runaway_row_count_is_refused(self):
        big = [{"id": str(i)} for i in range(history.MAX_ROWS + 1)]
        assert history.record("fc", big, day="2026-08-06") == 0


class TestDurability:
    def test_rewriting_a_day_replaces_it_rather_than_appending(self):
        # The satellite refreshes hourly. 24 copies of one day is noise.
        history.record("fc", ROWS, day="2026-08-06")
        history.record("fc", ROWS, day="2026-08-06")
        assert len(history.read_day("fc", "2026-08-06")) == 2

    def test_no_temp_file_is_left_behind(self):
        history.record("fc", ROWS, day="2026-08-06")
        d = history.snapshot_path("fc", "2026-08-06").parent
        assert not list(d.glob("*.tmp"))

    def test_one_corrupt_line_does_not_discard_the_whole_day(self):
        history.record("fc", ROWS, day="2026-08-06")
        p = history.snapshot_path("fc", "2026-08-06")
        p.write_text(p.read_text() + "{not json\n", encoding="utf-8")
        assert len(history.read_day("fc", "2026-08-06")) == 2

    def test_rows_are_written_one_per_line(self):
        history.record("fc", ROWS, day="2026-08-06")
        text = history.snapshot_path("fc", "2026-08-06").read_text()
        assert len(text.strip().splitlines()) == 2
        for line in text.strip().splitlines():
            json.loads(line)


class TestShrink:
    def test_market_keeps_only_what_a_backtest_reads(self):
        rows = history.shrink_market(
            {"1": {"name": "Josh Allen", "position": "QB", "value": 9000,
                   "overall_rank": 1, "trend30": -50, "junk": "x"}})
        assert rows == [{"id": "1", "name": "Josh Allen", "pos": "QB",
                         "value": 9000, "rank": 1}]

    def test_ktc_is_recorded_under_its_normalised_name_key(self):
        rows = history.shrink_ktc(
            {"josh allen": {"position": "QB", "value": 9500, "rank": 1}})
        assert rows == [{"name": "josh allen", "pos": "QB",
                         "value": 9500, "rank": 1}]

    def test_flock_records_rank_and_never_invents_a_value(self):
        rows = history.shrink_flock(
            {"josh allen": {"flock_rank": 2.13, "flock_tier": "S", "position": "QB"}})
        assert rows == [{"name": "josh allen", "pos": "QB",
                         "rank": 2.13, "tier": "S"}]
        assert "value" not in rows[0]

    def test_junk_entries_are_skipped_not_crashed_on(self):
        assert history.shrink_market({"1": None, "2": "nope"}) == []
        assert history.shrink_ktc({"a": None}) == []
        assert history.shrink_flock({"a": None}) == []

    def test_none_input_is_empty(self):
        assert history.shrink_market(None) == []
        assert history.shrink_ktc(None) == []
        assert history.shrink_flock(None) == []


class TestConcurrentWriters:
    """Every request is a fresh PROCESS, so two writers for one day is normal.

    A shared ".tmp" name lets a bot request and a satellite refresh open the
    same inode, write at overlapping offsets, and then rename it into place.
    The rename is atomic; the contents are not.

    The tear does not look like damage. Measured against the shared-name
    version (tools/tear_probe.py, 3 of 3 trials torn, 3 of 3 clean after the
    fix) the resulting file had the CORRECT row count and a mix of both
    writers' rows, so a length check calls it healthy.

    This test pins the MECHANISM rather than racing for the symptom. A
    thread-based race test passed against the bug (buffered writes never
    interleaved) and a spawn-based one passed because the children died on
    startup and it asserted against an empty directory. Both were vacuous. The
    probe script reproduces the real race; this keeps the suite honest and fast.
    """

    def test_each_writer_gets_its_own_temp_path(self, monkeypatch):
        import tempfile as _tf

        seen = []
        real = _tf.mkstemp

        def spy(*a, **k):
            fd, name = real(*a, **k)
            seen.append(name)
            return fd, name

        monkeypatch.setattr(history.tempfile, "mkstemp", spy)
        history.record("fc", ROWS, day="2026-08-06")
        history.record("fc", ROWS, day="2026-08-06")

        assert len(seen) == 2, "record() did not allocate a unique temp file"
        assert len(set(seen)) == 2, "two writers shared one temp path"

    def test_the_temp_file_lands_beside_its_target(self, monkeypatch):
        # Same filesystem, or the rename stops being atomic and becomes a copy.
        import tempfile as _tf

        seen = []
        real = _tf.mkstemp

        def spy(*a, **k):
            fd, name = real(*a, **k)
            seen.append(name)
            return fd, name

        monkeypatch.setattr(history.tempfile, "mkstemp", spy)
        history.record("fc", ROWS, day="2026-08-06")
        target = history.snapshot_path("fc", "2026-08-06")
        assert pathlib.Path(seen[0]).parent == target.parent
