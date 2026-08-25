"""Dynasty Daddy values provider.

Dynasty Daddy publishes its own in-house trade value set (time-weighted,
refreshed every couple of hours). The endpoint below is the same public,
unauthenticated GET its own web frontend calls:

    GET https://dynasty-daddy.com/api/v1/player/all/today

One response covers both formats: `trade_value` (1QB) and `sf_trade_value`
(superflex) per row, `sleeper_id` for players (a deterministic join), and
pick rows as position "PI" named exactly the way picks.parse_pick already
reads ("2027 Early 1st").

UNOFFICIAL API. It is not documented and can change or disappear without
notice, which is why every failure here is a SourceError the engine excludes
and reports rather than anything fatal, and why the response is cached: one
small GET every few hours, not a poll.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timezone

import httpx

from trade_scout import cache
from trade_scout.picks import parse_pick
from trade_scout.providers.base import LeagueFormat, SourceError, SourceSnapshot

log = logging.getLogger(__name__)

VALUES_URL = "https://dynasty-daddy.com/api/v1/player/all/today"

# Their values refresh every ~2 hours; refetching more than every 6 buys
# little for a personal tool and keeps the footprint polite.
CACHE_MAX_AGE = 6 * 60 * 60
STALE_AFTER_DAYS = 3

_MEMO: dict = {}


def _fetch_rows() -> list[dict]:
    raw = cache.read_json("dynastydaddy.json", CACHE_MAX_AGE)
    if isinstance(raw, list) and raw:
        return raw
    try:
        resp = httpx.get(VALUES_URL, timeout=30, follow_redirects=True,
                         headers={"Accept": "application/json"})
        resp.raise_for_status()
        data = resp.json()
    except httpx.HTTPError as e:
        raise SourceError(f"Dynasty Daddy fetch failed: {e}") from e
    except ValueError as e:
        raise SourceError(f"Dynasty Daddy returned non-JSON: {e}") from e
    if not isinstance(data, list) or not data:
        raise SourceError("Dynasty Daddy returned an unexpected shape "
                          f"({type(data).__name__}); the API may have changed")
    cache.write_json("dynastydaddy.json", data)
    return data


def get_dynastydaddy_snapshot(fmt: LeagueFormat) -> SourceSnapshot:
    memo_key = fmt.superflex
    if memo_key in _MEMO:
        return _MEMO[memo_key]

    rows = _fetch_rows()
    val_key = "sf_trade_value" if fmt.superflex else "trade_value"
    rank_key = "sf_overall_rank" if fmt.superflex else "overall_rank"
    pos_rank_key = "sf_position_rank" if fmt.superflex else "position_rank"

    players: dict[str, dict] = {}
    picks: dict[str, float] = {}
    as_of = ""
    unmatched = 0
    for r in rows:
        value = r.get(val_key)
        if not isinstance(value, (int, float)) or value <= 0:
            continue
        as_of = str(r.get("date") or as_of)
        name = str(r.get("full_name") or "").strip()
        if str(r.get("position") or "").upper() == "PI":
            ref = parse_pick(name)
            if ref is not None:
                picks[ref.key()] = float(value)
            continue
        sleeper_id = str(r.get("sleeper_id") or "").strip()
        if not sleeper_id or sleeper_id == "None":
            unmatched += 1
            continue
        players[sleeper_id] = {
            "value": float(value),
            "name": name,
            "position": str(r.get("position") or "").upper() or None,
            "age": r.get("age"),
            "overall_rank": r.get(rank_key),
            "position_rank": r.get(pos_rank_key),
            "source": "Dynasty Daddy",
        }

    if not players:
        raise SourceError("Dynasty Daddy rows parsed but none had a "
                          f"sleeper_id and a {val_key}; the shape changed")

    stale = False
    try:
        seen = datetime.fromisoformat(as_of.replace("Z", "+00:00"))
        stale = (datetime.now(timezone.utc) - seen).days > STALE_AFTER_DAYS
    except ValueError:
        pass

    snap = SourceSnapshot(
        source="dynastydaddy",
        display_name="Dynasty Daddy",
        as_of=(as_of[:10] if as_of else date.today().isoformat()),
        fmt=fmt,
        join="sleeper_id",
        players=players,
        picks=picks,
        stale=stale,
        meta={"unmatched_rows": unmatched, "pick_rows": len(picks)},
    )
    log.info("Dynasty Daddy: %d players, %d picks (as of %s%s)",
             len(players), len(picks), snap.as_of, ", STALE" if stale else "")
    _MEMO[memo_key] = snap
    return snap
