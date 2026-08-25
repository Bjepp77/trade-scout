"""Consultative trade skeleton generator.

Given a constraint like "Mike wants Caleb Williams", generates 3 trade
skeleton options (best deal, fair deal, stretch) using both rosters,
values, and positional context.
"""

from __future__ import annotations

import concurrent.futures
import json
import logging
import subprocess
from dataclasses import dataclass
from datetime import datetime, timezone

from trade_scout.config import get_league_config
from trade_scout.delivery import send_message
from trade_scout.name_matcher import NameMatcher
from trade_scout.report import ClaudeVerdictError, _parse_claude_json, _run_claude
from trade_scout.sleeper import SleeperClient
from trade_scout.trade import _classify_team_posture, _try_flock_rankings, format_basis_line
from trade_scout.values import get_values

log = logging.getLogger(__name__)

# Phrases that signal a consult intent (checked after stripping /consult prefix)
_CONSULT_PHRASES = (
    "wants my", "interested in my", "asking about my",
    "if i give him", "if i give her", "if i trade him", "if i trade her",
    "trades i could do with", "trade i could do with",
    "consult me on",
)


@dataclass
class ConsultRequest:
    """Parsed consult constraint."""

    partner_name: str
    target_players: list[str]
    league: str | None = None

    @property
    def target_player(self) -> str:
        """Primary target (first in list). For display."""
        return self.target_players[0] if self.target_players else ""

    @property
    def target_display(self) -> str:
        """Comma-joined target list for display."""
        return ", ".join(self.target_players)


class ConsultParseError(Exception):
    """Raised when the consult command cannot be parsed."""


def _split_player_list(text: str) -> list[str]:
    """Split 'Player A and Player B' or 'Player A, Player B' into a list."""
    # First split on comma
    parts: list[str] = []
    for chunk in text.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        # Then split on ' and ' within each comma segment
        for sub in chunk.split(" and "):
            sub = sub.strip()
            if sub:
                parts.append(sub)
    return parts if parts else [text.strip()]


def is_consult_command(text: str) -> bool:
    """Return True if text looks like a consult request."""
    stripped = text.strip().lower()
    if stripped.startswith("/consult"):
        return True
    if stripped.startswith("consult"):
        return True
    return any(phrase in stripped for phrase in _CONSULT_PHRASES)


def _league_from_text(text: str) -> str | None:
    """Return 'CL' or 'DD' only from an explicit LEADING league token.

    Returns None when the text does not name one. Prose is never scanned.

    The previous version guessed from anywhere in the message and was wrong on
    7 of 9 realistic inputs:

      "in my Dynasty Champs League"          -> DC, the league's real name is
                                                not "champions league"
      "Mike wants my Bijan, DD not CL"       -> CL, negation read as assertion
      "Watching the Champions League final"  -> CL, soccer
      "he said: lol my CL team is stacked"   -> CL, the trade PARTNER's pasted
                                                words chose the owner's league

    A prose heuristic cannot be tested into correctness; every green run
    licenses the next false positive. The two leagues score differently
    (1QB Standard vs Superflex PPR), so a wrong guess does not just swap the
    roster, it prices the whole trade in the wrong currency.
    """
    from trade_scout.config import known_league_shorts

    upper = str(text or "").upper().lstrip()
    for code in known_league_shorts():
        if upper.startswith(code) and (
            len(upper) == len(code) or not upper[len(code)].isalpha()
        ):
            return code
    return None


def _parse_structured_consult(text: str) -> ConsultRequest | None:
    """Try structured parse from /consult messages.

    Handles both single-line ("Mike wants Caleb Williams") and multi-line
    messages where the constraint is buried in context. Scans for known
    patterns line-by-line and across the full body.

    Returns None if the format doesn't match.
    """
    full_text = text.strip()
    body = full_text

    # Strip prefix
    lower = body.lower()
    if lower.startswith("/consult"):
        body = body[len("/consult"):].strip()
    elif lower.startswith("consult"):
        body = body[len("consult"):].strip()

    if not body:
        return None

    # An explicit leading token, or nothing. No scanning, no default: the
    # caller decides what an absent league means, and on the text path that
    # means refusing rather than guessing.
    from trade_scout.config import known_league_shorts

    league = None
    upper = body.upper().lstrip()
    for code in known_league_shorts():
        if upper.startswith(code) and (
            len(upper) == len(code) or not upper[len(code)].isalpha()
        ):
            league = code
            body = body.lstrip()[len(code):].strip()
            break

    # Phase 1: try the full body as one blob (works for single-line inputs)
    result = _try_extract_constraint(body, league)
    if result is not None:
        return result

    # Phase 2: scan line by line (works for multi-paragraph pastes)
    for line in body.splitlines():
        line = line.strip()
        if not line:
            continue
        result = _try_extract_constraint(line, league)
        if result is not None:
            return result

    return None


def _try_extract_constraint(text: str, league: str | None) -> ConsultRequest | None:
    """Try to extract partner + target from a text chunk.

    Checks multiple phrase patterns, returns ConsultRequest or None.
    """
    lower = text.lower()

    # "if I give him/her <player>" patterns: partner is earlier in context
    # These need special handling since partner isn't adjacent
    give_patterns = (
        "if i give him ", "if i give her ",
        "if i trade him ", "if i trade her ",
        "if i send him ", "if i send her ",
    )
    for phrase in give_patterns:
        pidx = lower.find(phrase)
        if pidx != -1:
            target = text[pidx + len(phrase):].strip().rstrip(".?!")
            if target:
                # Partner must come from "with <name>" earlier in text
                partner = _extract_partner_from_context(text[:pidx], lower[:pidx])
                if partner:
                    return ConsultRequest(
                        partner_name=partner,
                        target_players=_split_player_list(target),
                        league=league,
                    )

    # "trades I could do with <partner>" pattern
    with_patterns = (
        "trades i could do with ",
        "trade i could do with ",
        "trades with ",
        "deals i could do with ",
        "deal i could do with ",
    )
    for phrase in with_patterns:
        pidx = lower.find(phrase)
        if pidx != -1:
            rest = text[pidx + len(phrase):].strip()
            # Partner name ends at "if", "where", comma, or end of string
            partner = _extract_name_until_stop(rest)
            if partner:
                # Target should follow "give him" or similar later
                target = _extract_target_after(
                    text[pidx + len(phrase) + len(partner):],
                )
                if target:
                    return ConsultRequest(
                        partner_name=partner,
                        target_players=_split_player_list(target),
                        league=league,
                    )

    # "wants my" and similar possessive phrases
    for phrase in ("wants my", "interested in my", "asking about my"):
        pidx = lower.find(phrase)
        if pidx != -1:
            partner = text[:pidx].strip()
            target = text[pidx + len(phrase):].strip()
            if partner and target:
                return ConsultRequest(
                    partner_name=partner,
                    target_players=_split_player_list(target),
                    league=league,
                )

    # Generic "<partner> wants <player>"
    idx = lower.find(" wants ")
    if idx != -1:
        partner = text[:idx].strip()
        target = text[idx + len(" wants "):].strip()
        if partner and target:
            return ConsultRequest(
                partner_name=partner,
                target_players=_split_player_list(target),
                league=league,
            )

    return None


def _extract_partner_from_context(
    text: str, lower: str,
) -> str | None:
    """Extract partner name from context like 'do with theonb if'."""
    # Look for "with <name>" working backwards
    idx = lower.rfind(" with ")
    if idx != -1:
        after = text[idx + len(" with "):].strip()
        return _extract_name_until_stop(after)

    # Look for "from <name>" (e.g. "get back from NinjaChristian")
    idx = lower.rfind(" from ")
    if idx != -1:
        after = text[idx + len(" from "):].strip()
        return _extract_name_until_stop(after)

    # Look for "to <name>" as fallback
    idx = lower.rfind(" to ")
    if idx != -1:
        after = text[idx + len(" to "):].strip()
        return _extract_name_until_stop(after)

    return None


def _extract_name_until_stop(text: str) -> str | None:
    """Extract a name token from text, stopping at stop words or punctuation."""
    import re
    # Take text up to first stop word or sentence end
    match = re.match(
        r"^(.+?)(?:\s+(?:if|where|when|and|but|because|so)\b|[,;.]|$)",
        text.strip(),
        re.IGNORECASE,
    )
    if match:
        name = match.group(1).strip()
        if name:
            return name
    return text.strip() or None


def _extract_target_after(text: str) -> str | None:
    """Extract target player from text after partner, looking for give patterns."""
    lower = text.lower()
    for phrase in (
        "if i give him ", "if i give her ",
        "if i trade him ", "if i trade her ",
        "if i send him ", "if i send her ",
        "give him ", "give her ",
        "giving him ", "giving her ",
        "trading ", "giving ",
    ):
        idx = lower.find(phrase)
        if idx != -1:
            target = text[idx + len(phrase):].strip().rstrip(".?!")
            if target:
                return target
    return None


def _parse_with_claude(text: str) -> ConsultRequest:
    """Use Claude to interpret a natural language consult message."""
    from trade_scout.config import league_prompt_hint

    hint = league_prompt_hint()
    league_line = (
        f"- league: the league shortname if the message names one ({hint}), "
        "else null.\n\n"
        if hint else
        "- league: the league shortname (a short ALL-CAPS code) if the "
        "message names one, else null.\n\n"
    )
    prompt = (
        "Extract a consultative trade constraint from this fantasy football message. "
        "The user (the team OWNER) is describing a scenario where a league partner "
        "wants one or more of the owner's players, or the owner is considering "
        "trading players to a partner. The message may be multi-paragraph with "
        "quoted context from the partner. "
        "Focus on the user's actual ask, which is usually at the END of the message.\n\n"
        "Extract:\n"
        "- partner_name: the league partner's name/display name/username\n"
        "- target_players: a LIST of ALL players the owner would give up. "
        "There may be one or multiple.\n"
        + league_line +
        "Examples:\n"
        "- 'I give him Caleb Williams' -> target_players=['Caleb Williams']\n"
        "- 'I give him RJ Harvey and Caleb Williams' -> "
        "target_players=['RJ Harvey', 'Caleb Williams']\n"
        "- 'Mike wants my Bijan and a 2026 1st' -> "
        "target_players=['Bijan', '2026 1st']\n\n"
        "Return ONLY valid JSON, no markdown fences, in this exact format:\n"
        '{"partner_name": "Mike", "target_players": ["Caleb Williams"], '
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
        raise ConsultParseError(
            "Couldn't understand that consult request. "
            "Try: /consult CL Mike wants Caleb Williams"
        )

    if result.returncode != 0:
        raise ConsultParseError(
            "Couldn't understand that consult request. "
            "Try: /consult CL Mike wants Caleb Williams"
        )

    raw = _parse_claude_json(result.stdout)

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        log.warning("Claude returned non-JSON for consult parse: %s", raw[:200])
        raise ConsultParseError(
            "Couldn't understand that consult request. "
            "Try: /consult CL Mike wants Caleb Williams"
        )

    partner = parsed.get("partner_name", "")
    # Support both old single-value and new list format
    targets = parsed.get("target_players") or parsed.get("target_player", "")
    if isinstance(targets, str):
        targets = _split_player_list(targets) if targets else []
    # An unrecognised value means the model did not know, which is not the
    # same as the owner naming a league. None flows on and is resolved (or
    # refused) by the caller with the explicit selector in hand.
    from trade_scout.config import known_league_shorts

    league = parsed.get("league")
    league = league.upper() if (
        isinstance(league, str)
        and league.upper() in known_league_shorts()) else None

    if not partner or not targets:
        raise ConsultParseError(
            "Couldn't figure out who wants what. "
            "Try: /consult CL Mike wants Caleb Williams"
        )

    return ConsultRequest(
        partner_name=partner, target_players=targets, league=league,
    )


def parse_consult_command(text: str) -> ConsultRequest:
    """Parse a consult command, structured or natural language.

    Tries structured format first. Falls back to Claude for NL.
    """
    if not text or not text.strip():
        raise ConsultParseError(
            "Empty consult command. Try: /consult CL Mike wants Caleb Williams"
        )

    result = _parse_structured_consult(text)
    if result is not None:
        return result

    return _parse_with_claude(text)


def _enrich_roster(
    player_ids: list[str],
    players_map: dict,
    values_data: dict[str, dict],
    flock_data: dict[str, dict],
    top_n: int = 15,
) -> list[dict]:
    """Build an enriched roster snapshot for the top N assets by value.

    Each entry: {name, position, value, age, flock_rank, flock_tier}.
    Sorted by value descending.
    """
    entries: list[dict] = []
    for pid in player_ids:
        player = players_map.get(pid, {})
        name = player.get("full_name") or f"ID:{pid}"
        pos = player.get("position", "UNK")
        record = values_data.get(pid, {})
        value = record.get("value", 0)

        entry: dict = {
            "name": name,
            "position": pos,
            "value": value,
        }
        if player.get("age") is not None:
            entry["age"] = player["age"]

        # Flock enrichment by name
        flock_key = name.lower()
        flock_entry = flock_data.get(flock_key)
        if flock_entry is None:
            # Fuzzy substring fallback
            for fk in flock_data:
                if flock_key in fk or fk in flock_key:
                    flock_entry = flock_data[fk]
                    break
        if flock_entry:
            entry["flock_rank"] = flock_entry.get("flock_rank")
            entry["flock_tier"] = flock_entry.get("flock_tier")

        entries.append(entry)

    entries.sort(key=lambda e: e["value"], reverse=True)
    return entries[:top_n]


def _build_posture(
    player_ids: list[str],
    players_map: dict,
    values_data: dict[str, dict],
) -> dict:
    """Build team posture from roster player IDs. Wrapper around _classify_team_posture."""
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

    for pos in positions:
        positions[pos].sort(key=lambda p: p["value"], reverse=True)

    return _classify_team_posture(positions)


def generate_skeletons(
    constraint: ConsultRequest,
    brandon_roster: list[dict],
    partner_roster: list[dict],
    brandon_posture: dict,
    league: dict,
    flock_available: bool,
) -> list[dict]:
    """Call Claude to generate 3 trade skeleton options.

    Returns parsed list of option dicts.
    Raises ClaudeVerdictError if Claude fails.
    """
    format_str = league.get("format_str", "SF / 12-team / PPR")

    system_prompt = (
        "You are a dynasty fantasy football trade architect. The OWNER is giving "
        f"{constraint.target_display} to a league partner. Your job is to "
        "propose 3 concrete trade skeleton options from the partner's roster.\n\n"
        "You receive:\n"
        "- The owner's top assets (valued, ranked)\n"
        "- The partner's top assets (valued, ranked)\n"
        "- The owner's team posture\n"
        f"- League format: {format_str}\n\n"
        "RETURN EXACTLY 3 OPTIONS:\n"
        "1. BEST DEAL: maximizes value extraction for the owner. Ask for a slight overpay.\n"
        "2. FAIR DEAL: roughly even value, both sides benefit.\n"
        "3. STRETCH: aggressive ask that might still fly. Significant overpay for the owner.\n\n"
        "FLOCK EXPERT RANKINGS (MANDATORY):\n"
        "- Roster data includes flock_rank (expert consensus rank, SF) and "
        "flock_tier (S/A/B/C/D/E/F/G/H/I/J/K/L/M/N/O/P/Q/R).\n"
        "- You MUST cite Flock tier for every player mentioned in every rationale. "
        "Format: 'Player Name (Flock tier A)' or 'Player Name (Flock tier S, rank 4.2)'.\n"
        "- If Flock and FantasyCalc disagree on rank by 15+ spots, flag the divergence.\n"
        "- If flock_rank/flock_tier is missing for a player, note 'unranked by Flock'.\n\n"
        "RULES:\n"
        "- Only propose players/picks from the partner's roster in brandon_gets.\n"
        "- brandon_gives MUST include ALL target players the owner is giving up. "
        "Do not drop any. You may add a minor piece (pick, depth player) from "
        "the owner to balance if needed, but prefer simplicity.\n"
        "- Consider positional needs, age curves, team posture alignment.\n"
        "- value_delta is positive when the owner wins value.\n"
        "- Each rationale is 1-2 sentences. Direct, no fluff, no em dashes.\n"
        "- Draft picks should be formatted as '2026 1st', '2027 2nd', etc.\n\n"
        "Return ONLY valid JSON, no markdown fences, in this exact format:\n"
        "[\n"
        '  {"label": "BEST DEAL", "brandon_gives": ["Player"], '
        '"brandon_gets": ["Player", "2026 1st"], "value_delta": 1200, '
        '"rationale": "Explanation."},\n'
        '  {"label": "FAIR DEAL", ...},\n'
        '  {"label": "STRETCH", ...}\n'
        "]"
    )

    user_data = {
        "constraint": {
            "partner_name": constraint.partner_name,
            "brandon_gives": constraint.target_players,
        },
        "brandon_roster": brandon_roster,
        "brandon_posture": brandon_posture,
        "partner_roster": partner_roster,
        "format": format_str,
        "flock_available": flock_available,
    }

    user_prompt = json.dumps(user_data)
    full_prompt = f"SYSTEM: {system_prompt}\n\nDATA:\n{user_prompt}"

    cmd = [
        "claude", "-p", full_prompt, "--output-format", "json",
        "--max-turns", "1", "--allowedTools", "",
    ]
    result = _run_claude(cmd, timeout=120)
    raw = _parse_claude_json(result.stdout)

    if not raw:
        raise ClaudeVerdictError("Claude returned empty output for consult.")

    try:
        options = json.loads(raw)
    except json.JSONDecodeError:
        raise ClaudeVerdictError(f"Claude returned non-JSON for consult: {raw[:200]}")

    if not isinstance(options, list) or len(options) < 1:
        raise ClaudeVerdictError("Claude did not return a list of trade options.")

    return options


def _try_flock_calculator(
    options: list[dict],
    league: str,
) -> list[dict] | None:
    """Run the 3 trade skeletons through the Flock trade calculator.

    Returns list of Flock calc result dicts (one per option), or None on failure.
    Non-fatal: any failure returns None and the consult reply omits Flock grades.
    """
    try:
        from trade_scout.providers.flock import run_flock_trade_calc
    except ImportError:
        log.warning("Flock provider not available for calculator")
        return None

    trades: list[dict] = []
    for opt in options[:3]:
        trades.append({
            "give": opt.get("brandon_gives", []),
            "get": opt.get("brandon_gets", []),
        })

    try:
        results = run_flock_trade_calc(trades, league=league)
        log.info("Flock calculator returned %d results", len(results))
        return results
    except Exception as e:
        log.warning("Flock calculator failed (non-fatal): %s", e)
        return None


def _format_reply(
    constraint: ConsultRequest,
    options: list[dict],
    source: str,
    as_of: str,
    format_str: str,
    flock_calc_results: list[dict] | None = None,
    league_name: str | None = None,
) -> str:
    """Format the 3 trade skeletons into a Telegram-ready reply."""
    lines: list[str] = []
    lines.append(
        f"Trade options giving {constraint.target_display} "
        f"to {constraint.partner_name}:"
    )
    lines.append("")

    labels = ["BEST DEAL", "FAIR DEAL", "STRETCH"]
    for i, opt in enumerate(options[:3]):
        label = opt.get("label", labels[i] if i < len(labels) else f"OPTION {i + 1}")
        gives = opt.get("brandon_gives", [])
        gets = opt.get("brandon_gets", [])
        delta = opt.get("value_delta", 0)
        rationale = opt.get("rationale", "")

        # Format delta with sign and percentage hint
        if delta > 0:
            delta_str = f"+{delta:,} (you win)"
        elif delta < 0:
            delta_str = f"{delta:,} (you lose)"
        else:
            delta_str = "0 (even)"

        lines.append(f"{i + 1}. {label}")
        lines.append(f"Give: {', '.join(gives)}")
        lines.append(f"Get: {', '.join(gets)}")
        lines.append(f"Delta: {delta_str}")

        # Flock calculator verdict for this option
        if flock_calc_results and i < len(flock_calc_results):
            fc = flock_calc_results[i]
            if fc.get("verdict") and fc["verdict"] != "error":
                flock_line = f"Flock: {fc['verdict']}"
                uv = fc.get("user_value", 0)
                ov = fc.get("opponent_value", 0)
                if uv or ov:
                    flock_line += f" ({uv} vs {ov})"
                lines.append(flock_line)

        lines.append(f"Why: {rationale}")
        lines.append("")

    basis = format_basis_line(source, as_of, format_str, league_name)
    lines.append(basis)

    return "\n".join(lines)


def _find_partner_roster(
    sleeper,
    partner_name: str,
) -> tuple[str, list[str]] | None:
    """Find the partner's roster by display name.

    Tries exact match via find_user_by_name first, then partial/case-insensitive
    match against all users.

    Returns (matched_display_name, player_ids) or None.
    """
    # Exact match (already case-insensitive in SleeperClient)
    user = sleeper.find_user_by_name(partner_name)
    if user:
        owner_id = user.get("user_id", "")
        roster = sleeper.find_user_roster(owner_id)
        if roster:
            return (user.get("display_name", partner_name), roster.get("players") or [])

    # Partial match against all users
    users = sleeper.get_users()
    partner_lower = partner_name.lower()
    for u in users:
        dn = u.get("display_name", "")
        if partner_lower in dn.lower():
            owner_id = u.get("user_id", "")
            roster = sleeper.find_user_roster(owner_id)
            if roster:
                return (dn, roster.get("players") or [])

    return None


def run_consult(chat_id: str, text: str) -> None:
    """Orchestrator: parse -> fetch -> enrich -> generate -> reply."""
    try:
        constraint = parse_consult_command(text)
    except ConsultParseError as e:
        send_message(chat_id, f"Couldn't run that consult: {e}")
        return

    # Load league config
    try:
        # The caller's explicit selector (the console chip) wins; the text
        # token is the message path's way of saying the same thing. If
        # neither named a league, refuse rather than guess: this used to be
        # inferred from prose and got it wrong on 7 of 9 realistic messages.
        from trade_scout.config import explicit_league
        chosen = explicit_league()
        if chosen and constraint.league and chosen != constraint.league:
            raise RuntimeError(
                f"league conflict: you selected {chosen} but the message says "
                f"{constraint.league}. Say which one and send it again.")
        resolved_league = chosen or constraint.league
        league_cfg = get_league_config(resolved_league)
    except RuntimeError as e:
        send_message(chat_id, f"Couldn't run that consult: {e}")
        return

    # Fetch Sleeper data
    try:
        sleeper = SleeperClient(league_id=league_cfg["league_id"])
        league = sleeper.get_league_settings()
        players_map = sleeper.get_players_map()
    except Exception as e:
        send_message(chat_id, f"Sleeper API error: {e}")
        return

    # Find the owner's roster
    brandon_user = sleeper.find_user_by_name(league_cfg["display_name"])
    if brandon_user is None:
        send_message(chat_id, "Couldn't find your roster in this league.")
        return
    brandon_roster_obj = sleeper.find_user_roster(brandon_user["user_id"])
    if brandon_roster_obj is None:
        send_message(chat_id, "Couldn't load your roster.")
        return
    brandon_pids = brandon_roster_obj.get("players") or []

    # Verify target players are on the owner's roster
    matcher = NameMatcher(players_map)
    for target in constraint.target_players:
        target_pid = matcher.match(target)
        if target_pid and target_pid not in brandon_pids:
            send_message(
                chat_id,
                f"{target} isn't on your roster in this league.",
            )
            return

    # Find partner's roster
    partner_result = _find_partner_roster(sleeper, constraint.partner_name)
    if partner_result is None:
        send_message(
            chat_id,
            f"Couldn't find '{constraint.partner_name}' in this league. "
            "Check the display name on Sleeper.",
        )
        return
    partner_display, partner_pids = partner_result

    # Parallel fetch: values + flock
    with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
        values_future = pool.submit(get_values, league)
        flock_future = pool.submit(
            _try_flock_rankings, int(league.get('num_qbs', 1)) >= 2)
        values_data = values_future.result()
        flock_data = flock_future.result()

    flock_available = bool(flock_data)

    # Enrich both rosters (top 15 each)
    brandon_roster = _enrich_roster(brandon_pids, players_map, values_data, flock_data)
    partner_roster = _enrich_roster(partner_pids, players_map, values_data, flock_data)

    # The owner's team posture
    brandon_posture = _build_posture(brandon_pids, players_map, values_data)

    # Determine source and as_of from values data
    source = "FantasyCalc"
    for record in values_data.values():
        src = record.get("source")
        if src:
            source = src
            break
    as_of = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    format_str = league.get("format_str", "SF / 12-team / PPR")

    # Generate trade skeletons via Claude
    try:
        options = generate_skeletons(
            constraint=constraint,
            brandon_roster=brandon_roster,
            partner_roster=partner_roster,
            brandon_posture=brandon_posture,
            league=league,
            flock_available=flock_available,
        )
    except ClaudeVerdictError as e:
        send_message(chat_id, f"Couldn't generate trade options: {e}")
        return

    # Run Flock trade calculator on each skeleton (non-fatal)
    flock_calc_results = _try_flock_calculator(options, league_cfg["short"])

    # Format and send
    reply = _format_reply(
        constraint, options, source, as_of, format_str, flock_calc_results,
        league_name=league_cfg["league_name"],
    )

    notes: list[str] = []
    if not flock_available:
        notes.append("Flock rankings unavailable, using FantasyCalc only.")
    if flock_calc_results is None:
        notes.append("Flock calculator unavailable.")
    if notes:
        reply += "\n" + "\n".join(notes)

    send_message(chat_id, reply)
    log.info(
        "Consult reply sent for %s giving %s",
        constraint.partner_name, constraint.target_display,
    )
