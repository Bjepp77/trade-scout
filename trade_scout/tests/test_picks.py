"""Tests for draft pick valuation.

Picks were priced from a hardcoded table for months. The table is now the
fallback of last resort, and these lock down the three things that made
replacing it delicate: the two sources NAME picks differently, they PRICE them
on different scales, and they do not price the SAME SET of picks.
"""

from trade_scout.picks import (
    PickRef,
    build_pick_market,
    calibration_error,
    extract_fantasycalc_picks,
    extract_ktc_picks,
    lookup,
    parse_pick,
)


class TestParsing:
    def test_the_two_sources_name_the_same_pick_differently(self):
        # KTC says "2027 Early 1st", FantasyCalc says "2027 1st (Early)".
        # If these do not collapse to one key, the sources never blend.
        assert parse_pick("2027 Early 1st").key() == parse_pick("2027 1st (Early)").key()

    def test_exact_slots(self):
        r = parse_pick("2026 Pick 1.01")
        assert (r.year, r.round_, r.slot) == (2026, 1, 1)
        assert parse_pick("2026 1.12").key() == "2026-1.12"

    def test_untiered(self):
        r = parse_pick("2026 1st")
        assert (r.year, r.round_, r.tier, r.slot) == (2026, 1, None, None)

    def test_underscore_and_verbose_forms(self):
        assert parse_pick("2026_1").key() == parse_pick("2026 1st").key()
        assert parse_pick("2026 1st round pick").key() == parse_pick("2026 1st").key()

    def test_middle_normalises_to_mid(self):
        assert parse_pick("2027 middle 2nd").tier == "mid"

    def test_case_and_spacing_are_forgiving(self):
        assert parse_pick("  2027   EARLY   1ST ").key() == "2027-1-early"

    def test_a_player_name_is_not_a_pick(self):
        for name in ["Bijan Robinson", "", "next year's first", "1st", "2026"]:
            assert parse_pick(name) is None, name

    def test_a_slot_and_a_tier_are_different_keys(self):
        assert parse_pick("2026 1.01").key() != parse_pick("2026 early 1st").key()

    def test_label_reads_back(self):
        assert parse_pick("2027 early 1st").label() == "2027 Early 1st"
        assert parse_pick("2026 1.03").label() == "2026 1.03"


class TestExtractors:
    def test_fantasycalc_picks_are_found_by_POSITION_not_a_missing_id(self):
        # Every FantasyCalc entry carries a sleeperId, picks included: theirs is
        # synthetic ("DP_0_0"). Filtering on a missing id was tried first and
        # matched nothing at all.
        raw = [
            {"player": {"name": "2026 Pick 1.01", "position": "PICK",
                        "sleeperId": "DP_0_0"}, "value": 7016},
            {"player": {"name": "Bijan Robinson", "position": "RB",
                        "sleeperId": "8146"}, "value": 9000},
        ]
        assert extract_fantasycalc_picks(raw) == {"2026-1.01": 7016.0}

    def test_a_player_whose_name_looks_like_a_pick_is_not_extracted(self):
        raw = [{"player": {"name": "2026 Pick 1.01", "position": "WR",
                           "sleeperId": "1"}, "value": 100}]
        assert extract_fantasycalc_picks(raw) == {}

    def test_ktc_picks_are_found_by_RDP(self):
        ktc = {
            "2027 early 1st": {"name": "2027 Early 1st", "position": "RDP", "value": 7062},
            "bijan robinson": {"name": "Bijan Robinson", "position": "RB", "value": 9999},
        }
        assert extract_ktc_picks(ktc) == {"2027-1-early": 7062.0}

    def test_extractors_survive_junk(self):
        assert extract_fantasycalc_picks([]) == {}
        assert extract_ktc_picks({}) == {}
        assert extract_fantasycalc_picks([{"player": {}}]) == {}


class TestCalibration:
    # KTC runs hot: the same pick is 7062 to KTC and 4488 to FantasyCalc.
    FC = {"a": 4488.0, "b": 2955.0, "c": 2280.0, "d": 1833.0}
    KT = {"a": 7062.0, "b": 4488.0, "c": 2955.0, "d": 2367.0, "extra": 5500.0}

    def test_shared_keys_land_on_the_reference(self):
        m = build_pick_market(self.FC, self.KT)
        for k in ("a", "b", "c", "d"):
            assert abs(m[k]["ktc"] - self.FC[k]) < 1.0, k

    def test_a_ktc_only_pick_is_placed_on_the_reference_scale(self):
        # "extra" sits between a and b in KTC terms, so it must land between
        # their FantasyCalc values, not keep KTC's inflated number.
        m = build_pick_market(self.FC, self.KT)
        assert self.FC["b"] < m["extra"]["value"] < self.FC["a"]

    def test_a_single_source_pick_is_marked_single_source(self):
        m = build_pick_market(self.FC, self.KT)
        assert m["extra"]["sources"] == ["KeepTradeCut"]
        assert m["extra"]["disagreement_pct"] is None

    def test_too_few_shared_keys_means_no_fit(self):
        # One anchor cannot define a mapping. Returning the source unchanged is
        # honest; inventing a scale from one point is not.
        out = build_pick_market({"a": 100.0}, {"a": 500.0, "b": 400.0})
        assert out["b"]["ktc"] == 400.0

    def test_no_ktc_at_all_still_produces_a_market(self):
        m = build_pick_market(self.FC, {})
        assert set(m) == set(self.FC)
        assert all(e["sources"] == ["FantasyCalc"] for e in m.values())


class TestDisagreementIsNotFabricated:
    """The fit passes exactly through every shared key, so comparing the two
    sources there returns zero BY CONSTRUCTION. Reporting that as agreement
    would be inventing corroboration."""

    FC = {"a": 4488.0, "b": 2955.0, "c": 2280.0, "d": 1833.0, "e": 1500.0}
    KT = {"a": 7062.0, "b": 4488.0, "c": 2955.0, "d": 2367.0, "e": 2000.0}

    def test_leave_one_out_error_is_not_all_zero(self):
        err = calibration_error(self.KT, self.FC)
        assert err, "no shared keys were measured"
        assert any(v > 0 for v in err.values()), (
            "every disagreement is zero, which means the fit is being measured "
            "against itself"
        )

    def test_the_reported_disagreement_uses_leave_one_out(self):
        m = build_pick_market(self.FC, self.KT)
        reported = [m[k]["disagreement_pct"] for k in ("b", "c", "d")]
        assert any(v and v > 0 for v in reported)

    def test_too_few_points_reports_nothing_rather_than_zero(self):
        assert calibration_error({"a": 1.0, "b": 2.0}, {"a": 1.0, "b": 2.0}) == {}


class TestLookup:
    MARKET = {
        "2026-1.01": {"value": 7016.0, "sources": ["FantasyCalc"]},
        "2026-1.02": {"value": 4613.0, "sources": ["FantasyCalc"]},
        "2026-1.03": {"value": 4220.0, "sources": ["FantasyCalc"]},
        "2026-1.04": {"value": 3838.0, "sources": ["FantasyCalc"]},
        "2027-1-early": {"value": 4490.0, "sources": ["FantasyCalc", "KeepTradeCut"]},
        "2027-1-mid": {"value": 2956.0, "sources": ["FantasyCalc", "KeepTradeCut"]},
        "2027-1-late": {"value": 2280.0, "sources": ["FantasyCalc", "KeepTradeCut"]},
        "2028-1-any": {"value": 2075.0, "sources": ["FantasyCalc"]},
    }

    def test_an_exact_slot_is_used_as_asked(self):
        e, basis = lookup(self.MARKET, parse_pick("2026 1.01"))
        assert e["value"] == 7016.0 and basis == "exact"

    def test_an_exact_tier_is_used_as_asked(self):
        e, basis = lookup(self.MARKET, parse_pick("2027 early 1st"))
        assert e["value"] == 4490.0 and basis == "exact"

    def test_an_untiered_pick_prefers_the_two_source_mid_tier(self):
        e, basis = lookup(self.MARKET, parse_pick("2027 1st"))
        assert e["value"] == 2956.0
        assert "mid tier" in basis

    def test_a_current_year_pick_averages_the_known_slots(self):
        # The draft order is known, so the sources publish slots instead of
        # tiers. A pick with no slot yet is the expectation over all of them.
        e, basis = lookup(self.MARKET, parse_pick("2026 1st"))
        assert "known slots" in basis
        assert 3838.0 < e["value"] < 7016.0

    def test_falling_back_to_a_single_source_says_so(self):
        e, basis = lookup(self.MARKET, parse_pick("2028 1st"))
        assert e["value"] == 2075.0
        assert basis != "exact"

    def test_a_pick_nobody_prices_is_reported_missing_not_guessed(self):
        e, basis = lookup(self.MARKET, parse_pick("2031 4th"))
        assert e is None
        assert "not in the market" in basis

    def test_every_result_carries_a_basis(self):
        for q in ["2026 1.01", "2027 early 1st", "2027 1st", "2026 1st", "2031 4th"]:
            _e, basis = lookup(self.MARKET, parse_pick(q))
            assert basis, q


class TestTheOldTableIsNoLongerTheAnswer:
    def test_a_future_first_is_not_discounted_below_this_year(self):
        # The hardcoded table cut 20% a year, so a 2027 1st was always worth
        # less than a 2026 1st. The market does not agree, and a model that
        # bakes that in tells you to sell future firsts every time.
        from trade_scout.pick_values import pick_value

        assert pick_value(1, 1, "superflex") < pick_value(0, 1, "superflex"), (
            "the old table no longer discounts the future; this test is stale"
        )

    def test_the_market_can_price_a_future_first_above_a_current_one(self):
        market = {
            "2026-1-mid": {"value": 3515.0, "sources": ["FantasyCalc", "KeepTradeCut"]},
            "2027-1-mid": {"value": 3721.0, "sources": ["FantasyCalc", "KeepTradeCut"]},
        }
        now, _ = lookup(market, parse_pick("2026 1st"))
        future, _ = lookup(market, parse_pick("2027 1st"))
        assert future["value"] > now["value"]


class TestPickRef:
    def test_key_is_stable_and_distinct(self):
        assert PickRef(2027, 1, tier="early").key() == "2027-1-early"
        assert PickRef(2027, 1).key() == "2027-1-any"
        assert PickRef(2026, 1, slot=3).key() == "2026-1.03"
        keys = {PickRef(2027, 1, tier=t).key() for t in ("early", "mid", "late")}
        assert len(keys) == 3
