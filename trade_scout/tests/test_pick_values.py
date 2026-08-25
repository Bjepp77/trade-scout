"""Tests for draft pick valuation."""

from trade_scout.pick_values import parse_pick_code, pick_value


class TestPickValue:
    def test_first_round_this_year(self):
        val = pick_value(year_offset=0, round_=1)
        assert val == 5000

    def test_second_round_this_year(self):
        val = pick_value(year_offset=0, round_=2)
        assert val == 2500

    def test_third_round_this_year(self):
        val = pick_value(year_offset=0, round_=3)
        assert val == 1200

    def test_fourth_round_default(self):
        val = pick_value(year_offset=0, round_=4)
        assert val == 500

    def test_future_discount(self):
        this_year = pick_value(year_offset=0, round_=1)
        next_year = pick_value(year_offset=1, round_=1)
        assert next_year == int(this_year * 0.8)

    def test_two_years_out(self):
        val = pick_value(year_offset=2, round_=1)
        assert val == int(5000 * 0.8 * 0.8)

    def test_1qb_multiplier(self):
        sf = pick_value(year_offset=0, round_=1, format_="superflex")
        oqb = pick_value(year_offset=0, round_=1, format_="1qb")
        assert oqb == int(sf * 0.6)


class TestParsePickCode:
    def test_2026_1st(self):
        result = parse_pick_code("2026 1st")
        assert result is not None
        year, rnd, val = result
        assert year == 2026
        assert rnd == 1

    def test_2027_2nd(self):
        result = parse_pick_code("2027 2nd")
        assert result is not None
        assert result[0] == 2027
        assert result[1] == 2

    def test_underscore_format(self):
        result = parse_pick_code("2026_1")
        assert result is not None
        assert result[0] == 2026
        assert result[1] == 1

    def test_not_a_pick(self):
        assert parse_pick_code("Bijan Robinson") is None
        assert parse_pick_code("CMC") is None

    def test_3rd_round(self):
        result = parse_pick_code("2026 3rd")
        assert result is not None
        assert result[1] == 3
