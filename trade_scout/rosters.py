"""Roster dump for the console: who owns which player, priced.

Called as: python -m trade_scout rosters

READ ONLY. This module computes no trade, sends no message, and calls no LLM.
It exists because the console needs to show two rosters side by side, and the
join it needs is one the engine already knows how to do:

    Sleeper rosters (player ids)
      + Sleeper players map (names, positions, ages)
      + get_values()      (the SAME provider chain /trade prices with)

Doing that join in JavaScript would mean a second opinion about what a player
is worth, and two opinions about value is how the board and the trade verdict
start disagreeing about the same trade. So it happens here, once.

The Sleeper players map is 14.6 MB and Sleeper asks that it be fetched at most
once a day. That is the whole reason this is a command and not an HTTP call the
module makes itself: the big fetch happens here and what leaves this process is
only the ~180 rostered players, priced, at roughly 30 KB.

Output is a single JSON object on stdout, behind the same sentinel the reply
path uses, so the caller parses one thing in one way.
"""

from __future__ import annotations

import json
import logging
import os
import sys

from trade_scout import history
from trade_scout.model import Weights, score_players, starters_for
from trade_scout.pick_ownership import build_ownership, project_tier
from trade_scout.picks import PickRef, lookup
from trade_scout.providers.keeptradecut import KTCError, get_ktc_values, normalise_name
from trade_scout.sleeper import SleeperClient, SleeperError
from trade_scout.trade import _try_flock_rankings
from trade_scout.values import ValuesError, get_values

log = logging.getLogger(__name__)

SENTINEL = "TRADE_SCOUT_ROSTERS>>>"


def _ktc_for(name: str, ktc: dict) -> dict | None:
    """KTC joins by NAME, not by sleeper id. It publishes no sleeperID."""
    return ktc.get(normalise_name(name)) if ktc else None


def _flock_for(name: str, flock: dict) -> dict | None:
    """Flock joins by lowercased name, the same fuzzy risk KTC carries."""
    if not flock:
        return None
    hit = flock.get(str(name or "").lower())
    if hit is None:
        return None
    return {
        "rank": hit.get("flock_rank"),
        "tier": hit.get("flock_tier"),
        "position_rank": hit.get("flock_pos_rank"),
        "rank_delta": hit.get("rank_delta"),
    }


def _player_record(pid: str, players: dict, values: dict, ktc: dict | None = None,
                   flock: dict | None = None) -> dict:
    """One rostered player, priced. Unknown ids still get a row."""
    p = players.get(pid) or {}
    v = values.get(pid) or {}

    name = p.get("full_name")
    if not name:
        first = p.get("first_name") or ""
        last = p.get("last_name") or ""
        name = (f"{first} {last}").strip()
    # A player Sleeper does not know is still ON the roster. Showing the raw id
    # is honest; dropping the row would silently shrink somebody's team.
    if not name:
        name = f"ID:{pid}"

    return {
        "id": str(pid),
        "name": name,
        "position": p.get("position") or "UNK",
        "team": p.get("team") or None,
        "age": p.get("age"),
        "years_exp": p.get("years_exp"),
        "status": p.get("status") or None,
        "injury_status": p.get("injury_status") or None,
        # value is None, not 0, when the provider has no opinion. Zero is a
        # price; None is an absence, and the console must be able to tell them
        # apart or every unvalued rookie sorts as worthless.
        "value": v.get("value"),
        "overall_rank": v.get("overall_rank"),
        "position_rank": v.get("position_rank"),
        "trend_30day": v.get("trend_30day"),
        # Carried raw so the model can normalise it. Not blended here: KTC's
        # curve is shaped differently from FantasyCalc's and averaging the two
        # raw would inflate every bench player.
        "ktc": _ktc_for(name, ktc or {}),
        "flock": _flock_for(name, flock or {}),
    }


# Only the part of the board where a rank gap is worth real value. Below the
# curve's knee the values are flat in absolute terms, so a 40-rank gap is worth
# a rounding error and eight rankers forced into a total ordering must put
# SOMEBODY at 287. Flagging down there is noise dressed as conviction.
EXPERT_TOP_N = 150
# The 90th percentile of |gap| inside the top 150, measured across both leagues
# (18 in CL, 19 in DD). Flagging the top decile of disagreement, not a guess.
EXPERT_GAP_FLAG = 20


def attach_expert_divergence(players: list[dict], top_n: int = EXPERT_TOP_N,
                             flag_at: int = EXPERT_GAP_FLAG) -> int:
    """Compare the aggregated market against Flock's expert consensus.

    Never folded into the score, on purpose. A blended number cannot disagree
    with itself: the moment expert opinion is averaged into the price, the one
    signal this system can produce that a price cannot -- two independent reads
    diverge, so slow down and check the news -- is destroyed.

    BOTH SIDES ARE RE-RANKED INSIDE THE SHARED SET. Flock ranks against its own
    full board (438 players) while the market rank here is over the rostered
    universe (~340). Comparing those directly makes every rostered player look
    market-inflated by construction: the first version of this read
    "market higher" for all six of its top divergences, in both leagues.

    Lower rank number is better. gap = market_rank - flock_rank, so a POSITIVE
    gap means the market ranks the player worse than Flock does, which is Flock
    rating him HIGHER. Getting this backwards is easy and inverts the advice.
    """
    have = [p for p in players
            if p.get("market") and (p.get("flock") or {}).get("rank") is not None]
    if not have:
        return 0
    by_market = sorted(have, key=lambda x: -x["market"])
    by_flock = sorted(have, key=lambda x: x["flock"]["rank"])
    mrank = {id(p): i for i, p in enumerate(by_market, 1)}
    frank = {id(p): i for i, p in enumerate(by_flock, 1)}

    flagged = 0
    for p in have:
        mr, fr = mrank[id(p)], frank[id(p)]
        gap = mr - fr
        if gap > 0:
            direction = "flock_higher"
        elif gap < 0:
            direction = "market_higher"
        else:
            direction = "agree"
        flag = abs(gap) >= flag_at and mr <= top_n
        p["expert"] = {
            "market_rank": mr,
            "flock_rank": fr,
            "gap": gap,
            "direction": direction,
            "flagged": flag,
        }
        if flag:
            flagged += 1
    return flagged


def build_rosters(league_id: str, owner_user_id: str | None = None) -> dict:
    """Build the priced roster board for one league. Pure of I/O side effects
    beyond the reads it makes."""
    client = SleeperClient(league_id)

    league = client.get_league()
    settings = client.get_league_settings()
    rosters = client.get_rosters()
    users = client.get_users()
    players = client.get_players_map()

    # Draft capital. Sleeper publishes only the picks that have CHANGED HANDS;
    # the rest is implied by every team owning its own. Non-fatal: a board
    # without picks is still a board.
    #
    # Both fetches must succeed or picks are withheld entirely. Falling back to
    # empty lists looks harmless and is not: with no traded rows the board
    # asserts that NOBODY has ever traded a pick, and with no draft list it
    # cannot tell a spent draft from a future one, so the completed 2026 class
    # comes back as twelve live assets per roster. Two confident lies from one
    # dead endpoint. No picks at all is the honest answer.
    traded_picks: list | None = None
    drafts: list | None = None
    picks_error: str | None = None
    try:
        got_t = client.get_traded_picks()
        got_d = client.get_drafts()
        if not isinstance(got_t, list) or not isinstance(got_d, list):
            raise SleeperError("traded_picks or drafts returned an unexpected shape")
        traded_picks, drafts = got_t, got_d
    except Exception as e:  # noqa: BLE001
        picks_error = str(e)
        log.warning("Draft picks unavailable, withholding them: %s", e)

    superflex = int(settings.get("num_qbs", 1)) >= 2

    ktc: dict[str, dict] = {}
    ktc_error = None
    try:
        # Superflex matters: this owner's two leagues are 1QB Standard and
        # Superflex PPR, and a quarterback is worth wildly different amounts in
        # each. Asking KTC for the wrong build is the same class of error as
        # asking the wrong league.
        ktc = get_ktc_values(superflex=superflex)
    except KTCError as e:
        ktc_error = str(e)
        log.warning("KTC unavailable, board will be single-source: %s", e)

    # Fetched for THIS league's format. Flock publishes a different board per
    # format and this call used to take the superflex default in both leagues.
    #
    # Flock is expert RANK and TIER, never a value. It reaches the model as a
    # conviction signal and is asserted never to move the score: there is no
    # honest conversion from a rank to a price.
    #
    # Non-fatal by design, and currently dark: it needs FLOCK_EMAIL and
    # FLOCK_PASSWORD plus the `flock` extra (Playwright). _try_flock_rankings
    # returns {} on any failure, so the board simply reports it as absent.
    flock = _try_flock_rankings(superflex)

    values: dict[str, dict] = {}
    values_error = None
    try:
        values = get_values(settings)
    except ValuesError as e:
        # No values is a degraded board, not a dead one: the names and the
        # ownership are still true and still useful.
        values_error = str(e)
        log.warning("Values unavailable, roster board will be unpriced: %s", e)

    # Record what each source said today, before anything is blended.
    #
    # The weights in model.py are unfalsifiable right now, and not because the
    # method is hard: the only real ground truth for "was this valuation right"
    # is what the market says months later, and nothing was recording. The disk
    # cache is a rolling 30-minute overwrite, so the previous state is gone the
    # moment it refreshes. A day not recorded cannot be recovered at any price.
    #
    # RAW per source, never the composite. Recording the blend would bake
    # today's weights into the history and make it useless for testing any
    # other weighting, which is the entire point of keeping it.
    if values:
        history.record(f"market-{'sf' if superflex else '1qb'}",
                       history.shrink_market(values))
    if ktc:
        history.record(f"ktc-{'sf' if superflex else '1qb'}", history.shrink_ktc(ktc))
    if flock:
        history.record("flock", history.shrink_flock(flock))

    by_user = {str(u.get("user_id")): u for u in users}

    teams = []
    for r in rosters:
        owner_id = str(r.get("owner_id") or "")
        u = by_user.get(owner_id) or {}
        entries = [_player_record(str(pid), players, values, ktc, flock)
                   for pid in (r.get("players") or [])]

        # Highest value first, and unvalued players last rather than first.
        entries.sort(key=lambda e: (e["value"] is None, -(e["value"] or 0), e["name"]))

        priced = [e for e in entries if e["value"] is not None]
        s = r.get("settings") or {}
        teams.append({
            "roster_id": str(r.get("roster_id")),
            "owner_id": owner_id,
            "owner": u.get("display_name") or u.get("username") or "unknown owner",
            "is_owner": bool(owner_user_id) and owner_id == str(owner_user_id),
            "wins": s.get("wins", 0),
            "losses": s.get("losses", 0),
            "ties": s.get("ties", 0),
            "players": entries,
            "total_value": sum(e["value"] or 0 for e in priced),
            "priced_count": len(priced),
            "unpriced_count": len(entries) - len(priced),
        })

    # Score across the WHOLE league in one pass: replacement levels and the
    # trend spread are league-wide facts, and computing them per team would
    # give each roster its own private idea of what a startable player is.
    owner_ids = {
        e["id"] for t in teams if t["is_owner"] for e in t["players"]
    }
    scored = score_players(
        [{
            "id": e["id"], "name": e["name"], "position": e["position"],
            "age": e["age"],
            "fantasycalc": ({"value": e["value"], "trend_30day": e["trend_30day"]}
                            if e["value"] is not None else None),
            "ktc": e.get("ktc"),
            "flock": e.get("flock"),
        } for t in teams for e in t["players"]],
        num_teams=int(settings.get("num_teams", 12)),
        starters=starters_for(settings.get("num_qbs", 1)),
        # AGE IS OFF, and this is a measured decision, not a default.
        #
        # Dynasty prices already encode remaining career window: that is most
        # of what makes a dynasty value differ from a redraft one. A 30-year-old
        # RB is priced low BECAUSE he is 30. Multiplying by an age factor on top
        # charges for it twice, which is the Caleb Williams trend bug in a
        # different term.
        #
        # Measured on a reconstructed 30-day panel (market_t-30 = value minus
        # trend_30day, trend weight zeroed so the label is not also an input),
        # information coefficient of the composite's adjustment against the
        # realised 30-day return:
        #
        #                        DD 1QB     CL SF
        #     age ON            -0.0225   -0.0713
        #     age OFF           +0.0483   -0.0095
        #     age alone         -0.1023   -0.0993
        #
        # The age factor was ANTI-predictive in both leagues by two independent
        # routes. Removing it improves the composite in both.
        #
        # Honest limits: one overlapping window, every confidence interval
        # straddles zero, and 30-day moves are mostly news no valuation model
        # should predict. This does not prove age is harmful. It fails to show
        # it helps, points negative consistently, and has a mechanism. Keeping a
        # term on those terms is the worse bet. Revisit against the real
        # snapshots in trade_scout/history.py once a few months have accrued.
        # SCARCITY IS OFF TOO, and for a sharper reason than age.
        #
        # It only ever takes FOUR values, one per position. It is not adjusting
        # players, it is a flat bet that the market misprices whole positions.
        # And the market it bets against already knows the format: FantasyCalc
        # is fetched with numQbs / numTeams / ppr.
        #
        # Measured. On one player board, swapping the 1QB starters table for
        # the superflex one moves the QB multiplier +28.7%, so the table is
        # load-bearing in isolation. In production it is not: DD (1QB board,
        # 1QB starters) gives QB 0.854 and CL (SF board, SF starters) gives
        # 0.860. The superflex player board already prices QBs higher, which
        # lifts the replacement level and cancels the table. The two corrections
        # eat each other and the residual is a spurious ~15% QB discount in BOTH
        # formats, which is backwards in superflex.
        #
        # IC against realised 30-day return: +0.0156 (DD), -0.0519 (CL).
        # Inconsistent in sign, both CIs straddling zero. No evidence it helps
        # and a measured mechanism by which it hurts.
        #
        # Removing it takes the positional distortion out: QB 0.855 -> 1.000,
        # TE 1.150 -> 1.000, with RB and WR already level after the age change.
        # TREND IS OFF TOO, which makes the score the AGGREGATED MARKET and
        # nothing else. That is the product: the best consensus price the
        # sources can produce, with expert opinion kept alongside it as an
        # independent check rather than blended into it.
        #
        # Trend was the last of three adjustments that all failed the same way.
        # FantasyCalc publishes /values/current: a player who moved is quoted at
        # his POST-move price and trend_30day describes how he got there.
        # Multiplying by the recent direction charges for the same move twice.
        # It was already cut 0.25 -> 0.08 after Caleb Williams; at 0.08 it still
        # moved Josh Allen +8.4% off the blended market for no reason the market
        # had not already priced.
        #
        # Age, scarcity and trend each looked individually reasonable and each
        # measured as re-charging for something already in the price. The market
        # is set by thousands of real trades and is harder to beat than it looks.
        #
        # What remains: fit, which is the ONE adjustment with no double-count
        # argument, because the market cannot know this roster is sixteen deep
        # at WR. It fires only on owned players.
        weights=Weights(age=0.0, scarcity=0.0, trend=0.0),
        owner_roster_ids=owner_ids,
    )
    by_id = {r["id"]: r for r in scored["players"]}
    # Expert cross-check, computed after scoring so it sees the blended market.
    expert_flagged = attach_expert_divergence(list(by_id.values()))

    for t in teams:
        for e in t["players"]:
            r = by_id.get(e["id"]) or {}
            e["score"] = r.get("score")
            e["score_trend_averaged"] = r.get("score_trend_averaged")
            e["trend_mode_delta"] = r.get("trend_mode_delta")
            e["market"] = r.get("market")
            e["vorp"] = r.get("vorp")
            e["factors"] = r.get("factors") or {}
            src = r.get("sources") or {}
            e["sources"] = {
                "fantasycalc": src.get("fantasycalc"),
                "ktc": src.get("ktc"),
                "disagreement_pct": src.get("disagreement_pct"),
                "names": src.get("sources") or [],
            }
            # KTC's raw record has done its job; drop it so the payload stays
            # small and there is one obvious place the numbers come from.
            e.pop("ktc", None)
            e["flock"] = r.get("flock")
            # The expert cross-check. Copied explicitly like everything else
            # here: this merge whitelists fields, so anything attached to the
            # scored record and not named here is silently dropped.
            e["expert"] = r.get("expert")
        # Highest SCORE first, unscored last. The score is the ranking the
        # owner asked for; total_value stays the market total beneath it.
        t["players"].sort(
            key=lambda e: (e["score"] is None, -(e["score"] or 0), e["name"]))
        t["total_score"] = round(
            sum(e["score"] or 0 for e in t["players"]), 1)

    # --- draft capital -----------------------------------------------------
    # Priced AFTER scoring, because the projected tier of a future pick depends
    # on how strong the ORIGINATING roster is, and that is what the score
    # measures. A weak roster picks early, which makes its own future first
    # worth more, and that is the thing dynasty owners most often get backwards
    # about their own rebuild.
    teams.sort(key=lambda t: -t["total_score"])
    strength_rank = {t["roster_id"]: i + 1 for i, t in enumerate(teams)}

    pick_market: dict = {}
    try:
        from trade_scout.values import get_pick_values
        pick_market = get_pick_values(settings)
    except Exception as e:  # noqa: BLE001
        log.warning("Pick market unavailable (non-fatal): %s", e)

    try:
        season = int(league.get("season") or 0)
    except (TypeError, ValueError):
        season = 0
    if not season:
        picks_error = picks_error or "the league reported no season"
    rounds = int((league.get("settings") or {}).get("draft_rounds") or 4)
    owned = ({} if (picks_error or traded_picks is None or drafts is None)
             else build_ownership(traded_picks, drafts,
                                  [t["roster_id"] for t in teams], season, rounds))

    by_rid = {t["roster_id"]: t for t in teams}
    for rid, picks in (owned or {}).items():
        team = by_rid.get(rid)
        if team is None:
            continue
        rows = []
        for pk in picks:
            ref = PickRef(pk["season"], pk["round"], tier="mid")
            entry, basis = lookup(pick_market, ref) if pick_market else (None, "no market")
            tier = project_tier(strength_rank.get(pk["from_roster_id"], 99), len(teams))
            proj, proj_basis = (
                lookup(pick_market, PickRef(pk["season"], pk["round"], tier=tier))
                if pick_market else (None, "no market"))
            origin = by_rid.get(pk["from_roster_id"])
            rows.append({
                **pk,
                "label": PickRef(pk["season"], pk["round"]).label(),
                "from_owner": origin["owner"] if origin else "unknown",
                "value": entry["value"] if entry else None,
                "basis": basis,
                # A PROJECTION, and named as one. The market value above assumes
                # a mid pick; this assumes the originating roster finishes where
                # its current strength suggests.
                "projected_tier": tier,
                "projected_value": proj["value"] if proj else None,
                "projected_basis": proj_basis,
            })
        team["picks"] = rows
        team["pick_value"] = round(sum(r["value"] or 0 for r in rows), 1)
        team["pick_value_projected"] = round(
            sum(r["projected_value"] or 0 for r in rows), 1)

    for t in teams:
        t.setdefault("picks", [])
        t.setdefault("pick_value", 0)
        t.setdefault("pick_value_projected", 0)

    return {
        "league_id": str(league_id),
        "league_name": league.get("name"),
        "season": league.get("season"),
        "format": settings.get("format_str"),
        "teams": teams,
        "values_source": "FantasyCalc" if values else None,
        "values_error": values_error,
        "ktc_source": "KeepTradeCut" if ktc else None,
        "ktc_error": ktc_error,
        "flock_source": "Flock Fantasy" if flock else None,
        "flock_matched": sum(
            1 for t in teams for e in t["players"] if e.get("flock")),
        "ktc_matched": sum(
            1 for t in teams for e in t["players"]
            if (e.get("sources") or {}).get("ktc") is not None),
        "superflex": superflex,
        "pick_market_size": len(pick_market),
        "pick_count": sum(len(t["picks"]) for t in teams),
        # How many players the market and the experts materially disagree on.
        "expert_flagged": expert_flagged,
        # Not None means the board is deliberately showing NO picks rather than
        # showing default ownership as though it were fact.
        "picks_error": picks_error,
        "model": scored["meta"],
        "player_count": sum(len(t["players"]) for t in teams),
    }


def run_rosters() -> None:
    """Entry point. Reads SLEEPER_LEAGUE_ID and optional SLEEPER_OWNER_ID."""
    logging.basicConfig(level=logging.INFO, format="%(name)s %(levelname)s: %(message)s")

    league_id = os.environ.get("SLEEPER_LEAGUE_ID", "").strip()
    owner_user_id = os.environ.get("SLEEPER_OWNER_ID", "").strip() or None

    if not league_id:
        print("Missing SLEEPER_LEAGUE_ID", file=sys.stderr)
        sys.exit(2)

    try:
        payload = build_rosters(league_id, owner_user_id)
    except SleeperError as e:
        print(f"Sleeper unavailable: {e}", file=sys.stderr)
        sys.exit(3)

    # stdout carries the sentinel then the JSON. Logs go to stderr, so a noisy
    # provider can never corrupt the payload.
    print(SENTINEL)
    print(json.dumps(payload, separators=(",", ":")))
