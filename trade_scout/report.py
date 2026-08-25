"""Report module: Claude verdict via CLI subscription seam.

Inference runs by shelling out to `claude -p` with --output-format json,
authenticated via CLAUDE_CODE_OAUTH_TOKEN (Max subscription).
"""

from __future__ import annotations

import json
import logging
import subprocess

log = logging.getLogger(__name__)


class ClaudeAuthError(Exception):
    """Raised when the Claude CLI auth check fails."""


class ClaudeVerdictError(Exception):
    """Raised when the Claude CLI fails to produce a verdict."""


def strip_markdown_fences(text: str) -> str:
    """Strip ```json ... ``` fences from Claude output."""
    cleaned = text.strip()
    if not cleaned.startswith("```"):
        return cleaned
    lines = cleaned.split("\n")
    if lines and lines[0].startswith("```"):
        lines = lines[1:]
    if lines and lines[-1].strip() == "```":
        lines = lines[:-1]
    return "\n".join(lines).strip()


def check_claude_auth() -> bool:
    """Verify that `claude -p` is available and authenticated.

    Returns True if auth is good. Raises ClaudeAuthError with a helpful
    message if not.
    """
    try:
        result = subprocess.run(
            ["claude", "-p", "respond with exactly: ok", "--output-format", "json",
             "--max-turns", "1", "--allowedTools", ""],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        raise ClaudeAuthError(
            "Claude CLI not found. Install it or check PATH."
        )
    except subprocess.TimeoutExpired:
        raise ClaudeAuthError("Claude CLI timed out during auth check.")

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise ClaudeAuthError(
            f"Claude CLI auth failed (exit {result.returncode}). "
            "Refresh the token with `claude setup-token`. "
            f"stderr: {stderr[:200]}"
        )

    return True


def _parse_claude_json(stdout: str) -> str:
    """Parse the two-layer JSON envelope from `claude -p --output-format json`.

    The outer envelope has a `result` field containing the actual text.
    Falls back to raw stdout if parsing fails.
    """
    text = stdout.strip()
    if not text:
        return ""

    # Try parsing as JSON envelope
    try:
        envelope = json.loads(text)
        if isinstance(envelope, dict) and "result" in envelope:
            inner = envelope["result"]
            if isinstance(inner, str):
                return strip_markdown_fences(inner)
            # result might be a dict with a text field
            if isinstance(inner, dict):
                return strip_markdown_fences(inner.get("text", json.dumps(inner)))
        # No envelope structure; treat whole output as text
        return strip_markdown_fences(text)
    except json.JSONDecodeError:
        # Not JSON at all; return raw text
        return strip_markdown_fences(text)


def _run_claude(
    cmd: list[str], timeout: int = 120, max_retries: int = 1,
) -> subprocess.CompletedProcess[str]:
    """Run a Claude CLI command with retry on timeout or non-zero exit."""
    for attempt in range(max_retries + 1):
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            if result.returncode == 0:
                return result
            if attempt < max_retries:
                log.warning(
                    "Claude CLI failed (attempt %d), retrying...", attempt + 1,
                )
                continue
            raise ClaudeVerdictError(
                f"Claude CLI failed (exit {result.returncode}): "
                f"{result.stderr[:200]}"
            )
        except FileNotFoundError:
            raise ClaudeVerdictError("Claude CLI not found.")
        except subprocess.TimeoutExpired:
            if attempt < max_retries:
                log.warning(
                    "Claude CLI timeout (attempt %d), retrying...", attempt + 1,
                )
                continue
            raise ClaudeVerdictError("Claude CLI timed out after retries.")
    raise ClaudeVerdictError("Claude CLI exhausted retries.")


def generate_verdict(trade_data: dict) -> str:
    """Ask Claude to write a trade verdict from computed numbers.

    trade_data should contain: give_side, get_side, give_total, get_total,
    delta, pct_gap, winner, biggest_asset, unmatched, format_str, source,
    as_of.

    Returns the verdict text. Raises ClaudeVerdictError if Claude fails.
    """
    system_prompt = (
        "You are a seasoned dynasty fantasy football analyst writing a trade verdict "
        "for the team OWNER. You are ALWAYS advising the owner, never the trade partner.\n\n"
        "PERSPECTIVE (critical):\n"
        "- give_side = players/picks BRANDON gives away\n"
        "- get_side = players/picks BRANDON receives\n"
        "- roster_context = BRANDON's roster, BRANDON's team posture\n"
        "- 'you' in the verdict always means the owner\n"
        "- delta > 0 means the owner wins value; delta < 0 means the owner loses value\n\n"
        "You receive structured trade data including player values, "
        "rankings, age, experience, injury status, roster composition, and team posture. "
        "Think like a dynasty manager, not a calculator.\n\n"
        "DYNASTY REASONING FRAMEWORK:\n"
        "- Positional scarcity: elite RBs and QBs (in SF) are harder to replace than "
        "elite WRs. Losing the RB1 on a 2-deep RB room is different from losing a WR2 "
        "on a 5-deep WR room.\n"
        "- Age curves: RB value craters at 27. WR holds to 30-31. TE is bimodal "
        "(elite TEs hold, others fade). QB is flat to 33-35 in dynasty. A 26-year-old "
        "RB is a sell window; a 26-year-old WR is peak buy.\n"
        "- Shelf life: always note which side of the trade has the shorter remaining "
        "elite window. Picks appreciate if the team is bad, depreciate if it contends.\n\n"
        "TEAM POSTURE (from roster_context.team_posture):\n"
        "- If contending: prioritize proven production over youth. Never sell peak-value "
        "starters for picks unless massively overpaid. Flag trades that weaken the "
        "starting lineup for future upside.\n"
        "- If retooling: balanced moves are fine. Flag trades that commit hard to either "
        "direction without clear upside.\n"
        "- If rebuilding: prioritize youth and picks over aging production. Flag trades "
        "that acquire 27+ RBs or aging assets. Picks and young upside > proven vets.\n\n"
        "MANDATORY VERDICT STRUCTURE:\n"
        "1. Open with the team's posture and how this trade fits it.\n"
        "2. State the value delta and percentage gap. Reference player rankings "
        "(e.g. 'the #3 overall dynasty asset', 'the RB4'). Note 30-day trend if >5%.\n"
        "3. Analyze roster impact: what positions get thinner or deeper, whether the "
        "trade fills a need or creates a surplus.\n"
        "4. Close with a clear recommendation: take it, reject it, or what to ask for "
        "to make it work. State which side has the shorter shelf life.\n\n"
        "FLOCK EXPERT RANKINGS (from flock_rank / flock_tier):\n"
        "- Flock ranks are expert consensus from 8 dynasty rankers (SF format).\n"
        "- Tiers run S (elite) through R (replacement). Reference the tier when it "
        "tells a different story than the FantasyCalc value.\n"
        "- If Flock and FantasyCalc disagree on a player's rank by 20+ spots, flag "
        "the divergence.\n"
        "- If flock_rank is present, cite it alongside FantasyCalc rank.\n\n"
        "RULES:\n"
        "- 5-8 sentences. Direct, no fluff. No em dashes.\n"
        "- Never contradict the numbers. If the math says one side wins, say so.\n"
        "- If any players could not be priced, note them.\n"
        "- Use age and injury_status data when present. A player on IR or with a "
        "significant injury is worth flagging.\n"
        "- Do not invent facts not in the data. Reason only from what is provided."
    )

    user_prompt = json.dumps(trade_data)
    full_prompt = f"SYSTEM: {system_prompt}\n\nTRADE DATA:\n{user_prompt}"

    cmd = [
        "claude", "-p", full_prompt, "--output-format", "json",
        "--max-turns", "1", "--allowedTools", "",
    ]
    result = _run_claude(cmd, timeout=120)

    verdict = _parse_claude_json(result.stdout)
    if not verdict:
        raise ClaudeVerdictError("Claude returned empty output.")

    return verdict
