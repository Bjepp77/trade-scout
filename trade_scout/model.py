"""The proprietary player score. Pure functions, no I/O, no clock.

This is the one place that decides what a player is worth TO BRANDON, as
opposed to what the market says he is worth. Everything here is deterministic
and testable; the fetching lives in providers/.

WHAT BLENDS AND WHAT DOES NOT
-----------------------------
FantasyCalc and KTC both publish a per-player VALUE, so after normalisation
they can be averaged into one market number.

Flock publishes a RANK and a TIER. There is no honest conversion from a rank to
a price, so Flock is never averaged into the value. It is carried as a
CONVICTION SIGNAL: when the experts and the market disagree sharply about a
player, that disagreement is the interesting thing, and it is reported rather
than smoothed away. Inventing a number is the one thing PRD section 2 forbids.

THE FOUR ADJUSTMENTS
--------------------
1. Scarcity, via VORP. A player is worth what he gives you OVER the freely
   available alternative at his position, not his raw price. This is the
   honest way to express "an elite TE in 1QB is scarcer than a WR": it falls
   out of the replacement level instead of being asserted as a fudge factor.
2. Age curve, by position. An RB and a WR of the same age and the same price
   are not the same asset.
3. Trend. Available in TWO modes, deliberately, so the difference is visible
   rather than a hidden modelling choice. See trend_modes below.
4. Roster fit. Your fifth good WR is worth less to you than to a team that
   has none, and that is a fact about your roster, not about the player.

Every adjustment is a multiplier on the VORP-adjusted market value, every one
is reported separately, and any of them can be switched off by setting its
weight to 0. A score you cannot take apart is a score you cannot trust.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Where each position peaks and how fast it falls. Dynasty consensus shape:
# backs are done early, receivers hold, quarterbacks hold longest.
#   (peak_start, peak_end, decay_per_year_after_peak, ramp_per_year_before_peak)
AGE_CURVES: dict[str, tuple[float, float, float, float]] = {
    "RB": (22.0, 25.0, 0.115, 0.030),
    "WR": (23.0, 27.5, 0.070, 0.045),
    "TE": (24.0, 29.0, 0.065, 0.055),
    "QB": (24.0, 33.0, 0.055, 0.040),
}
DEFAULT_CURVE = (23.0, 28.0, 0.070, 0.040)

# Starters per position, used to find replacement level. FLEX demand is spread
# across RB/WR/TE, which is why these are fractional.
DEFAULT_STARTERS: dict[str, float] = {"QB": 1.0, "RB": 2.6, "WR": 3.4, "TE": 1.3}

# Superflex changes the answer completely: a second quarterback slot roughly
# doubles league-wide QB demand and pulls the replacement QB far down the list,
# which is exactly what makes an elite QB scarce there.
#
# Hardcoding the 1QB numbers gave Superflex a QB scarcity multiplier of 0.568,
# DISCOUNTING quarterbacks in the format where they matter most. The owner's
# second-most-valuable CL asset scored below players worth half as much. Caught
# on the live board, not in a test.
SUPERFLEX_STARTERS: dict[str, float] = {"QB": 1.9, "RB": 2.4, "WR": 3.1, "TE": 1.2}


def starters_for(num_qbs: int = 1) -> dict[str, float]:
    """Starting requirements for the league's build."""
    return dict(SUPERFLEX_STARTERS if int(num_qbs or 1) >= 2 else DEFAULT_STARTERS)

# How far a player's market value may be moved by any one adjustment. Without
# a clamp a single weird input (a 36-year-old with a top-5 price, a trend of
# +2000) swings the whole trade, and the model stops being explainable.
CLAMP_LOW, CLAMP_HIGH = 0.55, 1.45

# And a clamp on the PRODUCT, which is the one that was missing. Bounding each
# factor bounds nothing: four factors at 0.8 multiply to 0.41, well under any
# individual floor. On the live board the product reached 0.394 for eight
# players, and Jonathan Taylor came out 57% under his market price from
# adjustments that each looked reasonable on their own.
TOTAL_LOW, TOTAL_HIGH = 0.68, 1.50


@dataclass
class Weights:
    """Every knob, in one place. Set any to 0 to switch that effect off."""

    fantasycalc: float = 1.0
    ktc: float = 1.0
    age: float = 1.0
    scarcity: float = 1.0
    trend: float = 1.0
    fit: float = 1.0
    # THE TREND IS ALREADY IN THE PRICE. FantasyCalc publishes /values/current:
    # a player who fell 255 over 30 days is quoted at his POST-fall price, and
    # trend_30day describes how he got there. Multiplying today's price by the
    # recent direction charges him for the same move twice.
    #
    # At 0.25 that double-count was brutal: Caleb Williams, whom both sources
    # price at 7199 and agree on within 3.5%, scored 3957. The trend alone took
    # 37%, on top of a decline the 7199 already contained.
    #
    # What survives is a small tilt for momentum persistence, which is a real
    # but weak effect. 0.08 means a full standard deviation moves the score
    # about 8%. It expresses "this one is trending" without restating the price.
    trend_strength: float = 0.08
    # Same reasoning for 'averaged' mode, which repriced just as hard, only
    # more gently.
    trend_share: float = 0.08
    # Retooling: pay up for youth, discount the old. 0 = age-neutral.
    youth_bias: float = 0.0
    extra: dict = field(default_factory=dict)


def clamp(x: float, lo: float = CLAMP_LOW, hi: float = CLAMP_HIGH) -> float:
    return max(lo, min(hi, x))


def normalise_values(values: list[float], target_max: float = 10000.0) -> float:
    """Scale factor that puts a source's top player at target_max.

    Kept for the simple case, but NOT sufficient on its own: see
    quantile_normalise. Two sources can share a maximum and still disagree
    about the entire shape of the curve beneath it.
    """
    top = max((v for v in values if v is not None), default=0.0)
    return (target_max / top) if top > 0 else 0.0


def reference_curve(values: list[float | None]) -> list[float]:
    """A source's values, sorted high to low. The shape everything maps onto."""
    return sorted((v for v in values if v is not None), reverse=True)


def quantile_normalise(source_values: dict, ref: list[float]) -> dict:
    """Re-express one source's values on another's curve, by rank.

    THIS IS NOT COSMETIC. Measured on the live data, FantasyCalc and KTC agree
    at the very top and diverge violently below it:

        percentile     FantasyCalc      KTC
              0%            11076      9999
             25%             1953      4028
             50%             1093      2559
             75%              312      1500

    At the median KTC says a player is worth more than twice what FantasyCalc
    says. That is not two opinions about a player, it is two differently shaped
    curves, and averaging them raw would inflate every bench player on every
    roster and quietly rewrite what a depth piece is worth in a trade.

    Mapping by RANK keeps what each source actually claims -- its ordering --
    and discards only the arbitrary shape of its scale. After this, a
    disagreement between the two means they rank the player differently, which
    is the only kind of disagreement worth reporting.

    Returns {key: mapped_value}. An empty reference returns the input.
    """
    if not ref or not source_values:
        return dict(source_values)
    ordered = sorted(
        ((k, v) for k, v in source_values.items() if v is not None),
        key=lambda kv: kv[1], reverse=True)
    n = len(ordered)
    if n == 0:
        return {}
    out: dict = {}
    last = len(ref) - 1
    for i, (k, _v) in enumerate(ordered):
        pct = (i / (n - 1)) if n > 1 else 0.0
        out[k] = ref[min(last, int(round(pct * last)))]
    return out


def scarcity_multiplier(replacement: dict[str, float], strength: float = 0.5,
                        ) -> dict[str, float]:
    """Per-position multiplier from how cheap that position's replacement is.

    A position whose last startable player is cheap is a position where the
    elite are scarce, because the drop from the top to the freely available is
    steeper. That is the same idea as VORP, expressed as a factor so it can
    rank an entire roster instead of flooring every bench player at zero.
    """
    vals = [v for v in replacement.values() if v > 0]
    if not vals:
        return {k: 1.0 for k in replacement}
    mean = sum(vals) / len(vals)
    if mean <= 0:
        return {k: 1.0 for k in replacement}
    return {
        pos: clamp(1.0 + strength * ((mean - r) / mean))
        for pos, r in replacement.items()
    }


def age_factor(position: str, age: float | None, youth_bias: float = 0.0) -> float:
    """Multiplier from the position's age curve. Unknown age is neutral."""
    if age is None:
        return 1.0
    peak_start, peak_end, decay, ramp = AGE_CURVES.get(
        str(position).upper(), DEFAULT_CURVE)
    if age < peak_start:
        # Young and not yet peaked: slightly discounted for the wait, then
        # credited back by youth_bias if the owner is retooling.
        f = 1.0 - (peak_start - age) * ramp
    elif age <= peak_end:
        f = 1.0
    else:
        f = 1.0 - (age - peak_end) * decay
    if youth_bias:
        # Positive bias lifts anyone below the peak window and cuts anyone past
        # it, on top of the curve itself.
        if age < peak_start:
            f += youth_bias * (peak_start - age) * 0.5
        elif age > peak_end:
            f -= youth_bias * (age - peak_end) * 0.5
    return clamp(f)


def replacement_levels(
    by_position: dict[str, list[float]],
    num_teams: int = 12,
    starters: dict[str, float] | None = None,
) -> dict[str, float]:
    """Value of the last STARTABLE player at each position.

    Everything above this line is what you actually gain by rostering someone;
    everything at or below it is replaceable from waivers. This is where
    positional scarcity comes from, rather than from a hand-set multiplier.
    """
    starters = starters or DEFAULT_STARTERS
    out: dict[str, float] = {}
    for pos, vals in by_position.items():
        ranked = sorted((v for v in vals if v is not None), reverse=True)
        if not ranked:
            out[pos] = 0.0
            continue
        need = starters.get(str(pos).upper(), 1.0) * num_teams
        idx = max(0, min(len(ranked) - 1, int(round(need)) - 1))
        out[pos] = ranked[idx]
    return out


def vorp(value: float, replacement: float) -> float:
    """Value over replacement, floored at zero.

    Floored because a negative asset is not a thing you can trade: nobody pays
    you to take a player, they just decline.
    """
    return max(0.0, value - replacement)


def trend_z(trend: float | None, spread: float) -> float:
    """Trend expressed in standard deviations. Unknown or flat is 0."""
    if trend is None or spread <= 0:
        return 0.0
    return trend / spread


def fit_factor(position_depth_above_replacement: int,
               starters_needed: float,
               weight: float = 1.0) -> float:
    """Diminishing returns on a position you are already deep at.

    The first startable body at a position is worth full price. Each surplus
    one past your starting requirement is worth progressively less TO YOU,
    which is exactly why a fifth good receiver is trade bait and not depth.
    """
    surplus = max(0, position_depth_above_replacement - int(round(starters_needed)))
    if surplus <= 0:
        return 1.0
    return clamp(1.0 - weight * (0.06 * surplus))


def blend_market_n(
    values: dict[str, float | None],
    weights: dict[str, float],
) -> tuple[float | None, dict]:
    """Weight N already-NORMALISED source values into one market number.

    `values` maps source key -> value ON THE SHARED REFERENCE SCALE (i.e.
    after quantile_normalise); normalisation is deliberately not this
    function's job. Returns (blended, detail) where detail keeps every
    per-source value, which sources were present, and the worst pairwise
    disagreement in percent of the blend -- a single-source player is visibly
    single-source rather than quietly treated as a consensus.
    """
    detail: dict = {"per_source": {}, "sources": [], "disagreement_pct": None}
    parts: list[tuple[float, float]] = []
    for key, val in values.items():
        if val is None:
            detail["per_source"][key] = None
            continue
        w = weights.get(key, 1.0)
        detail["per_source"][key] = round(val, 1)
        if w > 0:
            detail["sources"].append(key)
            parts.append((val, w))
    if not parts:
        return None, detail

    total_w = sum(w for _, w in parts)
    blended = sum(v * w for v, w in parts) / total_w

    present = [v for v, _ in parts]
    if len(present) >= 2 and blended > 0:
        gap = max(present) - min(present)
        detail["disagreement_pct"] = round(gap / blended * 100, 1)
    return blended, detail


def blend_market(
    fc_value: float | None,
    ktc_value: float | None,
    fc_scale: float,
    ktc_scale: float,
    w: Weights,
) -> tuple[float | None, dict]:
    """Normalise and weight the two VALUE sources into one market number.

    Returns (blended, detail). detail always names which sources were present,
    so a single-source player is visibly single-source rather than quietly
    treated as a consensus.
    """
    parts = []
    detail: dict = {"fantasycalc": None, "ktc": None, "sources": []}
    if fc_value is not None and w.fantasycalc > 0:
        n = fc_value * fc_scale
        detail["fantasycalc"] = round(n, 1)
        detail["sources"].append("FantasyCalc")
        parts.append((n, w.fantasycalc))
    if ktc_value is not None and w.ktc > 0:
        n = ktc_value * ktc_scale
        detail["ktc"] = round(n, 1)
        detail["sources"].append("KeepTradeCut")
        parts.append((n, w.ktc))
    if not parts:
        return None, detail

    total_w = sum(x[1] for x in parts)
    blended = sum(v * wt for v, wt in parts) / total_w

    # Disagreement is the signal, not the noise. Reported in percent of the
    # blend so it reads the same for a 9000 asset and a 900 one.
    if detail["fantasycalc"] is not None and detail["ktc"] is not None and blended > 0:
        gap = abs(detail["fantasycalc"] - detail["ktc"])
        detail["disagreement_pct"] = round(gap / blended * 100, 1)
    else:
        detail["disagreement_pct"] = None
    return blended, detail


def score_players(
    players: list[dict],
    num_teams: int = 12,
    weights: Weights | None = None,
    starters: dict[str, float] | None = None,
    owner_roster_ids: set | None = None,
) -> dict:
    """Score every player. Returns {players: [...], meta: {...}}.

    Each input player: {id, name, position, age, fantasycalc: {...}|None,
    ktc: {...}|None, flock: {rank, tier}|None}.

    Each output carries the full derivation: the normalised inputs, every
    multiplier separately, and BOTH trend modes. A score you cannot take apart
    is a score you cannot argue with, and this one exists to be argued with.
    """
    w = weights or Weights()
    starters = starters or DEFAULT_STARTERS

    # Reference curve = FantasyCalc's shape. It is the incumbent that every
    # existing verdict was written against, so mapping onto it means the new
    # model does not silently restate every past trade in a new currency.
    fc_raw = {i: (p.get("fantasycalc") or {}).get("value") for i, p in enumerate(players)}
    ktc_raw = {i: (p.get("ktc") or {}).get("value") for i, p in enumerate(players)}
    ref = reference_curve(list(fc_raw.values()))
    if not ref:
        # No FantasyCalc at all: fall back to KTC's own curve as the reference
        # so the model still works on one source, visibly.
        ref = reference_curve(list(ktc_raw.values()))
    fc_norm = quantile_normalise({k: v for k, v in fc_raw.items() if v is not None}, ref)
    ktc_norm = quantile_normalise({k: v for k, v in ktc_raw.items() if v is not None}, ref)

    # Pass 1: market blend for everyone, so replacement levels are computed
    # from the blend rather than from one source's opinion.
    staged = []
    for i, p in enumerate(players):
        fc = p.get("fantasycalc") or {}
        market, detail = blend_market(fc_norm.get(i), ktc_norm.get(i), 1.0, 1.0, w)
        staged.append({"p": p, "market": market, "detail": detail,
                       "trend": fc.get("trend_30day")})

    by_pos: dict[str, list[float]] = {}
    for s in staged:
        if s["market"] is None:
            continue
        by_pos.setdefault(str(s["p"].get("position") or "UNK").upper(), []).append(s["market"])
    repl = replacement_levels(by_pos, num_teams, starters)
    scarcity = scarcity_multiplier(repl) if w.scarcity > 0 else {}

    # Trend spread, so 'a big mover' is defined by this week's data rather than
    # by a constant somebody typed once.
    trends = [abs(s["trend"]) for s in staged if s["trend"] is not None]
    spread = (sum(trends) / len(trends) * 1.5) if trends else 0.0

    # How many startable bodies the owner already has at each position.
    depth: dict[str, int] = {}
    if owner_roster_ids:
        for s in staged:
            if s["market"] is None or s["p"].get("id") not in owner_roster_ids:
                continue
            pos = str(s["p"].get("position") or "UNK").upper()
            if s["market"] > repl.get(pos, 0.0):
                depth[pos] = depth.get(pos, 0) + 1

    out = []
    for s in staged:
        p, market, detail = s["p"], s["market"], s["detail"]
        pos = str(p.get("position") or "UNK").upper()
        rec = {
            "id": p.get("id"), "name": p.get("name"), "position": pos,
            "age": p.get("age"), "sources": detail,
            "flock": p.get("flock"),
        }
        if market is None:
            # No value source knew him. Say so; never score him as zero.
            rec.update({"market": None, "score": None, "score_trend_averaged": None,
                        "unpriced": True, "factors": {}})
            out.append(rec)
            continue

        replacement = repl.get(pos, 0.0)
        # VORP is reported but NOT the score. Flooring at replacement makes
        # every bench player identical at zero, which is fine for "what is the
        # surplus in this trade" and useless for "rank my roster". The score
        # expresses scarcity as a multiplier instead, so it orders everyone.
        surplus = vorp(market, replacement)
        f_scar = scarcity.get(pos, 1.0) if w.scarcity > 0 else 1.0

        f_age = age_factor(pos, p.get("age"), w.youth_bias) if w.age > 0 else 1.0
        owned = bool(owner_roster_ids) and p.get("id") in owner_roster_ids
        f_fit = fit_factor(depth.get(pos, 0), starters.get(pos, 1.0), w.fit) \
            if (w.fit > 0 and owned) else 1.0

        z = trend_z(s["trend"], spread)
        # The TOTAL adjustment, clamped as one thing. Reported so the score can
        # be taken apart: if this is pinned at a bound, the factors below it are
        # no longer the whole story and the reader should know.
        f_total = clamp(f_scar * f_age * f_fit, TOTAL_LOW, TOTAL_HIGH)
        base = market * f_total
        # MODE A, 'factored': momentum is a MODIFIER on today's price. The
        # market number is treated as correct and the trend nudges it.
        f_trend = clamp(1.0 + w.trend_strength * z) if w.trend > 0 else 1.0
        score_factored = base * f_trend
        # MODE B, 'averaged': momentum is a CORRECTION to today's price, folded
        # into the market number itself at trend_share before anything else
        # touches it. Same inputs, a different claim about what a trend means.
        market_avg = (market * (1.0 - w.trend_share)
                      + market * (1.0 + z) * w.trend_share) if w.trend > 0 else market
        score_averaged = market_avg * f_total

        rec.update({
            "market": round(market, 1),
            "replacement": round(replacement, 1),
            "vorp": round(surplus, 1),
            "unpriced": False,
            "trend_30day": s["trend"],
            "trend_z": round(z, 2),
            "score": round(score_factored, 1),
            "score_trend_averaged": round(score_averaged, 1),
            "trend_mode_delta": round(score_factored - score_averaged, 1),
            "factors": {
                "age": round(f_age, 3),
                "fit": round(f_fit, 3),
                "scarcity": round(f_scar, 3),
                "trend_factored": round(f_trend, 3),
                "total": round(f_total, 3),
                "total_clamped": f_total in (TOTAL_LOW, TOTAL_HIGH),
            },
        })
        out.append(rec)

    out.sort(key=lambda r: (r["score"] is None, -(r["score"] or 0), r["name"]))
    return {
        "players": out,
        "meta": {
            "num_teams": num_teams,
            "replacement_levels": {k: round(v, 1) for k, v in repl.items()},
            "trend_spread": round(spread, 1),
            "owner_depth": depth,
            "scarcity_multipliers": {k: round(v, 3) for k, v in scarcity.items()},
            "reference_source": "FantasyCalc curve (KTC quantile-mapped onto it)",
            "weights": {
                "fantasycalc": w.fantasycalc, "ktc": w.ktc, "age": w.age,
                "scarcity": w.scarcity, "trend": w.trend, "fit": w.fit,
                "trend_strength": w.trend_strength, "trend_share": w.trend_share,
                "youth_bias": w.youth_bias,
            },
        },
    }
