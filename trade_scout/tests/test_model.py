"""Tests for the proprietary score.

The model is the one place that decides what a player is worth to the owner, so
every claim it makes has to be falsifiable here. The checks that matter most
are the ones about what the model REFUSES to do: invent a price for an unpriced
player, average a rank into a value, or let one source's curve shape dominate.
"""

from trade_scout.model import (
    SUPERFLEX_STARTERS,
    TOTAL_HIGH,
    TOTAL_LOW,
    Weights,
    age_factor,
    blend_market,
    clamp,
    fit_factor,
    quantile_normalise,
    reference_curve,
    replacement_levels,
    scarcity_multiplier,
    score_players,
    starters_for,
    trend_z,
    vorp,
)


def mk(i, pos, age, fc=None, ktc=None, trend=None, flock=None):
    return {
        "id": str(i), "name": f"P{i}", "position": pos, "age": age,
        "fantasycalc": None if fc is None else {"value": fc, "trend_30day": trend},
        "ktc": None if ktc is None else {"value": ktc},
        "flock": flock,
    }


class TestQuantileNormalise:
    def test_maps_a_source_onto_the_reference_curve(self):
        ref = [1000, 500, 100]
        out = quantile_normalise({"a": 9999, "b": 5000, "c": 1}, ref)
        assert out == {"a": 1000, "b": 500, "c": 100}

    def test_ordering_is_what_survives_not_magnitude(self):
        # Two sources with wildly different scales must land identically once
        # they agree on the ORDER. This is the whole point.
        ref = [1000, 500, 100]
        a = quantile_normalise({"x": 11000, "y": 3000, "z": 5}, ref)
        b = quantile_normalise({"x": 9999, "y": 2559, "z": 159}, ref)
        assert a == b

    def test_empty_reference_returns_input_unchanged(self):
        assert quantile_normalise({"a": 5}, []) == {"a": 5}

    def test_single_player_takes_the_top_of_the_curve(self):
        assert quantile_normalise({"a": 42}, [900, 400]) == {"a": 900}

    def test_none_values_are_dropped_not_ranked_as_zero(self):
        out = quantile_normalise({"a": 100, "b": None}, [10, 5])
        assert "b" not in out


class TestAgeCurve:
    def test_rb_decays_faster_than_wr_at_the_same_age(self):
        assert age_factor("RB", 29) < age_factor("WR", 29)

    def test_qb_holds_longest(self):
        assert age_factor("QB", 32) > age_factor("WR", 32) > age_factor("RB", 32)

    def test_inside_the_peak_window_is_neutral(self):
        assert age_factor("WR", 25) == 1.0

    def test_unknown_age_is_neutral_not_penalised(self):
        assert age_factor("RB", None) == 1.0

    def test_unknown_position_uses_the_default_curve(self):
        assert 0 < age_factor("KICKER", 30) < 1.0

    def test_youth_bias_lifts_the_young_and_cuts_the_old(self):
        assert age_factor("RB", 21, youth_bias=0.2) > age_factor("RB", 21)
        # 27, not 30: a 30-year-old RB is already pinned to the clamp floor, so
        # the bias has nothing left to take and the assertion would pass or
        # fail on the clamp rather than on the bias.
        assert age_factor("RB", 27, youth_bias=0.2) < age_factor("RB", 27)

    def test_the_clamp_floor_dominates_at_the_extremes(self):
        assert age_factor("RB", 30) == age_factor("RB", 30, youth_bias=0.2) == 0.55

    def test_never_escapes_the_clamp(self):
        assert age_factor("RB", 45) >= 0.55
        assert age_factor("QB", 18, youth_bias=5.0) <= 1.45


class TestScarcity:
    def test_a_cheap_replacement_position_is_scarcer(self):
        # TE replacement cheap -> elite TE matters more -> multiplier above 1.
        m = scarcity_multiplier({"TE": 1000.0, "WR": 3000.0})
        assert m["TE"] > 1.0 > m["WR"]

    def test_identical_replacements_are_all_neutral(self):
        m = scarcity_multiplier({"RB": 2000.0, "WR": 2000.0})
        assert m == {"RB": 1.0, "WR": 1.0}

    def test_no_data_is_neutral_not_a_crash(self):
        assert scarcity_multiplier({"RB": 0.0}) == {"RB": 1.0}

    def test_replacement_level_is_the_last_startable(self):
        vals = [float(x) for x in range(100, 0, -1)]  # 100 down to 1
        # 2 starters x 5 teams = the 10th best.
        r = replacement_levels({"RB": vals}, num_teams=5, starters={"RB": 2.0})
        assert r["RB"] == 91.0

    def test_a_thin_position_falls_back_to_its_worst_player(self):
        r = replacement_levels({"TE": [50.0, 40.0]}, num_teams=12, starters={"TE": 1.0})
        assert r["TE"] == 40.0


class TestSuperflex:
    """The owner's two leagues are 1QB Standard and Superflex PPR. A quarterback
    is worth wildly different amounts in each, and the replacement level is how
    the model is supposed to know that."""

    def test_superflex_demands_roughly_two_quarterbacks(self):
        assert starters_for(2)["QB"] > 1.5
        assert starters_for(1)["QB"] == 1.0

    def test_superflex_pulls_the_qb_replacement_deeper(self):
        # 12 teams x 1.9 starters is the ~23rd QB, versus the 12th in 1QB.
        qbs = [float(x) for x in range(3000, 0, -100)]  # 30 quarterbacks
        one = replacement_levels({"QB": qbs}, 12, starters_for(1))
        sf = replacement_levels({"QB": qbs}, 12, starters_for(2))
        assert sf["QB"] < one["QB"]

    def test_the_two_builds_are_not_the_same_object(self):
        # starters_for returns a copy; a caller mutating it must not rewrite
        # the module-level table for every later league.
        a = starters_for(2)
        a["QB"] = 99.0
        assert SUPERFLEX_STARTERS["QB"] != 99.0

    def test_unknown_or_missing_qb_count_is_treated_as_1qb(self):
        assert starters_for(None)["QB"] == 1.0
        assert starters_for(0)["QB"] == 1.0


class TestFitAndVorp:
    def test_no_surplus_is_neutral(self):
        assert fit_factor(2, 2.6) == 1.0

    def test_each_surplus_body_discounts_further(self):
        assert fit_factor(5, 2.0) < fit_factor(4, 2.0) < fit_factor(3, 2.0)

    def test_fit_never_escapes_the_clamp(self):
        assert fit_factor(50, 1.0) >= 0.55

    def test_vorp_floors_at_zero(self):
        # Nobody pays you to take a player; they decline.
        assert vorp(500.0, 900.0) == 0.0

    def test_vorp_is_the_surplus(self):
        assert vorp(900.0, 500.0) == 400.0

    def test_trend_z_handles_no_spread(self):
        assert trend_z(500, 0) == 0.0
        assert trend_z(None, 100) == 0.0


class TestCompoundingIsBounded:
    """Clamping each factor bounds nothing.

    Four factors at 0.8 multiply to 0.41, under any individual floor. On the
    live board the product hit 0.394 and Jonathan Taylor came out 57% below his
    market price from adjustments that each looked reasonable alone.
    """

    def test_the_total_adjustment_has_its_own_floor(self):
        # An old RB on a deep roster at a rich position: every factor pushes
        # down at once, which is exactly the case per-factor clamps miss.
        roster = [mk(i, "RB", 30, fc=5000 - i * 100, ktc=5000 - i * 100)
                  for i in range(1, 12)]
        res = score_players(roster, owner_roster_ids={str(i) for i in range(1, 12)})
        for r in res["players"]:
            assert r["factors"]["total"] >= TOTAL_LOW

    def test_the_total_adjustment_has_a_ceiling(self):
        roster = [mk(1, "TE", 25, fc=9000, ktc=9000), mk(2, "WR", 25, fc=100, ktc=100)]
        res = score_players(roster)
        for r in res["players"]:
            assert r["factors"]["total"] <= TOTAL_HIGH

    def test_the_total_is_reported_so_the_score_can_be_taken_apart(self):
        res = score_players([mk(1, "RB", 24, fc=5000, ktc=5000)])
        f = res["players"][0]["factors"]
        assert "total" in f and "total_clamped" in f

    def test_the_reported_total_is_the_actual_product(self):
        # Reporting a constant would satisfy the bounds checks above while
        # telling the reader nothing. The total must be the product it claims
        # to be, or it is decoration on a number nobody can audit.
        roster = [mk(1, "RB", 29, fc=5000, ktc=5000),
                  mk(2, "TE", 24, fc=4000, ktc=4000),
                  mk(3, "WR", 31, fc=3000, ktc=3000)]
        for r in score_players(roster)["players"]:
            f = r["factors"]
            if f["total_clamped"]:
                continue
            expected = round(f["scarcity"] * f["age"] * f["fit"], 3)
            assert abs(f["total"] - expected) < 0.01, r["name"]

    def test_a_downward_adjustment_shows_a_total_below_one(self):
        # A 29-year-old back is discounted, so his total must be under 1.
        r = score_players([mk(1, "RB", 29, fc=5000, ktc=5000),
                           mk(2, "RB", 24, fc=4000, ktc=4000)])["players"]
        old = [x for x in r if x["id"] == "1"][0]
        assert old["factors"]["total"] < 1.0

    def test_a_pinned_total_says_so(self):
        roster = [mk(i, "RB", 32, fc=5000 - i * 100, ktc=5000 - i * 100)
                  for i in range(1, 12)]
        res = score_players(roster, owner_roster_ids={str(i) for i in range(1, 12)})
        pinned = [r for r in res["players"] if r["factors"]["total_clamped"]]
        assert pinned, "nothing hit the bound, so the flag is untested here"


class TestTrendDoesNotRepriceTheMarket:
    """The market price ALREADY contains the trend.

    FantasyCalc publishes current values: a player who fell 255 over 30 days is
    quoted at his post-fall price. Multiplying that price by the recent
    direction charges him twice. At trend_strength 0.25 it took 37% off a
    player both sources agreed on within 3.5%.
    """

    def _movers(self):
        # One big faller among steady players, so the spread is real.
        return [mk(1, "WR", 25, fc=7000, ktc=7000, trend=-255),
                mk(2, "WR", 25, fc=6000, ktc=6000, trend=10),
                mk(3, "WR", 25, fc=5000, ktc=5000, trend=-5),
                mk(4, "WR", 25, fc=4000, ktc=4000, trend=20)]

    def test_the_default_tilt_is_small(self):
        assert Weights().trend_strength <= 0.10
        assert Weights().trend_share <= 0.10

    def test_a_big_faller_is_tilted_not_repriced(self):
        # This fixture is deliberately extreme: one huge faller among steady
        # players puts him past two standard deviations, harder than anything
        # on the real board (where the worst was 1.47). Even there the tilt
        # stays under a fifth, against the 37% the old default took.
        res = score_players(self._movers())
        faller = [r for r in res["players"] if r["id"] == "1"][0]
        assert faller["factors"]["trend_factored"] >= 0.78

    def test_a_one_sigma_move_is_about_the_stated_strength(self):
        # The knob should mean what it says: ~8% for a one-standard-deviation
        # move, not "8% of something else".
        res = score_players([mk(1, "WR", 25, fc=7000, ktc=7000, trend=100),
                             mk(2, "WR", 25, fc=6000, ktc=6000, trend=-100)])
        f = [r for r in res["players"] if r["id"] == "1"][0]["factors"]
        assert 1.03 <= f["trend_factored"] <= 1.09

    def test_the_tilt_still_points_the_right_way(self):
        res = score_players(self._movers())
        by = {r["id"]: r for r in res["players"]}
        assert by["1"]["factors"]["trend_factored"] < 1.0
        assert by["4"]["factors"]["trend_factored"] > 1.0

    def test_turning_the_trend_off_removes_it_entirely(self):
        res = score_players(self._movers(), weights=Weights(trend=0.0))
        assert all(r["factors"]["trend_factored"] == 1.0 for r in res["players"])


class TestBlend:
    def test_names_the_sources_it_used(self):
        v, d = blend_market(100, 200, 1.0, 1.0, Weights())
        assert v == 150
        assert d["sources"] == ["FantasyCalc", "KeepTradeCut"]

    def test_a_single_source_player_is_visibly_single_source(self):
        v, d = blend_market(100, None, 1.0, 1.0, Weights())
        assert v == 100
        assert d["sources"] == ["FantasyCalc"]
        assert d["disagreement_pct"] is None

    def test_no_source_returns_none_never_zero(self):
        v, d = blend_market(None, None, 1.0, 1.0, Weights())
        assert v is None
        assert d["sources"] == []

    def test_weights_shift_the_blend(self):
        v, _ = blend_market(100, 200, 1.0, 1.0, Weights(fantasycalc=3.0, ktc=1.0))
        assert v == 125

    def test_a_zero_weight_removes_the_source_entirely(self):
        v, d = blend_market(100, 200, 1.0, 1.0, Weights(ktc=0.0))
        assert v == 100
        assert "KeepTradeCut" not in d["sources"]

    def test_disagreement_is_reported_as_a_percentage(self):
        _, d = blend_market(100, 200, 1.0, 1.0, Weights())
        assert d["disagreement_pct"] == round(100 / 150 * 100, 1)


class TestScorePlayers:
    def setup_method(self):
        self.roster = [
            mk(1, "RB", 22, fc=9000, ktc=8800, trend=200),
            mk(2, "WR", 25, fc=6000, ktc=6200, trend=-150),
            mk(3, "TE", 27, fc=3000, ktc=3100, trend=0),
            mk(4, "RB", 30, fc=2000, ktc=2100, trend=0),
            mk(5, "WR", 24, fc=1000, ktc=1100, trend=0),
            mk(6, "QB", 26, fc=None, ktc=None),          # unpriced everywhere
        ]

    def test_an_unpriced_player_scores_none_never_zero(self):
        res = score_players(self.roster)
        p = [x for x in res["players"] if x["id"] == "6"][0]
        assert p["unpriced"] is True
        assert p["score"] is None and p["market"] is None

    def test_unpriced_players_sort_last(self):
        res = score_players(self.roster)
        assert res["players"][-1]["id"] == "6"

    def test_every_priced_player_gets_a_score(self):
        res = score_players(self.roster)
        assert all(p["score"] is not None for p in res["players"] if not p["unpriced"])

    def test_both_trend_modes_are_always_reported(self):
        res = score_players(self.roster)
        for p in res["players"]:
            if p["unpriced"]:
                continue
            assert p["score"] is not None
            assert p["score_trend_averaged"] is not None
            assert p["trend_mode_delta"] == round(
                p["score"] - p["score_trend_averaged"], 1)

    def test_the_two_modes_differ_when_a_trend_exists(self):
        # At the DEFAULT tilt the two modes nearly converge, which is itself
        # the point: once the trend stops repricing, how you apply it barely
        # matters. Turned up, they are visibly different theories.
        res = score_players(self.roster,
                            weights=Weights(trend_strength=0.30, trend_share=0.30))
        mover = [p for p in res["players"] if p["id"] == "1"][0]
        assert mover["trend_mode_delta"] != 0

    def test_the_two_modes_agree_when_there_is_no_trend(self):
        flat = [mk(1, "RB", 24, fc=5000, ktc=5000, trend=0),
                mk(2, "WR", 24, fc=3000, ktc=3000, trend=0)]
        res = score_players(flat)
        assert all(p["trend_mode_delta"] == 0 for p in res["players"])

    def test_turning_trend_off_collapses_the_two_modes(self):
        res = score_players(self.roster, weights=Weights(trend=0.0))
        assert all(p["trend_mode_delta"] == 0
                   for p in res["players"] if not p["unpriced"])

    def test_fit_only_penalises_players_you_actually_own(self):
        owned = score_players(self.roster, owner_roster_ids={"1", "4"})
        not_owned = score_players(self.roster, owner_roster_ids=set())
        a = [p for p in owned["players"] if p["id"] == "5"][0]
        b = [p for p in not_owned["players"] if p["id"] == "5"][0]
        assert a["factors"]["fit"] == 1.0 and b["factors"]["fit"] == 1.0

    def test_every_factor_is_reported_separately(self):
        res = score_players(self.roster)
        p = [x for x in res["players"] if x["id"] == "1"][0]
        assert set(p["factors"]) == {
            "age", "fit", "scarcity", "trend_factored", "total", "total_clamped"}

    def test_the_meta_states_its_basis(self):
        res = score_players(self.roster)
        m = res["meta"]
        assert "FantasyCalc" in m["reference_source"]
        assert m["replacement_levels"] and m["weights"]

    def test_zero_weights_neutralise_their_factor(self):
        res = score_players(self.roster, weights=Weights(age=0.0, scarcity=0.0, fit=0.0))
        for p in res["players"]:
            if p["unpriced"]:
                continue
            assert p["factors"]["age"] == 1.0
            assert p["factors"]["scarcity"] == 1.0
            assert p["factors"]["fit"] == 1.0

    def test_differently_shaped_curves_do_not_inflate_the_bottom(self):
        # THE case quantile normalisation exists for, and the one the rest of
        # these fixtures miss because their two sources happen to agree
        # numerically. Live, FantasyCalc decays steeply while KTC compresses:
        # at the median KTC says 2559 where FantasyCalc says 1093.
        #
        # Here both sources agree on ORDER and disagree wildly on SHAPE. After
        # mapping, both land on the reference curve, so the blend IS the
        # reference curve. Blend them raw instead and the bottom player comes
        # out at (100+4000)/2 = 2050 rather than 100: every bench player on
        # every roster silently inflated twentyfold.
        rows = [
            mk(1, "WR", 25, fc=9000, ktc=9900),
            mk(2, "WR", 25, fc=1000, ktc=5000),
            mk(3, "WR", 25, fc=100, ktc=4000),
        ]
        by = {p["id"]: p for p in score_players(rows)["players"]}
        assert by["1"]["market"] == 9000
        assert by["2"]["market"] == 1000
        assert by["3"]["market"] == 100

    def test_agreeing_on_order_means_zero_disagreement(self):
        # Corollary: once shape is normalised away, "disagreement" can only
        # mean the sources RANK the player differently. Same order, no gap.
        rows = [
            mk(1, "WR", 25, fc=9000, ktc=9900),
            mk(2, "WR", 25, fc=1000, ktc=5000),
            mk(3, "WR", 25, fc=100, ktc=4000),
        ]
        for p in score_players(rows)["players"]:
            assert p["sources"]["disagreement_pct"] == 0.0

    def test_disagreeing_on_order_shows_up_as_disagreement(self):
        rows = [
            mk(1, "WR", 25, fc=9000, ktc=100),   # sources flatly disagree
            mk(2, "WR", 25, fc=100, ktc=9000),
        ]
        gaps = [p["sources"]["disagreement_pct"] for p in score_players(rows)["players"]]
        assert all(g > 0 for g in gaps)

    def test_an_empty_roster_does_not_crash(self):
        res = score_players([])
        assert res["players"] == []

    def test_a_single_source_league_still_scores(self):
        only_fc = [mk(1, "RB", 24, fc=5000), mk(2, "WR", 24, fc=3000)]
        res = score_players(only_fc)
        assert all(p["score"] is not None for p in res["players"])
        assert all(p["sources"]["ktc"] is None for p in res["players"])

    def test_ktc_only_still_scores_using_its_own_curve(self):
        only_ktc = [mk(1, "RB", 24, ktc=5000), mk(2, "WR", 24, ktc=3000)]
        res = score_players(only_ktc)
        assert all(p["score"] is not None for p in res["players"])

    def test_flock_is_carried_but_never_priced_in(self):
        # A rank is not a value. Flock must reach the output untouched and must
        # not move the score, or the model has invented a number.
        with_flock = [mk(1, "RB", 24, fc=5000, ktc=5000, flock={"rank": 1, "tier": "S"}),
                      mk(2, "WR", 24, fc=3000, ktc=3000)]
        without = [mk(1, "RB", 24, fc=5000, ktc=5000),
                   mk(2, "WR", 24, fc=3000, ktc=3000)]
        a = score_players(with_flock)["players"]
        b = score_players(without)["players"]
        assert [p["score"] for p in a] == [p["score"] for p in b]
        assert a[0]["flock"] == {"rank": 1, "tier": "S"}

    def test_clamp_bounds(self):
        assert clamp(99) == 1.45 and clamp(-99) == 0.55

    def test_reference_curve_is_sorted_desc_and_drops_none(self):
        assert reference_curve([5, None, 10, 1]) == [10, 5, 1]
