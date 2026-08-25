"""Per-source trade evaluation with a consensus gate.

The invariant generalised: instead of one verdict on one blended number,
every enabled value source prices the whole trade INDEPENDENTLY, ON ITS OWN
RAW SCALE, and gets one row in the output. A composite (normalised, weighted)
number exists for sorting and prose, but the per-source disagreement is the
signal and stays visible. Flock remains an opinion column: shown, never
voting.

Scale rule, worth stating because it is the easy bug: a per-source row mixes
values from ONE source only. Normalised values appear ONLY in the composite.
A row that summed a raw KTC number with a raw FantasyCalc number would be
adding two currencies.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone

from trade_scout.config import AppConfig
from trade_scout.model import blend_market_n, quantile_normalise, reference_curve
from trade_scout.name_matcher import NameMatcher
from trade_scout.picks import build_pick_market, lookup, parse_pick
from trade_scout.providers import REGISTRY_BY_KEY
from trade_scout.providers.base import SourceSnapshot
from trade_scout.trade import (
    TradeAsset,
    TradeCommand,
    _build_asset_dict,
    _try_flock_rankings,
    build_roster_context,
    resolve_assets,
)
from trade_scout.values import ValuesError, get_snapshots

log = logging.getLogger(__name__)

# Inside this band a source calls the trade even instead of picking a winner:
# a 0.2% edge is noise, and letting noise cast consensus votes makes the gate
# flip on refetches. Overridable via config consensus.even_band_pct.
DEFAULT_EVEN_BAND_PCT = 3.0


def _price_asset_for_source(asset: TradeAsset, snap: SourceSnapshot) -> float | None:
    """One asset's value under one source, on that source's raw scale.

    None means this source cannot price it (that is reported, not hidden).
    Manual overrides win everywhere: the user pinned the number.
    """
    if asset.value is not None and asset.source == "manual override":
        return float(asset.value)
    if asset.is_pick:
        ref = parse_pick(asset.raw_name)
        if ref is None or not snap.picks:
            return None
        market = {k: {"value": v} for k, v in snap.picks.items()}
        entry, _basis = lookup(market, ref)
        return float(entry["value"]) if entry else None
    if asset.player_id is None:
        return None
    record = snap.players.get(asset.player_id)
    return float(record["value"]) if record and record.get("value") is not None else None


def _side_totals(assets: list[TradeAsset], snap: SourceSnapshot,
                 ) -> tuple[float, list[str]]:
    total = 0.0
    unpriced: list[str] = []
    for a in assets:
        v = _price_asset_for_source(a, snap)
        if v is None:
            unpriced.append(a.raw_name)
        else:
            total += v
    return total, unpriced


def _verdict(give_total: float, get_total: float, band_pct: float) -> dict:
    delta = get_total - give_total
    max_side = max(give_total, get_total, 1.0)
    pct = abs(delta) / max_side * 100
    if pct < band_pct:
        winner = "EVEN"
    else:
        winner = "GET" if delta > 0 else "GIVE"
    return {
        "give_total": round(give_total, 1),
        "get_total": round(get_total, 1),
        "delta": round(delta, 1),
        "pct_gap": f"{pct:.1f}%",
        "winner": winner,
    }


def _consensus(source_rows: dict[str, dict], consensus_cfg: dict) -> dict:
    gate = str(consensus_cfg.get("gate", "majority")).lower()
    exclude_stale = bool(consensus_cfg.get("exclude_stale", True))
    voting = {k: r for k, r in source_rows.items()
              if not (exclude_stale and r.get("stale"))}
    wins = sum(1 for r in voting.values() if r["winner"] == "GET")
    losses = sum(1 for r in voting.values() if r["winner"] == "GIVE")
    even = sum(1 for r in voting.values() if r["winner"] == "EVEN")
    eligible = len(voting)
    if eligible == 0:
        passes = False
    elif gate == "all":
        passes = wins == eligible
    elif gate == "any":
        passes = wins >= 1
    else:  # majority
        passes = wins > eligible / 2
    return {
        "wins": wins, "losses": losses, "even": even, "eligible": eligible,
        "gate": gate, "passes": passes,
        "summary": f"wins under {wins} of {eligible} source"
                   f"{'s' if eligible != 1 else ''}",
    }


def _flock_opinion(assets: list[TradeAsset], flock_data: dict,
                   players_map: dict) -> None:
    """Attach Flock rank/tier to assets, by name. Mutates in place."""
    for asset in assets:
        if asset.is_pick:
            continue
        key = asset.raw_name.lower()
        entry = flock_data.get(key)
        if entry is None and asset.player_id:
            full = (players_map.get(asset.player_id, {}) or {}).get("full_name", "")
            if full:
                entry = flock_data.get(full.lower())
        if entry is None:
            for flock_name in flock_data:
                if key in flock_name or flock_name in key:
                    entry = flock_data[flock_name]
                    break
        if entry:
            asset.flock_rank = entry.get("flock_rank")
            asset.flock_tier = entry.get("flock_tier")
            asset.flock_pos_rank = entry.get("flock_pos_rank")


def evaluate_trade(
    trade: TradeCommand,
    league: dict,
    players_map: dict,
    sleeper,
    cfg: AppConfig | None = None,
    display_name: str = "",
    *,
    snapshots: dict | None = None,
    source_errors: dict | None = None,
    matcher: NameMatcher | None = None,
    flock_data: dict | None = None,
    include_roster_context: bool = True,
) -> dict:
    """Price one trade under every enabled source. The multi-source verdict.

    Raises ValuesError only when NO source could be fetched at all; a partial
    source set is a normal answer with its gaps stated.

    The keyword-only parameters let a caller that prices MANY candidate
    trades (consult) fetch snapshots and Flock once and reuse them; when they
    are None this function fetches its own.
    """
    cfg = cfg or AppConfig.load()
    matcher = matcher or NameMatcher(players_map)

    if snapshots is None:
        import concurrent.futures

        superflex = int(league.get("num_qbs", 1)) >= 2
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            snaps_future = pool.submit(get_snapshots, league, cfg, matcher)
            flock_future = pool.submit(_try_flock_rankings, superflex)
            snapshots, errors = snaps_future.result()
            flock_data = flock_future.result()
    else:
        errors = dict(source_errors or {})
        flock_data = flock_data or {}

    if not snapshots:
        raise ValuesError(
            "No value source could be fetched: "
            + "; ".join(f"{k}: {v}" for k, v in errors.items()))

    # Composite pick market for asset resolution: the reference source's picks
    # calibrated against KTC's, exactly as the single-source path priced them.
    fc_picks = snapshots["fantasycalc"].picks if "fantasycalc" in snapshots else {}
    kt_picks = snapshots["ktc"].picks if "ktc" in snapshots else {}
    pick_market = build_pick_market(fc_picks, kt_picks)

    give_assets = resolve_assets(trade.give, players_map, matcher,
                                 trade.overrides, pick_market)
    get_assets = resolve_assets(trade.get, players_map, matcher,
                                trade.overrides, pick_market)
    all_assets = give_assets + get_assets

    flock_available = bool(flock_data)
    if flock_data:
        _flock_opinion(all_assets, flock_data, players_map)

    band = float(cfg.consensus.get("even_band_pct", DEFAULT_EVEN_BAND_PCT))

    # --- one row per source, each on its own raw scale --------------------
    source_rows: dict[str, dict] = {}
    for key, snap in snapshots.items():
        give_total, give_unpriced = _side_totals(give_assets, snap)
        get_total, get_unpriced = _side_totals(get_assets, snap)
        row = _verdict(give_total, get_total, band)
        row.update({
            "display_name": snap.display_name,
            "as_of": snap.as_of,
            "stale": snap.stale,
            "unmatched": give_unpriced + get_unpriced,
        })
        source_rows[key] = row

    consensus = _consensus(source_rows, cfg.consensus)

    # --- composite: everything normalised onto the reference curve --------
    ordered = list(snapshots.values())
    ref_snap = ordered[0]
    ref = reference_curve([p.get("value") for p in ref_snap.players.values()])
    norm_maps: dict[str, dict] = {}
    for key, snap in snapshots.items():
        raw = {pid: rec.get("value") for pid, rec in snap.players.items()
               if rec.get("value") is not None}
        norm_maps[key] = raw if snap is ref_snap else quantile_normalise(raw, ref)

    weights = {key: float((cfg.sources.get(key) or {}).get("weight", 1.0))
               for key in snapshots}

    def composite_value(asset: TradeAsset) -> tuple[float | None, dict]:
        if asset.value is not None and asset.source == "manual override":
            return float(asset.value), {"per_source": {}, "sources": ["override"],
                                        "disagreement_pct": None}
        if asset.is_pick:
            # Picks were priced from the composite market at resolution.
            detail = {"per_source": {}, "sources": ["pick market"],
                      "disagreement_pct": None}
            return (float(asset.value), detail) if asset.value is not None \
                else (None, detail)
        if asset.player_id is None:
            return None, {"per_source": {}, "sources": [], "disagreement_pct": None}
        vals = {key: norm_maps[key].get(asset.player_id) for key in snapshots}
        return blend_market_n(vals, weights)

    unmatched: list[str] = []
    give_comp = get_comp = 0.0
    asset_details: dict[int, dict] = {}
    for side_assets, is_give in ((give_assets, True), (get_assets, False)):
        for a in side_assets:
            val, detail = composite_value(a)
            asset_details[id(a)] = detail
            if val is None:
                unmatched.append(a.raw_name)
                continue
            if is_give:
                give_comp += val
            else:
                get_comp += val
    composite = _verdict(give_comp, get_comp, band)
    composite["weights"] = weights

    # --- per-asset breakdown ----------------------------------------------
    def asset_row(a: TradeAsset) -> dict:
        base = _build_asset_dict(a, players_map)
        base["per_source"] = {
            key: _price_asset_for_source(a, snap)
            for key, snap in snapshots.items()
        }
        base["composite_detail"] = asset_details.get(id(a))
        return base

    # Roster context runs off the reference source's own records: the same
    # {id: {value: ...}} shape the single-source path fed it.
    roster_context = None
    if include_roster_context:
        try:
            roster_context = build_roster_context(
                sleeper, display_name, players_map, ref_snap.players,
                give_assets, get_assets)
        except Exception as e:  # noqa: BLE001 - context is decoration, not verdict
            log.warning("Roster context failed (non-fatal): %s", e)

    source_names = ", ".join(r["display_name"] for r in source_rows.values())
    return {
        "give_side": [asset_row(a) for a in give_assets],
        "get_side": [asset_row(a) for a in get_assets],
        "sources": source_rows,
        "source_errors": errors,
        "composite": composite,
        "consensus": consensus,
        "flock_available": flock_available,
        "unmatched": unmatched,
        "format_str": league.get("format_str", "SF / 12-team / PPR"),
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        "roster_context": roster_context,
        # Legacy top-level keys: the composite, so the prose verdict and any
        # older consumer keep working against the blended number.
        "give_total": composite["give_total"],
        "get_total": composite["get_total"],
        "delta": composite["delta"],
        "pct_gap": composite["pct_gap"],
        "winner": composite["winner"],
        "source": source_names or "no sources",
    }


def format_source_table(result: dict) -> str:
    """The per-source verdict rows as fixed-width text, one line per source."""
    lines = []
    for row in result["sources"].values():
        verdict = {"GET": "WIN ", "GIVE": "LOSS", "EVEN": "EVEN"}[row["winner"]]
        stale = ", STALE" if row.get("stale") else ""
        missing = (f"  [couldn't price: {', '.join(row['unmatched'])}]"
                   if row.get("unmatched") else "")
        lines.append(
            f"{row['display_name']:<15} give {row['give_total']:>8,.0f} | "
            f"get {row['get_total']:>8,.0f} | {row['delta']:>+8,.0f} "
            f"({row['pct_gap']:>5}) {verdict}  as of {row['as_of']}{stale}{missing}")
    for key, err in (result.get("source_errors") or {}).items():
        display = getattr(REGISTRY_BY_KEY.get(key), "display_name", key)
        lines.append(f"{display:<15} unavailable: {err}")
    c = result["consensus"]
    lines.append(
        f"Consensus: {c['summary']} (gate: {c['gate']} -> "
        f"{'PASS' if c['passes'] else 'FAIL'})")
    return "\n".join(lines)
