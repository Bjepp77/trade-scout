"""Who owns which future draft pick, reconstructed from Sleeper.

Sleeper does not publish a list of every pick. It publishes the EXCEPTIONS:
/league/{id}/traded_picks holds one row per pick that has changed hands, and
everything else is implied by the default that a team owns its own.

    {"round": 1, "season": "2027", "roster_id": 1, "owner_id": 7,
     "previous_owner_id": 1}

    roster_id  whose pick it ORIGINALLY is (this never changes)
    owner_id   who holds it NOW
    previous_owner_id  who they got it from

So the full board is: every (season, round, roster) in scope, with the owner
taken from a traded row when one exists and from the originating roster
otherwise.

ONLY FUTURE DRAFTS. A completed draft's picks are spent, not assets. The 2026
draft in these leagues finished on 2026-05-02 and its rows are still in
traded_picks; showing them as tradeable would put twelve dead assets on every
roster.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

# Sleeper carries picks a few years out. Anything beyond what the sources
# price is noise, and the market runs dry after three future classes.
MAX_FUTURE_SEASONS = 3


def completed_seasons(drafts: list[dict]) -> set[int]:
    """Seasons whose draft is already done, so their picks are spent."""
    out: set[int] = set()
    for d in drafts or []:
        if str(d.get("status") or "").lower() == "complete":
            try:
                out.add(int(str(d.get("season"))))
            except (TypeError, ValueError):
                continue
    return out


def future_seasons(traded: list[dict], drafts: list[dict],
                   current_season: int) -> list[int]:
    """The draft seasons still worth showing, in order."""
    done = completed_seasons(drafts)
    seen: set[int] = set()
    for row in traded or []:
        try:
            seen.add(int(str(row.get("season"))))
        except (TypeError, ValueError):
            continue
    # A season with no trades at all still has picks; make sure the next few
    # are present even when nobody has dealt one.
    for offset in range(0, MAX_FUTURE_SEASONS + 1):
        seen.add(current_season + offset)
    live = sorted(s for s in seen if s not in done and s >= current_season)
    return live[:MAX_FUTURE_SEASONS]


def build_ownership(traded: list[dict], drafts: list[dict], roster_ids: list[str],
                    current_season: int, rounds: int = 4) -> dict[str, list[dict]]:
    """Return {roster_id: [pick, ...]} for every future pick.

    Each pick: {season, round, from_roster_id, is_own}. Values are added later
    by the caller, which is the only thing that knows the market.
    """
    seasons = future_seasons(traded, drafts, current_season)
    if not seasons or not roster_ids:
        return {rid: [] for rid in roster_ids}

    # (season, round, originating roster) -> current owner
    moved: dict[tuple[int, int, str], str] = {}
    for row in traded or []:
        try:
            season = int(str(row.get("season")))
            rnd = int(str(row.get("round")))
        except (TypeError, ValueError):
            continue
        origin = str(row.get("roster_id"))
        owner = str(row.get("owner_id"))
        if season in seasons and origin and owner:
            moved[(season, rnd, origin)] = owner

    out: dict[str, list[dict]] = {rid: [] for rid in roster_ids}
    for season in seasons:
        for rnd in range(1, max(1, int(rounds or 4)) + 1):
            for origin in roster_ids:
                owner = moved.get((season, rnd, origin), origin)
                if owner not in out:
                    # A pick owned by a roster that is not in the league any
                    # more. Report nothing rather than inventing a home.
                    log.warning("Pick %s r%s from roster %s owned by unknown roster %s",
                                season, rnd, origin, owner)
                    continue
                out[owner].append({
                    "season": season,
                    "round": rnd,
                    "from_roster_id": origin,
                    "is_own": owner == origin,
                })
    for picks in out.values():
        picks.sort(key=lambda p: (p["season"], p["round"], p["from_roster_id"]))
    return out


def project_tier(rank: int, team_count: int) -> str:
    """Guess where a team's pick will land from how good its roster is.

    A PROJECTION, and labeled as one everywhere it surfaces. A weak roster
    picks early, which makes its own future first MORE valuable, and that is
    the single most common thing a dynasty owner gets wrong about their own
    rebuild.

    Thirds, because the sources publish exactly three tiers.
    """
    if team_count <= 0:
        return "mid"
    third = team_count / 3.0
    # rank 1 is the STRONGEST roster, so it drafts last.
    if rank <= third:
        return "late"
    if rank <= 2 * third:
        return "mid"
    return "early"
