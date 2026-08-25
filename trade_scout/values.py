"""The ONE seam for values. Nothing else knows where values come from.

get_snapshots(league_settings, cfg, matcher)
    -> ({source_key: SourceSnapshot}, {source_key: error})

Every enabled provider in the registry is fetched concurrently and resolved
to sleeper_id keys; one failing source is excluded and reported, never fatal.
This is what the multi-source evaluator consumes.

get_values / get_pick_values are the older single-blend entry points, kept
for the legacy single-verdict path and the roster board.
"""

from __future__ import annotations

import logging

from trade_scout.providers.fantasycalc import (
    ValuesSourceError,
    fantasycalc_provider,
    last_raw_response,
)

log = logging.getLogger(__name__)


class ValuesError(Exception):
    """Raised when no provider can supply values."""


def get_snapshots(league_settings: dict, cfg=None, matcher=None):
    """Fetch every enabled value source concurrently.

    Returns (snapshots, errors):
        snapshots: {source_key: SourceSnapshot}, keyed by sleeper_id when a
                   NameMatcher is supplied (name-joined sources are resolved
                   centrally), in REGISTRY priority order.
        errors:    {source_key: reason} for sources that were enabled but
                   failed. Failure of one source is never fatal while at
                   least one other lives; total failure is the caller's
                   ValuesError to raise if it needs values at all.
    """
    import concurrent.futures

    from trade_scout.config import AppConfig
    from trade_scout.engine.resolve import resolve_snapshot
    from trade_scout.providers import REGISTRY
    from trade_scout.providers.base import LeagueFormat, SourceError

    cfg = cfg or AppConfig.load()
    fmt = LeagueFormat.from_league_settings(league_settings)
    enabled = [s for s in REGISTRY if s.enabled(cfg)]

    snapshots: dict = {}
    errors: dict[str, str] = {}
    if not enabled:
        return snapshots, errors

    from trade_scout.providers.base import SourceSnapshot

    with concurrent.futures.ThreadPoolExecutor(max_workers=len(enabled)) as pool:
        futures = {pool.submit(s.fetch, fmt): s for s in enabled}
        results: dict[str, SourceSnapshot] = {}
        for future, source in futures.items():
            try:
                results[source.key] = future.result()
            except SourceError as e:
                errors[source.key] = str(e)
                log.warning("%s unavailable: %s", source.display_name, e)
            except Exception as e:  # noqa: BLE001 - a broken provider must not
                errors[source.key] = f"unexpected: {e}"  # take down the rest
                log.exception("%s crashed", source.display_name)

    for source in enabled:  # registry order, not completion order
        snap = results.get(source.key)
        if snap is None:
            continue
        if matcher is not None:
            snap = resolve_snapshot(snap, matcher)
        snapshots[source.key] = snap
    return snapshots, errors


def get_values(league_settings: dict) -> dict[str, dict]:
    """Fetch player values from the best available source.

    Returns dict mapping sleeper_player_id -> {
        value, overall_rank, position_rank, trend_30day, source
    }.

    Raises ValuesError if the provider fails.
    """
    try:
        result = fantasycalc_provider(league_settings)
        log.info("Values from FantasyCalc: %d players", len(result))
        return result
    except ValuesSourceError as e:
        raise ValuesError(f"FantasyCalc: {e}") from e


def get_pick_values(league_settings: dict) -> dict[str, dict]:
    """The ONE seam for DRAFT PICK values, mirroring get_values for players.

    Picks used to be priced from a hardcoded table that discounted the future
    20% a year, while the market pays MORE for a 2027 first than a 2026 one.
    That is now the fallback of last resort, and it says so when it is used.

    Never raises. A trade that cannot price a pick is still worth answering
    with the pick named as unpriced, which is what the invariant in section 2
    asks for.
    """
    from trade_scout import picks as picks_mod
    from trade_scout.providers.keeptradecut import KTCError, get_ktc_values

    superflex = int(league_settings.get("num_qbs", 1)) >= 2

    fc: dict[str, float] = {}
    try:
        # fantasycalc_provider drops picks (they carry no sleeperId) but keeps
        # the raw response, so this needs no second round trip.
        fantasycalc_provider(league_settings)
        fc = picks_mod.extract_fantasycalc_picks(last_raw_response())
    except (ValuesSourceError, Exception) as e:  # noqa: BLE001
        log.warning("FantasyCalc picks unavailable (non-fatal): %s", e)

    kt: dict[str, float] = {}
    try:
        kt = picks_mod.extract_ktc_picks(get_ktc_values(superflex=superflex))
    except (KTCError, Exception) as e:  # noqa: BLE001
        log.warning("KTC picks unavailable (non-fatal): %s", e)

    market = picks_mod.build_pick_market(fc, kt)
    log.info("Pick market: %d entries (FantasyCalc %d, KTC %d)",
             len(market), len(fc), len(kt))
    return market
