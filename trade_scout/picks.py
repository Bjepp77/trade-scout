"""Draft pick valuation, from the market instead of from constants.

WHAT THIS REPLACES. pick_values.py priced a pick from a hardcoded table: a
superflex 1st was 5000 flat, discounted 20% for every year into the future.
Measured against the live market that was not merely imprecise, it had the SIGN
wrong:

    pick        hardcoded   KTC market   off by
    2026 1st         5000         5546     -10%
    2027 1st         4000         7062     -43%
    2028 1st         3200         5122     -37%

A 2027 first trades ABOVE a 2026 first. An unknown pick in a class the market
likes carries option value, and a flat future discount is blind to it. The old
table therefore told you to sell future firsts, every time, in one direction.

It also broke the PRD's central rule: get_values() is "the ONE seam for values,
nothing else knows where values come from". Picks were a second, hidden
valuation system nobody audited.

CANONICAL KEYS. The two sources name picks differently:

    KTC          "2027 Early 1st"      (year, tier, round)
    FantasyCalc  "2027 1st (Early)"    (year, round, tier)
                 "2026 Pick 1.01"      (year, exact slot)
                 "2026 1st"            (year, round, untiered)

Both reduce to (year, round, tier) with tier in early/mid/late/None, plus an
exact-slot form for the current draft where the order is known.

SCALES. KTC's pick values live on KTC's scale and FantasyCalc's on its own, the
same mismatch the player values had: "2027 Early 1st" is 7062 to KTC and 4488
to FantasyCalc. They are quantile-mapped pick-to-pick by rank, so what survives
is each source's ORDERING of the picks, which is the part they actually
disagree about.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from datetime import datetime, timezone

log = logging.getLogger(__name__)

TIERS = ("early", "mid", "late")
_ORDINAL = {1: "1st", 2: "2nd", 3: "3rd", 4: "4th", 5: "5th"}


@dataclass(frozen=True)
class PickRef:
    """A draft pick, as named by a human or a source."""

    year: int
    round_: int
    tier: str | None = None      # early | mid | late | None
    slot: int | None = None      # 1..12 when the exact selection is known

    def key(self) -> str:
        if self.slot is not None:
            return f"{self.year}-{self.round_}.{self.slot:02d}"
        return f"{self.year}-{self.round_}-{self.tier or 'any'}"

    def label(self) -> str:
        ordinal = _ORDINAL.get(self.round_, f"{self.round_}th")
        if self.slot is not None:
            return f"{self.year} {self.round_}.{self.slot:02d}"
        if self.tier:
            return f"{self.year} {self.tier.capitalize()} {ordinal}"
        return f"{self.year} {ordinal}"


# "2026 1st", "2027 early 2nd", "2026 1st (late)", "2026 1.05", "2026_1"
_YEAR = r"(20\d\d)"
_RND = r"([1-9])(?:st|nd|rd|th)?"
_TIER = r"(early|mid|middle|late)"

_PATTERNS = (
    # 2026 Pick 1.01 / 2026 1.01
    re.compile(rf"^{_YEAR}\s+(?:pick\s+)?([1-9])\.(\d{{1,2}})$", re.I),
    # 2027 Early 1st  |  2027 early 1
    re.compile(rf"^{_YEAR}\s+{_TIER}\s+{_RND}$", re.I),
    # 2027 1st (Early)
    re.compile(rf"^{_YEAR}\s+{_RND}\s*\(\s*{_TIER}\s*\)$", re.I),
    # 2026 1st  |  2026_1  |  2026 1st round pick
    re.compile(rf"^{_YEAR}[\s_]+{_RND}(?:\s*round)?(?:\s*pick)?$", re.I),
)


def parse_pick(text: str) -> PickRef | None:
    """Parse a human or source pick reference. None when it is not a pick."""
    s = re.sub(r"\s+", " ", str(text or "").strip())
    if not s:
        return None

    m = _PATTERNS[0].match(s)
    if m:
        return PickRef(int(m.group(1)), int(m.group(2)), slot=int(m.group(3)))

    m = _PATTERNS[1].match(s)
    if m:
        return PickRef(int(m.group(1)), int(m.group(3)), tier=_norm_tier(m.group(2)))

    m = _PATTERNS[2].match(s)
    if m:
        return PickRef(int(m.group(1)), int(m.group(2)), tier=_norm_tier(m.group(3)))

    m = _PATTERNS[3].match(s)
    if m:
        return PickRef(int(m.group(1)), int(m.group(2)))
    return None


def _norm_tier(t: str | None) -> str | None:
    if not t:
        return None
    t = t.lower()
    return "mid" if t == "middle" else t


def current_draft_year() -> int:
    return datetime.now(timezone.utc).year


def _calibrate(source: dict[str, float],
               reference: dict[str, float]) -> dict[str, float]:
    """Put one source's picks on the other's scale, fitted on the OVERLAP.

    Rank-mapping was tried first and is wrong here, for a reason worth
    recording. The two sources do not price the same universe: FantasyCalc
    publishes every slot of the current draft (2026 Pick 1.01 through 1.12)
    while KeepTradeCut publishes tiers only. Of 76 FantasyCalc keys and 36 KTC
    keys, just 12 are shared. Mapping "rank 1 of 36" onto a 76-entry curve
    therefore reads KTC's top pick off an entry KTC never priced, and produced
    apparent disagreements of 26 to 44 percent where players sit at 0.4 to 13.

    So the fit uses only the picks BOTH sources name, then interpolates every
    other KTC value through that fitted curve. Below two shared points there is
    nothing to fit and the source is returned unchanged, which the caller sees
    as a single-source entry rather than a confident blend.
    """
    shared = sorted(
        ((source[k], reference[k]) for k in set(source) & set(reference)),
        key=lambda pair: pair[0],
    )
    if len(shared) < 2:
        return dict(source)

    xs = [p[0] for p in shared]
    ys = [p[1] for p in shared]

    def project(v: float) -> float:
        if v <= xs[0]:
            # Below the fitted range: scale by the lowest anchor rather than
            # clamping to it, or every cheap pick collapses onto one value.
            return v * (ys[0] / xs[0]) if xs[0] else v
        if v >= xs[-1]:
            return v * (ys[-1] / xs[-1]) if xs[-1] else v
        for i in range(1, len(xs)):
            if v <= xs[i]:
                span = xs[i] - xs[i - 1]
                t = (v - xs[i - 1]) / span if span else 0.0
                return ys[i - 1] + t * (ys[i] - ys[i - 1])
        return v

    return {k: project(v) for k, v in source.items()}


def calibration_error(source: dict[str, float],
                      reference: dict[str, float]) -> dict[str, float]:
    """Leave-one-out disagreement, in percent, per shared key.

    On a shared key the calibration is fitted THROUGH that point, so comparing
    the two sources there returns zero by construction. Reporting that as
    agreement would be inventing corroboration: it measures the fit, not the
    sources.

    Leaving each point out of its own fit gives the real question: with the
    other picks calibrated, how far off is this one? That number can be large,
    and it should be visible when it is.
    """
    keys = sorted(set(source) & set(reference))
    if len(keys) < 3:
        return {}
    out: dict[str, float] = {}
    for k in keys:
        held = {j: source[j] for j in keys if j != k}
        ref = {j: reference[j] for j in keys if j != k}
        projected = _calibrate({**held, k: source[k]}, ref).get(k)
        actual = reference[k]
        if projected is not None and actual:
            out[k] = round(abs(projected - actual) / actual * 100, 1)
    return out


def build_pick_market(fantasycalc_picks: dict[str, float],
                      ktc_picks: dict[str, float]) -> dict[str, dict]:
    """Blend the two sources into one value per canonical pick key.

    Pure. Both inputs are {canonical_key: raw_value}. FantasyCalc is the
    reference scale, as it is for players, so past verdicts stay comparable.
    """
    ktc_mapped = _calibrate(ktc_picks, fantasycalc_picks)
    # Honest disagreement: the fit is exact on shared keys, so it has to be
    # measured with each point held out of its own calibration.
    loo = calibration_error(ktc_picks, fantasycalc_picks)

    out: dict[str, dict] = {}
    for key in set(fantasycalc_picks) | set(ktc_mapped):
        fc = fantasycalc_picks.get(key)
        kt = ktc_mapped.get(key)
        parts = [v for v in (fc, kt) if v is not None]
        if not parts:
            continue
        names = []
        if fc is not None:
            names.append("FantasyCalc")
        if kt is not None:
            names.append("KeepTradeCut")
        blended = sum(parts) / len(parts)
        entry = {
            "value": round(blended, 1),
            "fantasycalc": round(fc, 1) if fc is not None else None,
            "ktc": round(kt, 1) if kt is not None else None,
            "sources": names,
        }
        # Not abs(fc - kt): after calibration those are equal on every shared
        # key by construction. This is the leave-one-out error, or None when
        # only one source priced the pick.
        entry["disagreement_pct"] = loo.get(key) if (fc is not None and kt is not None) else None
        out[key] = entry
    return out


def lookup(market: dict[str, dict], ref: PickRef) -> tuple[dict | None, str]:
    """Find the best market entry for a pick, and say how it was found.

    Resolution order, most specific first. The second return value is the
    basis, so a reply can state that an exact 2026 1.03 was priced but a
    "2027 1st" fell back to the mid tier. A pick priced by a fallback and a
    pick priced exactly must not look the same.
    """
    # A slot or an explicit tier is a specific request: honour it exactly.
    if ref.slot is not None or ref.tier is not None:
        exact = market.get(ref.key())
        if exact:
            return exact, "exact"

    if ref.slot is None:
        # An untiered future pick: the honest default is the middle of the
        # round, because nobody knows where a team will finish.
        #
        # The MID TIER is preferred over the source's own untiered entry even
        # when both exist, because only FantasyCalc publishes an untiered
        # value: taking it would price the commonest way of naming a pick
        # ("2027 1st") from one source while a tiered request got two.
        if ref.tier is None:
            mid = market.get(PickRef(ref.year, ref.round_, tier="mid").key())
            if mid and len(mid.get("sources") or []) > 1:
                return mid, "mid tier (no tier given)"
            # For the CURRENT draft the order is known, so the sources publish
            # every slot instead of tiers. A pick whose slot is not yet decided
            # is the expectation over all of them, which is a better answer
            # than one source's untiered figure.
            slots = [v for k, v in market.items()
                     if k.startswith(f"{ref.year}-{ref.round_}.")]
            if len(slots) >= 4:
                avg = sum(x["value"] for x in slots) / len(slots)
                srcs = sorted({n for x in slots for n in (x.get("sources") or [])})
                return ({"value": round(avg, 1), "fantasycalc": None, "ktc": None,
                         "sources": srcs, "disagreement_pct": None},
                        f"average of {len(slots)} known slots")
            plain = market.get(ref.key())
            if plain:
                return plain, "untiered (as published)"
            if mid:
                return mid, "mid tier (no tier given)"
            tiers = [market.get(PickRef(ref.year, ref.round_, tier=t).key()) for t in TIERS]
            found = [t for t in tiers if t]
            if found:
                avg = sum(t["value"] for t in found) / len(found)
                return ({**found[0], "value": round(avg, 1)},
                        f"average of {len(found)} tiers")
        else:
            # A tier was asked for but that tier is not published: fall back to
            # the untiered entry for the round rather than to another tier.
            plain = market.get(PickRef(ref.year, ref.round_).key())
            if plain:
                return plain, "round average (tier not published)"
    return None, "not in the market"


# --------------------------------------------------------------- extractors

def extract_ktc_picks(ktc: dict[str, dict]) -> dict[str, float]:
    """Pull pick entries out of a KTC values map.

    KTC marks them with position RDP and names them "2027 Early 1st".
    """
    out: dict[str, float] = {}
    for rec in (ktc or {}).values():
        if (rec.get("position") or "").upper() != "RDP":
            continue
        ref = parse_pick(rec.get("name") or "")
        value = rec.get("value")
        if ref and value:
            out[ref.key()] = float(value)
    return out


def extract_fantasycalc_picks(raw: list) -> dict[str, float]:
    """Pull pick entries out of a raw FantasyCalc response.

    Marked by position == "PICK" and carrying a SYNTHETIC sleeperId like
    "DP_0_0". Filtering on a missing sleeperId does not work and was the first
    thing tried: every entry has one, so that filter matched nothing. Filtering
    on the name would also drag in real players whose names happen to contain
    an ordinal.

    Because the id is synthetic but present, these already flow into
    get_values() keyed by "DP_0_0" and similar, where they sit inert: no roster
    ever references those ids.
    """
    out: dict[str, float] = {}
    for entry in (raw or []):
        player = entry.get("player") or {}
        if (player.get("position") or "").upper() != "PICK":
            continue
        ref = parse_pick(player.get("name") or "")
        value = entry.get("value")
        if ref and value:
            out[ref.key()] = float(value)
    return out
