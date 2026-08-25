"""Trade command parsing and math.

Parses /trade commands and computes per-side totals, delta, percentage gap,
winner, biggest asset, and unmatched list.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass, field
from datetime import datetime, timezone

from trade_scout.pick_values import parse_pick_code
from trade_scout.picks import lookup, parse_pick


class TradeParseError(Exception):
    """Raised when the trade command cannot be parsed."""


@dataclass
class TradeAsset:
    """A single asset in a trade (player or pick)."""

    raw_name: str
    player_id: str | None = None
    is_pick: bool = False
    # How a pick was priced: "exact", "average of 12 known slots", or the
    # fallback table. A pick priced exactly and one priced by fallback must
    # never look the same in a verdict.
    pick_basis: str | None = None
    pick_year: int | None = None
    pick_round: int | None = None
    value: int | None = None
    position_rank: int | None = None
    overall_rank: int | None = None
    trend_30day: float | None = None
    source: str | None = None
    flock_rank: float | None = None
    flock_tier: str | None = None
    flock_pos_rank: int | None = None


@dataclass
class TradeCommand:
    """Parsed trade command."""

    give: list[str]
    get: list[str]
    overrides: dict[str, int] = field(default_factory=dict)
    # Did the TEXT name a league, or is `league` just the default? Without
    # this the caller cannot tell "the user said DD" from "nobody said
    # anything", and those must be handled differently.
    league_explicit: bool = False
    partner: str | None = None
    league: str | None = None


def _parse_structured(text: str) -> TradeCommand | None:
    """Try to parse a structured /trade command. Returns None if format doesn't match."""
    text = text.strip()

    if text.lower().startswith("/trade"):
        text = text[len("/trade"):].strip()

    if not text:
        return None

    # Detect bare league prefix (e.g. "CL give: ..." or "DD give: ...").
    # Only CONFIGURED shortnames are recognised: an arbitrary leading token
    # treated as a league would turn typos into silent league selections.
    from trade_scout.config import known_league_shorts

    league: str | None = None
    league_explicit = False
    upper_text = text.upper().lstrip()
    for code in known_league_shorts():
        if upper_text.startswith(code) and (
            len(upper_text) == len(code) or not upper_text[len(code)].isalpha()
        ):
            league = code
            league_explicit = True
            text = text.lstrip()[len(code):].strip()
            break

    sections = [s.strip() for s in text.split("|")]

    give_assets: list[str] = []
    get_assets: list[str] = []
    overrides: dict[str, int] = {}
    partner: str | None = None

    for section in sections:
        lower = section.lower()
        if lower.startswith("give:"):
            give_str = section[len("give:"):].strip()
            give_assets = [a.strip() for a in give_str.split(",") if a.strip()]
        elif lower.startswith("get:"):
            get_str = section[len("get:"):].strip()
            get_assets = [a.strip() for a in get_str.split(",") if a.strip()]
        elif lower.startswith("flock:"):
            flock_str = section[len("flock:"):].strip()
            for pair in flock_str.split(","):
                pair = pair.strip()
                if "=" not in pair:
                    continue
                name, val = pair.rsplit("=", 1)
                try:
                    overrides[name.strip()] = int(val.strip())
                except ValueError:
                    raise TradeParseError(f"Invalid override value: {pair}")
        elif lower.startswith("partner:"):
            partner = section[len("partner:"):].strip() or None
        elif lower.startswith("league:"):
            parsed_league = section[len("league:"):].strip().upper()
            if parsed_league in known_league_shorts():
                league = parsed_league

    if not give_assets or not get_assets:
        return None

    return TradeCommand(
        give=give_assets, get=get_assets, overrides=overrides,
        league_explicit=league_explicit,
        partner=partner, league=league,
    )


def _parse_with_claude(text: str) -> TradeCommand:
    """Use Claude to interpret a natural language trade message.

    Handles inputs like:
        "trade Kyle Monangai and Rashod Bateman for Gadsden"
        "what about my CMC and a 2026 1st for Bijan"
    """
    import json
    import logging

    log = logging.getLogger(__name__)

    from trade_scout.config import known_league_shorts, league_prompt_hint

    hint = league_prompt_hint()
    league_line = (
        f"The user may reference a league by shortname ({hint}). "
        if hint else
        "The user may reference a league by a short ALL-CAPS code. "
    )
    prompt = (
        "Extract the two sides of a fantasy football trade from this message. "
        "The user is the team OWNER. Output must ALWAYS be from the owner's "
        "perspective:\n"
        "- 'give' = players/picks the OWNER sends away\n"
        "- 'get' = players/picks the OWNER receives\n\n"
        "PERSPECTIVE RULES (critical):\n"
        "- 'I give X for Y' -> give=X, get=Y\n"
        "- 'my X for his Y' -> give=X, get=Y\n"
        "- 'He gives X for Y' or 'Trade idea from [name]: he gives X for Y' -> "
        "the OTHER person gives X to the owner, so give=Y, get=X (FLIPPED)\n"
        "- '[Name] sends X for Y' -> the other person sends X, so give=Y, get=X\n"
        "- 'X for Y' with no subject -> assume the owner gives X, gets Y\n\n"
        "Words like 'and' separate multiple assets on the same side. "
        "Draft picks look like '2026 1st' or '2027 2nd'. "
        + league_line +
        "Set 'league' to that shortname if the message names one, else null. "
        "Return ONLY valid JSON, no markdown fences, in this exact format:\n"
        '{"give": ["Player Name", ...], "get": ["Player Name", ...], '
        '"league": null}\n\n'
        f"Message: {text}"
    )

    try:
        result = subprocess.run(
            ["claude", "-p", prompt, "--output-format", "json",
             "--max-turns", "1", "--allowedTools", ""],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        raise TradeParseError(
            "Couldn't understand that trade. Try: /trade give: Player1 | get: Player2"
        )

    if result.returncode != 0:
        raise TradeParseError(
            "Couldn't understand that trade. Try: /trade give: Player1 | get: Player2"
        )

    from trade_scout.report import _parse_claude_json

    raw = _parse_claude_json(result.stdout)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Claude returned non-JSON for trade parse: %s", raw[:200])
        raise TradeParseError(
            "Couldn't understand that trade. Try: /trade give: Player1 | get: Player2"
        )

    give = parsed.get("give", [])
    get = parsed.get("get", [])
    # An unrecognised value means the model did not know, which is not the same
    # as the owner saying DD. None flows on and is resolved (or refused) by the
    # caller with the explicit selector in hand.
    league = parsed.get("league")
    league = league.upper() if (
        isinstance(league, str)
        and league.upper() in known_league_shorts()) else None

    if not give or not get:
        raise TradeParseError(
            "Couldn't figure out both sides. Try: /trade give: Player1 | get: Player2"
        )

    return TradeCommand(give=give, get=get, league=league)


def parse_trade_command(text: str) -> TradeCommand:
    """Parse a trade command, structured or natural language.

    Tries structured format first (give:/get: with | separators).
    Falls back to Claude for natural language ("trade X and Y for Z").
    """
    if not text or not text.strip():
        raise TradeParseError(
            "Empty trade command. Try: /trade give: Player1 | get: Player2"
        )

    # Try structured parse first (fast, no API call)
    result = _parse_structured(text)
    if result is not None:
        return result

    # Fall back to Claude for natural language
    return _parse_with_claude(text)


def resolve_assets(
    names: list[str],
    players_map: dict,
    name_matcher,
    overrides: dict[str, int] | None = None,
    pick_market: dict | None = None,
) -> list[TradeAsset]:
    """Resolve a list of asset names to TradeAsset objects.

    Picks are detected by parse_pick_code. Players are resolved via
    the name_matcher. Unmatched players get player_id=None (surfaced, not
    silently dropped).
    """
    overrides = overrides or {}
    assets: list[TradeAsset] = []

    for name in names:
        ref = parse_pick(name)
        if ref is not None:
            entry, basis = (lookup(pick_market, ref) if pick_market else (None, ""))
            if entry:
                asset = TradeAsset(
                    raw_name=name, is_pick=True,
                    pick_year=ref.year, pick_round=ref.round_,
                    value=int(round(entry["value"])),
                    source="+".join(entry.get("sources") or []) or "market",
                    pick_basis=basis,
                )
            else:
                # The hardcoded table, and ONLY as a last resort. It discounts
                # the future 20% a year while the market pays more for the next
                # class than this one, so a verdict resting on it says so.
                legacy = parse_pick_code(name)
                if legacy is None:
                    continue
                year, rnd, val = legacy
                asset = TradeAsset(
                    raw_name=name, is_pick=True, pick_year=year, pick_round=rnd,
                    value=val, source="fallback table",
                    pick_basis="NOT IN THE MARKET: priced from a fixed table",
                )
            assets.append(asset)
            continue

        # Try player resolution
        pid = name_matcher.match(name)
        asset = TradeAsset(raw_name=name, player_id=pid)

        # Apply manual override if present
        if name in overrides:
            asset.value = overrides[name]
            asset.source = "manual override"

        assets.append(asset)

    return assets


# Flock publishes a separate board per format, and the difference is not
# cosmetic: the superflex top ten holds four quarterbacks led by Josh Allen,
# the 1QB top ten holds NONE and is led by Jahmyr Gibbs. Measured, not assumed.
FLOCK_FORMATS = {True: "superflex", False: "oneqb"}


def _try_flock_rankings(superflex: bool) -> dict[str, dict]:
    """Attempt to fetch Flock rankings for THIS league's format. {} on failure.

    `superflex` is required and has no default on purpose. It used to default,
    and the default was superflex, so the 1QB league was scored against
    superflex expert ranks while FantasyCalc and KTC were both correctly
    format-switched. The prompt mandates flagging Flock/market divergence, so
    the wrong board did not fail quietly: it manufactured a standing buy-QB
    signal in the one format where that is most expensive.
    """
    import logging

    log = logging.getLogger(__name__)
    fmt = FLOCK_FORMATS[bool(superflex)]
    try:
        from trade_scout.providers.flock import get_flock_rankings

        data = get_flock_rankings(fmt)
        log.info("Flock rankings loaded: %d players (%s)", len(data), fmt)
        return data
    except Exception as e:
        log.warning("Flock rankings unavailable for %s (non-fatal): %s", fmt, e)
        return {}


def _build_asset_dict(asset: TradeAsset, players_map: dict) -> dict:
    """Build an enriched dict for a single asset."""
    d: dict = {"name": asset.raw_name, "value": asset.value, "is_pick": asset.is_pick}
    if asset.is_pick and asset.pick_basis:
        d["pick_basis"] = asset.pick_basis
    if asset.overall_rank is not None:
        d["overall_rank"] = asset.overall_rank
    if asset.position_rank is not None:
        d["position_rank"] = asset.position_rank
    if asset.trend_30day is not None:
        d["trend_30day"] = asset.trend_30day
    if asset.flock_rank is not None:
        d["flock_rank"] = asset.flock_rank
    if asset.flock_tier is not None:
        d["flock_tier"] = asset.flock_tier
    if asset.flock_pos_rank is not None:
        d["flock_pos_rank"] = asset.flock_pos_rank
    if asset.player_id and not asset.is_pick:
        player = players_map.get(asset.player_id, {})
        d["position"] = player.get("position", "UNK")
        if player.get("age") is not None:
            d["age"] = player["age"]
        if player.get("years_exp") is not None:
            d["years_exp"] = player["years_exp"]
        injury = player.get("injury_status")
        if injury:
            d["injury_status"] = injury
    return d


def build_roster_context(
    sleeper,
    display_name: str,
    players_map: dict,
    values_data: dict,
    give_assets: list[TradeAsset],
    get_assets: list[TradeAsset],
) -> dict | None:
    """Build position-grouped roster summary with trade impact.

    Returns None if roster can't be fetched (non-fatal).
    """
    import logging

    log = logging.getLogger(__name__)

    user = sleeper.find_user_by_name(display_name)
    if user is None:
        log.warning("Could not find user '%s' in league", display_name)
        return None

    owner_id = user.get("user_id")
    if not owner_id:
        return None

    roster = sleeper.find_user_roster(owner_id)
    if roster is None:
        log.warning("No roster found for owner_id=%s", owner_id)
        return None

    player_ids = roster.get("players") or []

    # Group roster by position
    positions: dict[str, list[dict]] = {}
    for pid in player_ids:
        player = players_map.get(pid, {})
        pos = player.get("position", "UNK")
        name = player.get("full_name") or f"ID:{pid}"
        record = values_data.get(pid, {})
        entry: dict = {
            "name": name,
            "value": record.get("value", 0),
            "rank": record.get("overall_rank"),
        }
        if player.get("age") is not None:
            entry["age"] = player["age"]
        positions.setdefault(pos, []).append(entry)

    # Sort each position group by value descending
    for pos in positions:
        positions[pos].sort(key=lambda p: p["value"], reverse=True)

    # Summarize per position
    summary: dict[str, dict] = {}
    for pos, players in positions.items():
        summary[pos] = {
            "count": len(players),
            "players": players[:5],  # Top 5 per position for context
        }

    # Compute trade impact on position counts
    give_positions: dict[str, int] = {}
    get_positions: dict[str, int] = {}
    for asset in give_assets:
        if asset.is_pick or not asset.player_id:
            continue
        pos = players_map.get(asset.player_id, {}).get("position", "UNK")
        give_positions[pos] = give_positions.get(pos, 0) + 1
    for asset in get_assets:
        if asset.is_pick or not asset.player_id:
            continue
        pos = players_map.get(asset.player_id, {}).get("position", "UNK")
        get_positions[pos] = get_positions.get(pos, 0) + 1

    impact: list[str] = []
    all_pos = set(list(give_positions.keys()) + list(get_positions.keys()))
    for pos in sorted(all_pos):
        lost = give_positions.get(pos, 0)
        gained = get_positions.get(pos, 0)
        if lost and gained:
            impact.append(f"swapping {lost} {pos} for {gained} {pos}")
        elif lost:
            impact.append(f"losing {lost} {pos}")
        elif gained:
            impact.append(f"gaining {gained} {pos}")

    # Team posture heuristic
    team_posture = _classify_team_posture(positions)

    return {
        "positions": summary,
        "trade_impact": impact,
        "team_posture": team_posture,
    }


def _classify_team_posture(positions: dict[str, list[dict]]) -> dict:
    """Classify team as contending / retooling / rebuilding.

    Uses top-10 assets by value. Looks at median age and ratio of young (<25)
    vs aging (27+ for RB, 30+ for others) assets weighted by value.
    """
    # Positional age cliffs: RBs decline earlier than other positions
    AGE_CLIFFS = {"RB": 27, "QB": 33, "WR": 30, "TE": 30}
    DEFAULT_CLIFF = 30

    # Collect all rostered players with value
    all_players: list[dict] = []
    for pos, players in positions.items():
        for p in players:
            all_players.append({**p, "pos": pos})

    # Sort by value and take top 10
    all_players.sort(key=lambda p: p.get("value", 0), reverse=True)
    top = all_players[:10]

    if not top:
        return {"label": "unknown", "detail": "no valued players on roster"}

    # Compute age stats from top assets
    ages = [p["age"] for p in top if p.get("age") is not None]
    if not ages:
        return {"label": "unknown", "detail": "age data unavailable"}

    median_age = sorted(ages)[len(ages) // 2]

    # Count assets past their positional cliff vs young assets
    young_value = 0
    aging_value = 0
    total_value = 0
    for p in top:
        val = p.get("value", 0)
        age = p.get("age")
        total_value += val
        if age is None:
            continue
        cliff = AGE_CLIFFS.get(p.get("pos", ""), DEFAULT_CLIFF)
        if age >= cliff:
            aging_value += val
        elif age < 25:
            young_value += val

    aging_pct = (aging_value / total_value * 100) if total_value else 0
    young_pct = (young_value / total_value * 100) if total_value else 0

    # Classification
    if aging_pct >= 50:
        label = "contending"
        detail = (
            f"median age {median_age} among top-10 assets, "
            f"{aging_pct:.0f}% of value in win-now-window players"
        )
    elif young_pct >= 50:
        label = "rebuilding"
        detail = (
            f"median age {median_age} among top-10 assets, "
            f"{young_pct:.0f}% of value in sub-25 players"
        )
    else:
        label = "retooling"
        detail = (
            f"median age {median_age} among top-10 assets, "
            f"mixed age profile ({young_pct:.0f}% young, {aging_pct:.0f}% aging)"
        )

    return {"label": label, "median_age": median_age, "detail": detail}


def compute_trade_math(
    trade: TradeCommand,
    league: dict,
    players_map: dict,
    sleeper,
    display_name: str = "",
) -> dict:
    """Compute the full trade math: resolve, price, compare.

    Returns a dict with all fields needed by the verdict generator.
    """
    from trade_scout.name_matcher import NameMatcher
    from trade_scout.values import get_values

    matcher = NameMatcher(players_map)

    # Resolve assets on each side
    # The pick market is fetched alongside the player values. Non-fatal: a
    # trade with no picks in it never notices, and one with picks says how each
    # was priced.
    pick_market: dict = {}
    try:
        from trade_scout.values import get_pick_values
        pick_market = get_pick_values(league)
    except Exception as e:  # noqa: BLE001
        import logging
        logging.getLogger(__name__).warning(
            "Pick market unavailable (non-fatal): %s", e)

    give_assets = resolve_assets(trade.give, players_map, matcher, trade.overrides,
                                 pick_market)
    get_assets = resolve_assets(trade.get, players_map, matcher, trade.overrides,
                                pick_market)

    # Fetch values and Flock rankings in parallel
    import concurrent.futures

    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        values_future = pool.submit(get_values, league)
        flock_future = pool.submit(
            _try_flock_rankings, int(league.get('num_qbs', 1)) >= 2)
        values_data = values_future.result()
        flock_data = flock_future.result()

    source = "unknown"
    flock_available = bool(flock_data)

    # Price each asset
    unmatched: list[str] = []
    for asset in give_assets + get_assets:
        if asset.is_pick:
            continue  # Already priced
        if asset.value is not None:
            continue  # Manual override

        if asset.player_id is None:
            unmatched.append(asset.raw_name)
            continue

        record = values_data.get(asset.player_id)
        if record is None:
            unmatched.append(asset.raw_name)
            continue

        asset.value = record.get("value", 0)
        asset.overall_rank = record.get("overall_rank")
        asset.position_rank = record.get("position_rank")
        asset.trend_30day = record.get("trend_30day")
        asset.source = record.get("source", "unknown")
        source = record.get("source", source)

    # Enrich assets with Flock data (by name match)
    if flock_data:
        for asset in give_assets + get_assets:
            if asset.is_pick:
                continue
            key = asset.raw_name.lower()
            flock_entry = flock_data.get(key)
            if flock_entry is None and asset.player_id:
                # Try full name from players_map
                player = players_map.get(asset.player_id, {})
                full_name = player.get("full_name", "")
                if full_name:
                    flock_entry = flock_data.get(full_name.lower())
            if flock_entry is None:
                # Fuzzy fallback via NameMatcher-style matching
                for flock_name in flock_data:
                    if asset.raw_name.lower() in flock_name or flock_name in asset.raw_name.lower():
                        flock_entry = flock_data[flock_name]
                        break
            if flock_entry:
                asset.flock_rank = flock_entry.get("flock_rank")
                asset.flock_tier = flock_entry.get("flock_tier")
                asset.flock_pos_rank = flock_entry.get("flock_pos_rank")

    # Compute totals (unmatched players contribute 0, surfaced in reply)
    give_total = sum(a.value or 0 for a in give_assets)
    get_total = sum(a.value or 0 for a in get_assets)
    delta = get_total - give_total

    # Percentage gap relative to the larger side
    max_side = max(give_total, get_total, 1)
    pct_gap = f"{abs(delta) / max_side * 100:.1f}%"

    winner = "GET" if delta > 0 else "GIVE" if delta < 0 else "EVEN"

    # Biggest asset
    all_assets = give_assets + get_assets
    priced = [a for a in all_assets if a.value is not None and a.value > 0]
    biggest = max(priced, key=lambda a: a.value or 0) if priced else None
    biggest_name = biggest.raw_name if biggest else "N/A"

    # Format string from league settings (pre-computed in get_league_settings)
    format_str = league.get("format_str", "SF / 12-team / PPR")

    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    # Build roster context (non-fatal if it fails)
    try:
        roster_context = build_roster_context(
            sleeper, display_name, players_map, values_data,
            give_assets, get_assets,
        )
    except Exception as e:
        import logging as _log
        _log.getLogger(__name__).warning("Roster context failed (non-fatal): %s", e)
        roster_context = None

    return {
        "give_side": [_build_asset_dict(a, players_map) for a in give_assets],
        "get_side": [_build_asset_dict(a, players_map) for a in get_assets],
        "give_total": give_total,
        "get_total": get_total,
        "delta": delta,
        "pct_gap": pct_gap,
        "winner": winner,
        "biggest_asset": biggest_name,
        "unmatched": unmatched,
        "format_str": format_str,
        "source": source,
        "as_of": as_of,
        "roster_context": roster_context,
        "flock_available": flock_available,
    }


def format_basis_line(source: str, as_of: str, format_str: str,
                      league_name: str | None = None) -> str:
    """Format the basis line per the invariant (section 2).

    The league is stated FIRST because it is the fact that decides whether the
    rest of the line means anything. A Telegram reply has no chip and no
    heading; this line is the only place the league can ever appear, and for a
    long time nothing in the output named it at all.
    """
    league = f"league: {league_name} | " if league_name else ""
    return f"{league}values: {source}, as of {as_of}, {format_str}"
