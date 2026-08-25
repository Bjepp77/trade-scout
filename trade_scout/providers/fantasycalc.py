"""FantasyCalc values provider. No auth required.

GET https://api.fantasycalc.com/values/current?isDynasty=true&numQbs=2&numTeams=12&ppr=1

Returns a JSON array; each element has a player object and value fields.
Confirmed fields: player.sleeperId, value, redraftValue, overallRank,
positionRank, trend30Day.
"""

from __future__ import annotations

import logging

import httpx

from trade_scout import cache

log = logging.getLogger(__name__)

# The most recent raw response, so picks can be extracted without a second
# HTTP round trip. Not a cache: it is only ever read right after a fetch.
_LAST_RAW: dict = {}

# Keyed on the parameters that actually change the response. Same reasoning as
# KTC: one board build fetched this twice, once for players and once for picks.
_MEMO: dict = {}


def last_raw_response() -> list:
    """The raw list from the most recent successful fetch, or []."""
    got = _LAST_RAW.get("response")
    return got if isinstance(got, list) else []

CACHE_MAX_AGE = 30 * 60  # dynasty values move over days
FANTASYCALC_URL = "https://api.fantasycalc.com/values/current"


class ValuesSourceError(Exception):
    """Raised when a values provider returns an unexpected shape or fails."""


def _shape(data: list) -> dict[str, dict]:
    """Raw FantasyCalc rows to the keyed value map.

    Split out so a cached RAW response can be reshaped. The raw rows are what
    gets cached, not this result, because the pick market is extracted from
    them and a cache holding only the player map would send picks back to the
    network.
    """
    result: dict[str, dict] = {}
    missing = 0
    for entry in data or []:
        player = entry.get("player", {}) or {}
        sleeper_id = player.get("sleeperId")
        if not sleeper_id:
            missing += 1
            continue
        result[str(sleeper_id)] = {
            "value": entry.get("value", 0),
            "overall_rank": entry.get("overallRank"),
            "position_rank": entry.get("positionRank"),
            "trend_30day": entry.get("trend30Day"),
            "source": "FantasyCalc",
        }
    if missing:
        log.info("FantasyCalc: %d entries had no sleeperId", missing)
    return result


def fantasycalc_provider(league_settings: dict) -> dict[str, dict]:
    """Fetch values from FantasyCalc.

    Returns dict mapping sleeper_player_id -> {
        value, overall_rank, position_rank, trend_30day, source
    }.

    Raises ValuesSourceError on HTTP failure or unexpected response shape.
    """
    params = {
        "isDynasty": "true" if league_settings.get("is_dynasty", True) else "false",
        "numQbs": str(league_settings.get("num_qbs", 2)),
        "numTeams": str(league_settings.get("num_teams", 12)),
        "ppr": str(league_settings.get("ppr", 1)),
    }

    memo_key = (
        params.get("isDynasty"), params.get("numQbs"),
        params.get("numTeams"), params.get("ppr"),
    )
    if memo_key in _MEMO:
        cached_raw, cached_result = _MEMO[memo_key]
        _LAST_RAW["response"] = cached_raw
        return cached_result

    # Across processes too. The RAW response is cached, not the keyed result,
    # because the pick market is extracted from the raw rows and a cache that
    # served only the player map would send picks back to the network.
    cache_name = "fantasycalc-" + "-".join(str(x) for x in memo_key) + ".json"
    disk = cache.read_json(cache_name, CACHE_MAX_AGE)
    if isinstance(disk, list) and disk:
        _LAST_RAW["response"] = disk
        result = _shape(disk)
        if result:
            _MEMO[memo_key] = (disk, result)
            return result

    try:
        resp = httpx.get(FANTASYCALC_URL, params=params, timeout=30)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise ValuesSourceError(f"FantasyCalc HTTP error: {e}") from e

    data = resp.json()
    # Kept so the pick market can read the entries this provider drops: picks
    # carry no sleeperId, so the loop below discards them by design.
    _LAST_RAW["response"] = data
    if not isinstance(data, list):
        raise ValuesSourceError(
            f"FantasyCalc returned unexpected shape: {type(data).__name__}"
        )

    result = _shape(data)
    if not result:
        raise ValuesSourceError("FantasyCalc returned no matchable players")

    log.info("FantasyCalc: %d players with values", len(result))
    _MEMO[memo_key] = (data, result)
    cache.write_json(cache_name, data)
    return result
