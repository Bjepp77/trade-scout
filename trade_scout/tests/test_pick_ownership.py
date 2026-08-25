"""Tests for reconstructing who owns which future pick.

Sleeper publishes only the EXCEPTIONS: one row per pick that changed hands.
Everything else is implied by a team owning its own, which means a bug here
does not look like an error, it looks like a roster quietly missing assets or
holding ones it traded away.
"""

from trade_scout.pick_ownership import (
    build_ownership,
    completed_seasons,
    future_seasons,
    project_tier,
)

ROSTERS = [str(i) for i in range(1, 13)]
DRAFTS_2026_DONE = [{"season": "2026", "status": "complete"}]


class TestWhichSeasonsCount:
    def test_a_completed_draft_is_spent(self):
        assert completed_seasons(DRAFTS_2026_DONE) == {2026}

    def test_an_in_progress_draft_is_not_spent(self):
        assert completed_seasons([{"season": "2027", "status": "pre_draft"}]) == set()

    def test_a_spent_season_is_excluded_even_though_its_rows_remain(self):
        # traded_picks keeps 2026 rows after the 2026 draft finishes. Showing
        # them would put twelve dead assets on every roster.
        traded = [{"season": "2026", "round": 1, "roster_id": 1, "owner_id": 2}]
        assert 2026 not in future_seasons(traded, DRAFTS_2026_DONE, 2026)

    def test_the_next_seasons_appear_even_with_no_trades_at_all(self):
        assert future_seasons([], DRAFTS_2026_DONE, 2026) == [2027, 2028, 2029]

    def test_seasons_are_capped(self):
        traded = [{"season": str(y), "round": 1, "roster_id": 1, "owner_id": 2}
                  for y in range(2027, 2040)]
        assert len(future_seasons(traded, DRAFTS_2026_DONE, 2026)) == 3

    def test_junk_rows_do_not_break_it(self):
        traded = [{"season": None}, {"season": "nonsense"}, {}]
        assert future_seasons(traded, DRAFTS_2026_DONE, 2026) == [2027, 2028, 2029]


class TestOwnership:
    def test_every_pick_is_accounted_for(self):
        own = build_ownership([], DRAFTS_2026_DONE, ROSTERS, 2026, rounds=4)
        total = sum(len(v) for v in own.values())
        assert total == 3 * 4 * 12  # seasons x rounds x teams

    def test_untraded_picks_stay_with_their_team(self):
        own = build_ownership([], DRAFTS_2026_DONE, ROSTERS, 2026, rounds=4)
        assert all(p["is_own"] for picks in own.values() for p in picks)

    def test_a_traded_pick_moves_and_is_not_marked_own(self):
        traded = [{"season": "2027", "round": 1, "roster_id": "1", "owner_id": "7"}]
        own = build_ownership(traded, DRAFTS_2026_DONE, ROSTERS, 2026, rounds=4)
        gained = [p for p in own["7"] if p["season"] == 2027 and p["round"] == 1]
        assert any(p["from_roster_id"] == "1" and not p["is_own"] for p in gained)

    def test_the_team_that_traded_it_no_longer_has_it(self):
        traded = [{"season": "2027", "round": 1, "roster_id": "1", "owner_id": "7"}]
        own = build_ownership(traded, DRAFTS_2026_DONE, ROSTERS, 2026, rounds=4)
        lost = [p for p in own["1"] if p["season"] == 2027 and p["round"] == 1]
        assert lost == [], "a traded pick is still on the original roster"

    def test_the_total_is_conserved_after_trades(self):
        # Picks move; they are not created or destroyed.
        traded = [
            {"season": "2027", "round": 1, "roster_id": "1", "owner_id": "7"},
            {"season": "2028", "round": 2, "roster_id": "3", "owner_id": "7"},
            {"season": "2029", "round": 4, "roster_id": "5", "owner_id": "2"},
        ]
        own = build_ownership(traded, DRAFTS_2026_DONE, ROSTERS, 2026, rounds=4)
        assert sum(len(v) for v in own.values()) == 3 * 4 * 12

    def test_a_team_can_hold_two_of_the_same_round(self):
        # Two 2028 2nds are different assets and must both appear.
        traded = [
            {"season": "2028", "round": 2, "roster_id": "3", "owner_id": "1"},
            {"season": "2028", "round": 2, "roster_id": "4", "owner_id": "1"},
        ]
        own = build_ownership(traded, DRAFTS_2026_DONE, ROSTERS, 2026, rounds=4)
        seconds = [p for p in own["1"] if p["season"] == 2028 and p["round"] == 2]
        assert len(seconds) == 3  # its own plus the two acquired
        assert len({p["from_roster_id"] for p in seconds}) == 3

    def test_a_pick_owned_by_an_unknown_roster_is_dropped_not_misfiled(self):
        traded = [{"season": "2027", "round": 1, "roster_id": "1", "owner_id": "99"}]
        own = build_ownership(traded, DRAFTS_2026_DONE, ROSTERS, 2026, rounds=4)
        assert "99" not in own
        assert not any(p["from_roster_id"] == "1" and p["season"] == 2027
                       and p["round"] == 1 for picks in own.values() for p in picks)

    def test_round_count_comes_from_the_league(self):
        own = build_ownership([], DRAFTS_2026_DONE, ROSTERS, 2026, rounds=2)
        assert sum(len(v) for v in own.values()) == 3 * 2 * 12

    def test_no_rosters_is_not_a_crash(self):
        assert build_ownership([], DRAFTS_2026_DONE, [], 2026) == {}

    def test_picks_come_back_in_a_stable_order(self):
        own = build_ownership([], DRAFTS_2026_DONE, ROSTERS, 2026, rounds=4)
        picks = own["1"]
        assert picks == sorted(
            picks, key=lambda p: (p["season"], p["round"], p["from_roster_id"]))


class TestProjectedTier:
    def test_the_weakest_rosters_pick_earliest(self):
        # The thing dynasty owners most often get backwards: a bad team's own
        # future first is MORE valuable, not less.
        assert project_tier(12, 12) == "early"
        assert project_tier(1, 12) == "late"

    def test_the_middle_is_mid(self):
        assert project_tier(6, 12) == "mid"

    def test_every_rank_lands_in_a_published_tier(self):
        tiers = {project_tier(r, 12) for r in range(1, 13)}
        assert tiers <= {"early", "mid", "late"}
        assert tiers == {"early", "mid", "late"}

    def test_a_degenerate_league_is_neutral_rather_than_a_crash(self):
        assert project_tier(1, 0) == "mid"
