"""Sleeper API client. Read-only, no auth required.

Base URL: https://api.sleeper.app/v1
"""

from __future__ import annotations

import logging

import httpx

from trade_scout import cache

log = logging.getLogger(__name__)

SLEEPER_BASE = "https://api.sleeper.app/v1"


class SleeperError(Exception):
    """Raised on Sleeper API failures."""


class SleeperClient:
    """Read-only Sleeper API client."""

    def __init__(self, league_id: str | None = None) -> None:
        # An empty string is refused rather than treated as absent: a blank
        # id silently falling back to some other league is exactly the
        # mispricing bug the league-routing tests exist to prevent.
        if league_id is None or not str(league_id).strip():
            raise SleeperError(
                "No league id supplied. Check the league configuration "
                "rather than falling back to another league.")
        self.league_id = league_id
        self._http = httpx.Client(base_url=SLEEPER_BASE, timeout=30)
        self._players_cache: dict[str, dict] | None = None
        self._users_cache: list[dict] | None = None
        self._rosters_cache: list[dict] | None = None

    def _get(self, path: str) -> dict | list:
        resp = self._http.get(path)
        resp.raise_for_status()
        return resp.json()

    def get_league(self) -> dict:
        """GET /league/{league_id} - full league object."""
        try:
            data = self._get(f"/league/{self.league_id}")
            if not isinstance(data, dict):
                raise SleeperError("Unexpected league response shape")
            return data
        except (httpx.HTTPStatusError, httpx.InvalidURL) as e:
            raise SleeperError(f"Failed to fetch league: {e}") from e

    def get_league_settings(self) -> dict:
        """Derive numQbs, numTeams, ppr, isDynasty from the league object.

        PRD 3.2: numQbs=2 if roster_positions contains SUPER_FLEX or two QB slots.
        """
        league = self.get_league()
        roster_positions = league.get("roster_positions", [])
        scoring = league.get("scoring_settings", {})

        qb_count = roster_positions.count("QB")
        has_sf = "SUPER_FLEX" in roster_positions
        num_qbs = 2 if (has_sf or qb_count >= 2) else 1

        num_teams = league.get("total_rosters", 12)
        ppr = scoring.get("rec", 1.0)

        fmt = "SF" if num_qbs >= 2 else "1QB"
        ppr_str = f"{ppr} PPR" if ppr == 0.5 else ("PPR" if ppr >= 1 else "Standard")
        format_str = f"{fmt} / {num_teams}-team / {ppr_str}"

        return {
            "num_qbs": num_qbs,
            "num_teams": num_teams,
            "ppr": ppr,
            "is_dynasty": True,
            "league_id": self.league_id,
            "season": league.get("season"),
            "format_str": format_str,
        }

    def get_rosters(self) -> list[dict]:
        """GET /league/{league_id}/rosters - all team rosters. Cached."""
        if self._rosters_cache is not None:
            return self._rosters_cache
        try:
            data = self._get(f"/league/{self.league_id}/rosters")
            if not isinstance(data, list):
                raise SleeperError("Unexpected rosters response shape")
            self._rosters_cache = data
            return data
        except httpx.HTTPStatusError as e:
            raise SleeperError(f"Failed to fetch rosters: {e}") from e

    def get_users(self) -> list[dict]:
        """GET /league/{league_id}/users - map user_id to display_name. Cached."""
        if self._users_cache is not None:
            return self._users_cache
        try:
            data = self._get(f"/league/{self.league_id}/users")
            if not isinstance(data, list):
                raise SleeperError("Unexpected users response shape")
            self._users_cache = data
            return data
        except httpx.HTTPStatusError as e:
            raise SleeperError(f"Failed to fetch users: {e}") from e


    # The players map is the only thing here worth caching across processes.
    PLAYERS_CACHE_MAX_AGE = 20 * 3600  # a working day
    PLAYERS_CACHE_NAME = "sleeper-players.json"

    def _read_players_cache(self) -> dict | None:
        got = cache.read_json(self.PLAYERS_CACHE_NAME, self.PLAYERS_CACHE_MAX_AGE)
        return got if isinstance(got, dict) else None

    def _write_players_cache(self, data: dict) -> None:
        cache.write_json(self.PLAYERS_CACHE_NAME, data)

    def get_players_map(self) -> dict[str, dict]:
        """GET /players/nfl - the full player map, cached to disk for a day.

        This response is 14.6 MB and Sleeper's own documentation asks that it be
        fetched at most once a day. The in-process cache did nothing for us:
        every request spawns a FRESH process, so an hourly refresh across two
        leagues was pulling roughly 700 MB a day for a file that changes when
        somebody signs a practice-squad receiver.

        The cache is deliberately dumb: one file, an mtime check, and a full
        refetch when it is stale. No invalidation protocol, because the only
        consequence of being a few hours behind is a rookie showing as ID:<n>,
        which the board already renders honestly.
        """
        if self._players_cache is not None:
            return self._players_cache

        cached = self._read_players_cache()
        if cached is not None:
            self._players_cache = cached
            log.info("Sleeper players map from disk cache: %d players", len(cached))
            return cached

        try:
            data = self._get("/players/nfl")
            if not isinstance(data, dict):
                raise SleeperError("Unexpected players response shape")
            self._players_cache = data
            self._write_players_cache(data)
            log.info("Sleeper players map loaded: %d players", len(data))
            return data
        except httpx.HTTPStatusError as e:
            raise SleeperError(f"Failed to fetch players: {e}") from e

    def get_traded_picks(self) -> list[dict]:
        """GET /league/{id}/traded_picks - the EXCEPTIONS, not every pick."""
        try:
            data = self._get(f"/league/{self.league_id}/traded_picks")
            return data if isinstance(data, list) else []
        except httpx.HTTPStatusError as e:
            raise SleeperError(f"Failed to fetch traded picks: {e}") from e

    def get_drafts(self) -> list[dict]:
        """GET /league/{id}/drafts - used to tell a spent draft from a future one."""
        try:
            data = self._get(f"/league/{self.league_id}/drafts")
            return data if isinstance(data, list) else []
        except httpx.HTTPStatusError as e:
            raise SleeperError(f"Failed to fetch drafts: {e}") from e

    def find_user_roster(self, owner_id: str) -> dict | None:
        """Find a roster by owner_id."""
        rosters = self.get_rosters()
        for r in rosters:
            if r.get("owner_id") == owner_id:
                return r
        return None

    def find_user_by_name(self, display_name: str) -> dict | None:
        """Find a user by display_name (case-insensitive)."""
        users = self.get_users()
        lower = display_name.lower()
        for u in users:
            if (u.get("display_name") or "").lower() == lower:
                return u
        return None


def get_user(username: str) -> dict:
    """Look up a Sleeper user by username. League-independent, for setup."""
    resp = httpx.get(f"{SLEEPER_BASE}/user/{username}", timeout=30)
    if resp.status_code == 404:
        raise SleeperError(f"No Sleeper user named {username!r}")
    resp.raise_for_status()
    data = resp.json()
    if not data:
        raise SleeperError(f"No Sleeper user named {username!r}")
    return data


def get_user_leagues(user_id: str, season: str) -> list[dict]:
    """All NFL leagues for a user in a season. League-independent, for setup."""
    resp = httpx.get(
        f"{SLEEPER_BASE}/user/{user_id}/leagues/nfl/{season}", timeout=30)
    resp.raise_for_status()
    return resp.json() or []
