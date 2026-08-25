"""DynastyProcess values provider. Open data, no auth, no scraping.

DynastyProcess publishes weekly trade values built on FantasyPros expert
consensus ranks, as plain CSVs in a public GitHub repo:

    values.csv        players AND picks, with value_1qb / value_2qb columns
    db_playerids.csv  id crosswalk: fantasypros_id <-> sleeper_id (and more)

Two properties make this the easiest provider in the set:
- The crosswalk gives a DETERMINISTIC join to sleeper_id for most players, so
  this source needs no fuzzy name matching at all for matched rows.
- Pick rows use the same naming KTC does ("2027 Early 1st", "2026 Pick 1.01"),
  which picks.parse_pick already reduces to canonical keys.

The data updates weekly (GitHub Actions on their side), so a snapshot whose
scrape_date is older than STALE_AFTER_DAYS is flagged stale rather than
refused: last week's expert consensus is degraded, not worthless, and the
consensus layer decides whether stale sources vote.
"""

from __future__ import annotations

import csv
import io
import logging
from datetime import date, datetime

import httpx

from trade_scout import cache
from trade_scout.picks import parse_pick
from trade_scout.providers.base import LeagueFormat, SourceError, SourceSnapshot

log = logging.getLogger(__name__)

VALUES_URL = ("https://raw.githubusercontent.com/dynastyprocess/data/"
              "master/files/values.csv")
PLAYERIDS_URL = ("https://raw.githubusercontent.com/dynastyprocess/data/"
                 "master/files/db_playerids.csv")

# The values update weekly; re-downloading more than daily buys nothing.
CACHE_MAX_AGE = 24 * 60 * 60
# The crosswalk changes when players enter the league, i.e. rarely.
CROSSWALK_MAX_AGE = 7 * 24 * 60 * 60
STALE_AFTER_DAYS = 10

_MEMO: dict = {}


def _fetch_csv(url: str, cache_name: str, max_age: float) -> list[dict]:
    """One CSV as a list of row dicts, disk-cached as the raw text."""
    raw = cache.read_json(cache_name, max_age)
    if not isinstance(raw, str) or not raw:
        try:
            resp = httpx.get(url, timeout=30, follow_redirects=True)
            resp.raise_for_status()
        except httpx.HTTPError as e:
            raise SourceError(f"DynastyProcess fetch failed: {e}") from e
        raw = resp.text
        cache.write_json(cache_name, raw)
    return list(csv.DictReader(io.StringIO(raw)))


def _sleeper_by_fp() -> dict[str, str]:
    """fantasypros_id -> sleeper_id from the crosswalk. {} on failure."""
    try:
        rows = _fetch_csv(PLAYERIDS_URL, "dp-playerids.csv", CROSSWALK_MAX_AGE)
    except SourceError as e:
        log.warning("DynastyProcess crosswalk unavailable: %s", e)
        return {}
    out: dict[str, str] = {}
    for r in rows:
        fp = (r.get("fantasypros_id") or "").strip()
        sl = (r.get("sleeper_id") or "").strip()
        if fp and sl and fp != "NA" and sl != "NA":
            out[fp] = sl
    return out


def _num(row: dict, col: str) -> float | None:
    raw = (row.get(col) or "").strip()
    if not raw or raw == "NA":
        return None
    try:
        return float(raw)
    except ValueError:
        return None


def get_dynastyprocess_snapshot(fmt: LeagueFormat) -> SourceSnapshot:
    """Fetch and shape the DynastyProcess values for one format."""
    memo_key = fmt.superflex
    if memo_key in _MEMO:
        return _MEMO[memo_key]

    rows = _fetch_csv(VALUES_URL, "dp-values.csv", CACHE_MAX_AGE)
    if not rows:
        raise SourceError("DynastyProcess returned an empty values file")

    crosswalk = _sleeper_by_fp()
    val_col = "value_2qb" if fmt.superflex else "value_1qb"
    ecr_col = "ecr_2qb" if fmt.superflex else "ecr_1qb"

    players: dict[str, dict] = {}
    picks: dict[str, float] = {}
    as_of = ""
    unmatched = 0
    for r in rows:
        value = _num(r, val_col)
        if value is None:
            continue
        as_of = r.get("scrape_date") or as_of
        name = (r.get("player") or "").strip()
        if (r.get("pos") or "").strip().upper() == "PICK":
            ref = parse_pick(name)
            if ref is not None:
                picks[ref.key()] = value
            continue
        fp_id = (r.get("fp_id") or "").strip()
        sleeper_id = crosswalk.get(fp_id, "")
        if not sleeper_id:
            unmatched += 1
            continue
        players[sleeper_id] = {
            "value": value,
            "name": name,
            "position": (r.get("pos") or "").strip().upper() or None,
            "age": _num(r, "age"),
            "overall_rank": _num(r, ecr_col),
            "source": "DynastyProcess",
        }

    if not players:
        raise SourceError(
            "DynastyProcess rows parsed but none joined to a sleeper id "
            "(crosswalk missing or column names changed)")

    stale = False
    try:
        stale = (date.today() -
                 datetime.strptime(as_of, "%Y-%m-%d").date()).days > STALE_AFTER_DAYS
    except ValueError:
        pass

    snap = SourceSnapshot(
        source="dynastyprocess",
        display_name="DynastyProcess",
        as_of=as_of or date.today().isoformat(),
        fmt=fmt,
        join="sleeper_id",
        players=players,
        picks=picks,
        stale=stale,
        meta={"unmatched_rows": unmatched, "pick_rows": len(picks)},
    )
    log.info("DynastyProcess: %d players, %d picks (as of %s%s)",
             len(players), len(picks), snap.as_of, ", STALE" if stale else "")
    _MEMO[memo_key] = snap
    return snap
