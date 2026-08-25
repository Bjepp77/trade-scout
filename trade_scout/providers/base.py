"""The provider contract: what every value source promises to the engine.

Two distinct roles, deliberately kept apart:

- A ValueSource publishes per-asset PRICES and votes in the trade consensus.
- An OpinionSource (Flock) publishes rank/tier CONVICTION and is shown beside
  the verdict but never blended into a number and never given a vote. Folding
  it in would collapse two independent readings into one that agrees with
  itself.

The snapshot player record is a plain dict, not a dataclass, because every
consumer downstream (the model, the evaluator, the board) already speaks
dicts. The contract is documented here instead:

    {"value": float,               # REQUIRED, on the SOURCE'S OWN scale
     "name": str,                  # required when join == "name"
     "position": str | None,      # helps central name->id resolution
     "overall_rank": int | None,
     "position_rank": int | None,
     ...source-specific extras}

Values are never normalised inside a provider. Cross-source normalisation is
the engine's job (model.quantile_normalise), because it needs every snapshot
in hand to pick the reference curve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Literal, Protocol, runtime_checkable

if TYPE_CHECKING:
    from trade_scout.config import AppConfig

JoinKey = Literal["sleeper_id", "name"]


class SourceError(Exception):
    """A provider failed: network, shape change, or bad data."""


class SourceDisabled(SourceError):
    """The provider is turned off or missing what it needs to run."""


@dataclass(frozen=True)
class LeagueFormat:
    """The format axes that change what an asset is worth."""

    superflex: bool
    ppr: float = 1.0
    num_teams: int = 12
    is_dynasty: bool = True

    @classmethod
    def from_league_settings(cls, league_settings: dict) -> "LeagueFormat":
        return cls(
            superflex=int(league_settings.get("num_qbs", 1)) >= 2,
            ppr=float(league_settings.get("ppr", 1)),
            num_teams=int(league_settings.get("num_teams", 12)),
            is_dynasty=bool(league_settings.get("is_dynasty", True)),
        )

    def to_league_settings(self) -> dict:
        """The legacy league_settings shape the older providers consume."""
        return {
            "num_qbs": 2 if self.superflex else 1,
            "ppr": self.ppr,
            "num_teams": self.num_teams,
            "is_dynasty": self.is_dynasty,
        }


@dataclass
class SourceSnapshot:
    """One source's complete view of the market at one moment."""

    source: str                       # provider key, e.g. "fantasycalc"
    display_name: str                 # e.g. "FantasyCalc"
    as_of: str                        # ISO date of the underlying data
    fmt: LeagueFormat
    join: JoinKey                     # what `players` is keyed by
    players: dict[str, dict]          # see the record contract above
    picks: dict[str, float] = field(default_factory=dict)  # canonical pick key
    stale: bool = False               # served, but older than the source's cadence
    meta: dict = field(default_factory=dict)  # diagnostics (unmatched counts, ...)


@runtime_checkable
class ValueSource(Protocol):
    """A source of per-asset prices. Votes in the consensus."""

    key: str
    display_name: str
    requires: tuple[str, ...]         # env vars / extras needed; () if none

    def enabled(self, cfg: "AppConfig") -> bool: ...

    def fetch(self, fmt: LeagueFormat) -> SourceSnapshot:
        """Return a snapshot. Raises SourceError (or SourceDisabled)."""
        ...


def config_enabled(cfg: "AppConfig", key: str, default: bool = True) -> bool:
    """The user's enabled flag for a source, with the registry default."""
    entry = cfg.sources.get(key) or {}
    val = entry.get("enabled", default)
    return bool(val) if val != "auto" else True
