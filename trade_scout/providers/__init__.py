"""Provider registry: every value source the engine can consult.

Priority order matters: the first AVAILABLE source's value curve is the
reference every other source is quantile-mapped onto, so past verdicts stay
comparable as long as the front of this list is stable.
"""

from __future__ import annotations

from datetime import date, datetime, timezone
from typing import TYPE_CHECKING

from trade_scout.providers.base import (
    LeagueFormat,
    SourceSnapshot,
    config_enabled,
)

if TYPE_CHECKING:
    from trade_scout.config import AppConfig


class FantasyCalcSource:
    """Market values fitted from real completed dynasty trades. Open API."""

    key = "fantasycalc"
    display_name = "FantasyCalc"
    requires: tuple[str, ...] = ()

    def enabled(self, cfg: "AppConfig") -> bool:
        return config_enabled(cfg, self.key, default=True)

    def fetch(self, fmt: LeagueFormat) -> SourceSnapshot:
        from trade_scout import picks as picks_mod
        from trade_scout.providers import fantasycalc as fc
        from trade_scout.providers.base import SourceError

        try:
            players = fc.fantasycalc_provider(fmt.to_league_settings())
            pick_map = picks_mod.extract_fantasycalc_picks(fc.last_raw_response())
        except fc.ValuesSourceError as e:
            raise SourceError(str(e)) from e
        return SourceSnapshot(
            source=self.key, display_name=self.display_name,
            as_of=date.today().isoformat(), fmt=fmt, join="sleeper_id",
            players=players, picks=pick_map,
        )


class DynastyProcessSource:
    """Weekly expert-consensus values from open CSVs on GitHub."""

    key = "dynastyprocess"
    display_name = "DynastyProcess"
    requires: tuple[str, ...] = ()

    def enabled(self, cfg: "AppConfig") -> bool:
        return config_enabled(cfg, self.key, default=True)

    def fetch(self, fmt: LeagueFormat) -> SourceSnapshot:
        from trade_scout.providers.dynastyprocess import get_dynastyprocess_snapshot

        return get_dynastyprocess_snapshot(fmt)


class KTCSource:
    """Crowdsourced values scraped from the KTC rankings page. Name-joined."""

    key = "ktc"
    display_name = "KeepTradeCut"
    requires: tuple[str, ...] = ()

    def enabled(self, cfg: "AppConfig") -> bool:
        return config_enabled(cfg, self.key, default=True)

    def fetch(self, fmt: LeagueFormat) -> SourceSnapshot:
        from trade_scout import picks as picks_mod
        from trade_scout.providers import keeptradecut as ktc
        from trade_scout.providers.base import SourceError

        try:
            players = ktc.get_ktc_values(superflex=fmt.superflex)
            pick_map = picks_mod.extract_ktc_picks(players)
        except ktc.KTCError as e:
            raise SourceError(str(e)) from e
        return SourceSnapshot(
            source=self.key, display_name=self.display_name,
            as_of=date.today().isoformat(), fmt=fmt, join="name",
            players=players, picks=pick_map,
        )


class DynastyDaddySource:
    """Dynasty Daddy's in-house values, from the same public unauthenticated
    endpoint their web frontend uses. Unofficial API: any failure excludes
    the source for the run, never more."""

    key = "dynastydaddy"
    display_name = "Dynasty Daddy"
    requires: tuple[str, ...] = ()

    def enabled(self, cfg: "AppConfig") -> bool:
        return config_enabled(cfg, self.key, default=True)

    def fetch(self, fmt: LeagueFormat) -> SourceSnapshot:
        from trade_scout.providers.dynastydaddy import get_dynastydaddy_snapshot

        return get_dynastydaddy_snapshot(fmt)


# Priority order: the first available source is the reference curve.
REGISTRY: list = [
    FantasyCalcSource(),
    DynastyProcessSource(),
    KTCSource(),
    DynastyDaddySource(),
]

REGISTRY_BY_KEY = {s.key: s for s in REGISTRY}


def utcnow_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
