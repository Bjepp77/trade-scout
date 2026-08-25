"""Dated value snapshots, so the model can eventually be backtested.

The model's weights are currently unfalsifiable. Not because the method is
hard, but because the only real ground truth for "was this valuation right" is
what the market said MONTHS LATER, and nothing was recording. The disk cache is
a rolling 30-minute overwrite; the moment it refreshes, the previous state is
gone forever. Every day without this file is a day of history that cannot be
recovered at any price.

So this records one compact row per player per source per day. Not the full
provider payload: FantasyCalc alone is 400 KB, which is 146 MB a year to keep
fields no backtest will read. Name, position, value and rank are what answers
"did the composite at time T beat raw FantasyCalc at T at predicting price at
T+6mo", and that is about 15 KB per source per day.

Deliberately dumb, and deliberately incapable of breaking its caller, for the
same reason cache.py is: a snapshot is a nice-to-have, and a board build must
never fail because a write failed. Every path here swallows and logs.

Idempotent per day. The satellite refreshes hourly, and 24 copies of one day is
noise, not resolution. The last write of a day wins.
"""

from __future__ import annotations

import json
import logging
import os
import tempfile
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

# Not inside cache/. That directory is a rolling overwrite that anything is
# free to delete to force a refetch, which is the exact opposite of what an
# append-only history needs.
DEFAULT_DIR = Path.home() / ".trade-scout" / "history"

# Guards a runaway caller from filling the disk. A source that suddenly wants
# to write 100k rows has broken, not grown.
MAX_ROWS = 20000


class HistoryDisabled(Exception):
    """TRADE_SCOUT_HISTORY_DIR was set empty: run without recording."""


def history_dir() -> Path:
    """Where snapshots live. Raises HistoryDisabled when recording is off."""
    base = os.environ.get("TRADE_SCOUT_HISTORY_DIR")
    if base is not None:
        if not base.strip():
            raise HistoryDisabled()
        return Path(base)
    return DEFAULT_DIR


def snapshot_path(source: str, day: str) -> Path:
    """One file per source per day. Flat and greppable on purpose."""
    safe = "".join(c for c in str(source) if c.isalnum() or c in "-_")
    if not safe:
        raise ValueError("source must have at least one alphanumeric character")
    return history_dir() / safe / f"{day}.jsonl"


def today() -> str:
    return date.today().isoformat()


def record(source: str, rows: list[dict], day: str | None = None) -> int:
    """Write one day's rows for one source. Returns the count written, 0 on any failure.

    Never raises. A failed snapshot must not cost the caller its board.
    """
    day = day or today()
    try:
        if not isinstance(rows, list) or not rows:
            return 0
        if len(rows) > MAX_ROWS:
            log.warning("history: %s offered %d rows, over the %d cap; not recording",
                        source, len(rows), MAX_ROWS)
            return 0
        path = snapshot_path(source, day)
        path.parent.mkdir(parents=True, exist_ok=True)
        # Temp plus rename, so a crash mid-write leaves the previous day's file
        # intact rather than a half-written one that parses as truncated data.
        #
        # Unique per writer, not a fixed ".tmp": every request is a fresh
        # process and two of them recording the same day would interleave into
        # one temp file and then both rename it. Atomic rename, non-atomic
        # contents, which is the failure that looks like clean data.
        fd, tmp_name = tempfile.mkstemp(dir=str(path.parent), prefix=path.name + ".", suffix=".tmp")
        os.close(fd)
        tmp = Path(tmp_name)
        written = 0
        with tmp.open("w", encoding="utf-8") as fh:
            for r in rows:
                if not isinstance(r, dict):
                    continue
                fh.write(json.dumps(r, separators=(",", ":"), sort_keys=True) + "\n")
                written += 1
        if not written:
            tmp.unlink(missing_ok=True)
            return 0
        tmp.replace(path)
        log.info("history: recorded %d %s rows for %s", written, source, day)
        return written
    except HistoryDisabled:
        return 0
    except Exception as e:
        log.warning("history: could not record %s for %s (non-fatal): %s", source, day, e)
        return 0


def read_day(source: str, day: str) -> list[dict]:
    """Read one day back. Returns [] when absent or unreadable."""
    try:
        path = snapshot_path(source, day)
        if not path.exists():
            return []
        out = []
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except json.JSONDecodeError:
                # One bad line must not discard the day. Partial history beats
                # none, and the count is what a backtest sanity-checks anyway.
                continue
            if isinstance(row, dict):
                out.append(row)
        return out
    except (HistoryDisabled, ValueError):
        return []
    except Exception as e:
        log.warning("history: could not read %s for %s: %s", source, day, e)
        return []


def available_days(source: str) -> list[str]:
    """Every recorded day for a source, oldest first. The backtest's index."""
    try:
        d = history_dir() / "".join(c for c in str(source) if c.isalnum() or c in "-_")
        if not d.is_dir():
            return []
        return sorted(p.stem for p in d.glob("*.jsonl"))
    except (HistoryDisabled, ValueError):
        return []
    except Exception:
        return []


def shrink_market(values: dict) -> list[dict]:
    """FantasyCalc's keyed result -> the four fields a backtest reads."""
    out = []
    for pid, v in (values or {}).items():
        if not isinstance(v, dict):
            continue
        out.append({
            "id": str(pid),
            "name": v.get("name"),
            "pos": v.get("position"),
            "value": v.get("value"),
            "rank": v.get("overall_rank") or v.get("rank"),
        })
    return out


def shrink_ktc(values: dict) -> list[dict]:
    """KTC is keyed by NORMALISED NAME, not id. Recorded as-is so the join at
    backtest time is the same fuzzy name join the live path already uses."""
    out = []
    for name, v in (values or {}).items():
        if not isinstance(v, dict):
            continue
        out.append({
            "name": name,
            "pos": v.get("position"),
            "value": v.get("value"),
            "rank": v.get("rank"),
        })
    return out


def shrink_flock(rankings: dict) -> list[dict]:
    """Flock is ORDINAL. No value field, and inventing one here would bake a
    curve mapping into the history that a later backtest could not undo."""
    out = []
    for name, v in (rankings or {}).items():
        if not isinstance(v, dict):
            continue
        out.append({
            "name": name,
            "pos": v.get("position"),
            "rank": v.get("flock_rank"),
            "tier": v.get("flock_tier"),
        })
    return out
