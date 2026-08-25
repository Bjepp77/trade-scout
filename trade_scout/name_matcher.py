"""Fuzzy name-to-player_id resolution. Salvaged from dynasty-intel agent_09.

Uses rapidfuzz for fuzzy matching when exact match fails.
On a miss, returns None (surfaced as "couldn't find: <name>" per invariant).
"""

from __future__ import annotations

import logging
import re
import unicodedata
from collections.abc import Iterable

log = logging.getLogger(__name__)

try:
    from rapidfuzz import fuzz, process
    HAS_RAPIDFUZZ = True
except ImportError:
    HAS_RAPIDFUZZ = False

_SUFFIX_RE = re.compile(r"\b(jr|sr|ii|iii|iv|v)\b\.?", re.IGNORECASE)
_PUNCT_RE = re.compile(r"[^\w\s]")


def _normalize(name: str) -> str:
    """Lowercase, strip accents, drop punctuation, drop Jr/Sr/etc."""
    n = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    n = n.lower().strip()
    n = _SUFFIX_RE.sub("", n).strip()
    n = _PUNCT_RE.sub("", n)
    n = re.sub(r"\s+", " ", n)
    return n


class NameMatcher:
    """Resolve human names to Sleeper player_ids.

    Build once with a Sleeper players dict, reuse across the run.

        matcher = NameMatcher(sleeper_players_dict)
        pid = matcher.match("Bijan Robinson")
    """

    def __init__(self, sleeper_players: dict[str, dict]) -> None:
        self.players = sleeper_players
        # name (normalized) -> list of (player_id, position)
        self._by_name: dict[str, list[tuple[str, str]]] = {}
        for pid, p in sleeper_players.items():
            first = (p.get("first_name") or "").strip()
            last = (p.get("last_name") or "").strip()
            if not first and not last:
                continue
            full = _normalize(f"{first} {last}")
            pos = (p.get("position") or "").upper()
            self._by_name.setdefault(full, []).append((pid, pos))
        log.info("NameMatcher built: %d unique normalized names", len(self._by_name))

    def match(
        self,
        name: str,
        position: str | None = None,
        allowed_positions: Iterable[str] | None = None,
        threshold: float = 88.0,
    ) -> str | None:
        """Return a Sleeper player_id, or None if no confident match.

        - position: if set, only match candidates whose position equals this.
        - allowed_positions: match if candidate position is in this set.
        - threshold: fuzz score 0-100; default 88 is conservative.
        """
        norm = _normalize(name)

        def passes_pos(p: str) -> bool:
            if position is not None:
                return p == position.upper()
            if allowed_positions is not None:
                return p in {x.upper() for x in allowed_positions}
            return True

        # Exact normalized match
        candidates = [(pid, p) for pid, p in self._by_name.get(norm, []) if passes_pos(p)]
        if len(candidates) == 1:
            return candidates[0][0]
        if len(candidates) > 1:
            log.warning(
                "Ambiguous exact match for %r (%s): %d candidates",
                name, position, len(candidates),
            )
            return None

        # Fuzzy fallback
        if not HAS_RAPIDFUZZ:
            return None

        pool = [
            (full, pid)
            for full, lst in self._by_name.items()
            for pid, p in lst
            if passes_pos(p)
        ]
        if not pool:
            return None

        names_only = [n for n, _ in pool]
        result = process.extractOne(norm, names_only, scorer=fuzz.token_set_ratio)
        if result is None:
            return None

        match_name, score, idx = result
        if score < threshold:
            log.debug(
                "Low-confidence fuzzy match for %r: %r (%.1f) - rejected",
                name, match_name, score,
            )
            return None

        pid = pool[idx][1]
        log.debug("Fuzzy matched %r -> %r (%s, score=%.1f)", name, match_name, pid, score)
        return pid
