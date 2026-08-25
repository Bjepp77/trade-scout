"""Central name -> sleeper_id resolution for source snapshots.

Every source joins the engine on ONE canonical key: the Sleeper player id.
Sources that publish sleeper ids (FantasyCalc, DynastyProcess via crosswalk)
pass through untouched. Name-keyed sources (KTC) are resolved here, once,
with the position hint each record carries, instead of every consumer doing
its own ad-hoc fuzzy join.

Unmatched rows are DROPPED from the resolved map but counted in meta, so a
drifting name-join shows up as a number on the status page instead of as
silent mispricing.
"""

from __future__ import annotations

import logging

from trade_scout.name_matcher import NameMatcher
from trade_scout.providers.base import SourceSnapshot

log = logging.getLogger(__name__)


def resolve_snapshot(snap: SourceSnapshot, matcher: NameMatcher) -> SourceSnapshot:
    """Return a snapshot keyed by sleeper_id.

    join == "sleeper_id" passes through unchanged. join == "name" resolves
    each record's display name through the NameMatcher; a record that cannot
    be matched confidently is dropped and counted.
    """
    if snap.join == "sleeper_id":
        return snap

    players: dict[str, dict] = {}
    unmatched = 0
    for key, record in snap.players.items():
        name = record.get("name") or key
        pid = matcher.match(name, position=record.get("position"))
        if pid is None:
            # A position hint that is wrong (KTC lists RDP/pick rows oddly,
            # Sleeper occasionally disagrees on position) should not kill an
            # otherwise-unambiguous name.
            pid = matcher.match(name)
        if pid is None:
            unmatched += 1
            continue
        players[pid] = record
    resolved = SourceSnapshot(
        source=snap.source, display_name=snap.display_name, as_of=snap.as_of,
        fmt=snap.fmt, join="sleeper_id", players=players, picks=snap.picks,
        stale=snap.stale,
        meta={**snap.meta, "unmatched_names": unmatched},
    )
    if unmatched:
        log.info("%s: %d/%d names did not resolve to a sleeper id",
                 snap.display_name, unmatched, len(snap.players))
    return resolved
