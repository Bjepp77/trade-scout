"""The ONE seam for values. Nothing else knows where values come from.

get_values(league_settings) -> dict[sleeper_player_id -> ValueRecord]

Currently uses FantasyCalc as the sole provider. The seam is here so a future
provider (KTC, Dynasty Daddy, or a working Flock scraper) can slot in by adding
a provider to this one file. Nothing else changes.
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
