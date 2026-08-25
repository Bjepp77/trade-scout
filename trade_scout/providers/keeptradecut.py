"""KeepTradeCut values provider. No auth, no browser.

KTC has no public API: /api/players is a 404 and the graph endpoint 500s. What
it does have is the dynasty-rankings page, which embeds the entire ranking set
as a JavaScript array literal (`var players = [...]`). One plain GET, ~1.3 MB,
500 players.

Returns dict keyed by NORMALISED NAME, not by sleeper id. KTC does not publish
a sleeperID field (it carries playerID, slug and mflid instead), so the join is
by name and inherits every fuzzy-match risk that implies. That is a real cost
and it is why the caller must be able to report which players it could not
match rather than dropping them.

Two things about KTC's numbers that are easy to misread:
  - `value` tops out near 9999, where FantasyCalc tops near 11000. The scales
    are NOT interchangeable and must be normalised before blending.
  - `overallTrend` is movement in RANK, not a change in value. FantasyCalc's
    trend30Day is a value delta. Averaging them would be adding a rank to a
    price.
"""

from __future__ import annotations

import json
import logging
import re

import httpx

from trade_scout import cache

log = logging.getLogger(__name__)

KTC_URL = "https://keeptradecut.com/dynasty-rankings"
TIMEOUT = 30.0
# Dynasty values move over days, not minutes.
CACHE_MAX_AGE = 30 * 60
# A browser UA: the page is server-rendered HTML meant for people, and a bare
# client gets a different (useless) response.
UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "\
     "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"


class KTCError(Exception):
    """Raised when KTC cannot be fetched or its shape has changed."""


def normalise_name(name: str) -> str:
    """Lowercase, strip punctuation and common suffixes. The join key."""
    n = str(name or "").lower().strip()
    n = re.sub(r"\b(jr|sr|ii|iii|iv|v)\b\.?", "", n)
    n = re.sub(r"[^a-z0-9 ]", "", n)
    return re.sub(r"\s+", " ", n).strip()


def _extract_array(html: str) -> list[dict]:
    """Pull the `var players = [...]` literal out of the page.

    Bracket-matched rather than regex-greedy: the array holds nested objects
    and arrays, and a lazy regex silently truncates it mid-player.
    """
    marker = html.find("var players")
    if marker == -1:
        raise KTCError("no `var players` array in the KTC page; the page changed")
    start = html.find("[", marker)
    if start == -1:
        raise KTCError("`var players` found but no array follows it")

    depth = 0
    for i in range(start, len(html)):
        c = html[i]
        if c == "[":
            depth += 1
        elif c == "]":
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(html[start:i + 1])
                except json.JSONDecodeError as e:
                    raise KTCError(f"KTC array did not parse as JSON: {e}") from e
    raise KTCError("KTC array never closed; the page is truncated")


_MEMO: dict = {}


def get_ktc_values(superflex: bool = False) -> dict[str, dict]:
    """Fetch KTC dynasty values.

    Returns {normalised_name: {value, rank, positional_rank, tier, trend_rank,
    position, age, source}}. Raises KTCError on any failure, so the caller
    decides whether a missing KTC is fatal (it is not).
    """
    # Process-lifetime memo. A single board build asked for this TWICE: once
    # to price players and again to price picks, pulling 1.3 MB of HTML each
    # time. Every request is a fresh process, so the memo cannot go stale
    # within a run and cannot leak between runs.
    if superflex in _MEMO:
        return _MEMO[superflex]

    # And across processes. Values move slowly; every request is a new spawn,
    # so without this a board refresh and a trade a minute later each pull the
    # same 1.3 MB and re-parse it.
    cache_name = f"ktc-{'sf' if superflex else '1qb'}.json"
    disk = cache.read_json(cache_name, CACHE_MAX_AGE)
    if isinstance(disk, dict) and disk:
        _MEMO[superflex] = disk
        return disk

    try:
        resp = httpx.get(KTC_URL, timeout=TIMEOUT, headers={"User-Agent": UA},
                         follow_redirects=True)
        resp.raise_for_status()
    except httpx.HTTPError as e:
        raise KTCError(f"KTC fetch failed: {e}") from e

    players = _extract_array(resp.text)
    if not isinstance(players, list) or not players:
        raise KTCError("KTC returned an empty player array")

    key = "superflexValues" if superflex else "oneQBValues"
    out: dict[str, dict] = {}
    skipped = 0
    for p in players:
        vals = p.get(key) or {}
        value = vals.get("value")
        if value is None:
            skipped += 1
            continue
        name = p.get("playerName")
        if not name:
            skipped += 1
            continue
        out[normalise_name(name)] = {
            "name": name,
            "value": value,
            "rank": vals.get("rank"),
            "positional_rank": vals.get("positionalRank"),
            "tier": vals.get("overallTier"),
            # RANK movement, not a value delta. Named so nobody blends it with
            # FantasyCalc's trend30Day by accident.
            "trend_rank": vals.get("overallTrend"),
            "position": p.get("position"),
            "age": p.get("age"),
            "source": "KeepTradeCut",
        }

    if not out:
        raise KTCError(f"KTC parsed {len(players)} rows but none had a {key} value")
    log.info("KTC: %d players with values (%d skipped)", len(out), skipped)
    _MEMO[superflex] = out
    cache.write_json(cache_name, out)
    return out
