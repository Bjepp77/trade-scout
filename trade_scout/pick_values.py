"""Draft pick valuation. Salvaged from dynasty-intel agent_09.

Values are round-based with a future discount. The seam is simple:
pick_value(year_offset, round) -> int.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Superflex base values by round
_SUPERFLEX_BASE: dict[int, int] = {
    1: 5000,
    2: 2500,
    3: 1200,
}
_SUPERFLEX_DEFAULT = 500  # 4th round+
_1QB_MULTIPLIER = 0.6
_FUTURE_DISCOUNT_RATE = 0.8  # 20% per year out

_CURRENT_DRAFT_YEAR = datetime.now(timezone.utc).year

# Matches "2026 1st", "2027 2nd", "2026_1", etc.
_PICK_RE = re.compile(r"^(\d{4})[\s_](\d)(?:st|nd|rd|th)?$", re.IGNORECASE)

# Also match "2026 1st round pick" style
_PICK_RE_VERBOSE = re.compile(
    r"^(\d{4})\s+(\d)(?:st|nd|rd|th)?\s*(?:round)?\s*(?:pick)?$",
    re.IGNORECASE,
)


def pick_value(
    year_offset: int,
    round_: int,
    format_: str = "superflex",
) -> int:
    """Return the trade value of a draft pick.

    Args:
        year_offset: 0 = this year's draft, 1 = next year, etc.
        round_: Draft round (1, 2, 3, 4, ...).
        format_: "superflex" (default) or "1qb".

    Returns:
        Integer trade value after format multiplier and future discount.
    """
    base = _SUPERFLEX_BASE.get(round_, _SUPERFLEX_DEFAULT)
    if format_ == "1qb":
        base = int(base * _1QB_MULTIPLIER)
    discounted = base * (_FUTURE_DISCOUNT_RATE ** year_offset)
    return int(discounted)


def parse_pick_code(code: str) -> tuple[int, int, int] | None:
    """Parse '2026 1st' or '2026_1' -> (year, round, value). None if not a pick."""
    code = code.strip()
    m = _PICK_RE.match(code) or _PICK_RE_VERBOSE.match(code)
    if not m:
        return None
    year = int(m.group(1))
    rnd = int(m.group(2))
    year_offset = max(0, year - _CURRENT_DRAFT_YEAR)
    value = pick_value(year_offset=year_offset, round_=rnd)
    return (year, rnd, value)
