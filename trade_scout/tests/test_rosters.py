"""Tests for the read-only roster board the console renders.

The board decides three things that are easy to get quietly wrong:
  1. an UNPRICED player is not a worthless player
  2. a player Sleeper does not recognise is still ON the roster
  3. sort order, which is the whole point of the feature
"""

from unittest.mock import MagicMock, patch

import trade_scout.rosters as R
from trade_scout.model import Weights, score_players
from trade_scout.rosters import _player_record, build_rosters

PLAYERS = {
    "1": {"full_name": "Alpha Back", "position": "RB", "team": "ATL", "age": 24, "years_exp": 2},
    "2": {"first_name": "Beta", "last_name": "Wide", "position": "WR", "team": "BUF", "age": 27},
    "3": {"full_name": "Gamma Tight", "position": "TE", "team": "CIN", "age": 21},
    # No name at all, which is what a practice-squad id looks like.
    "4": {"position": "QB"},
}

VALUES = {
    "1": {"value": 5000, "overall_rank": 10, "position_rank": 3, "trend_30day": 120},
    "2": {"value": 9000, "overall_rank": 2, "position_rank": 1, "trend_30day": -40},
    # "3" and "4" deliberately unpriced.
    # Priced at EXACTLY zero, which is not the same thing as unpriced and is
    # the only fixture that can tell the two sort keys apart.
    "5": {"value": 0, "overall_rank": 900, "position_rank": 90, "trend_30day": 0},
}

PLAYERS["5"] = {"full_name": "Zed Zero", "position": "WR", "team": "DEN", "age": 30}


def _client(rosters, users):
    c = MagicMock()
    c.get_league.return_value = {"name": "Test League", "season": "2026"}
    c.get_league_settings.return_value = {"format_str": "1QB / 12-team / Standard"}
    c.get_rosters.return_value = rosters
    c.get_users.return_value = users
    c.get_players_map.return_value = PLAYERS
    # Real lists, not MagicMocks: the pick reconstruction iterates these.
    c.get_traded_picks.return_value = []
    c.get_drafts.return_value = []
    return c


def _build(rosters, users, owner=None, values=VALUES, values_exc=None, ktc=None,
           flock=None):
    # KTC is patched too. Without it these tests reach out to keeptradecut.com,
    # which makes the unit suite depend on someone else's uptime and took it
    # from 0.2s to 10s.
    with patch("trade_scout.rosters.SleeperClient", return_value=_client(rosters, users)), \
            patch("trade_scout.rosters.get_ktc_values", return_value=(ktc or {})), \
            patch("trade_scout.rosters._try_flock_rankings", return_value=(flock or {})):
        target = "trade_scout.rosters.get_values"
        if values_exc is not None:
            with patch(target, side_effect=values_exc):
                return build_rosters("L1", owner)
        with patch(target, return_value=values):
            return build_rosters("L1", owner)


class TestPlayerRecord:
    def test_full_name_preferred(self):
        assert _player_record("1", PLAYERS, VALUES)["name"] == "Alpha Back"

    def test_first_last_fallback(self):
        assert _player_record("2", PLAYERS, VALUES)["name"] == "Beta Wide"

    def test_nameless_player_shows_its_id(self):
        # Dropping the row would silently shrink somebody's roster.
        assert _player_record("4", PLAYERS, VALUES)["name"] == "ID:4"

    def test_unknown_id_still_produces_a_row(self):
        rec = _player_record("999", PLAYERS, VALUES)
        assert rec["name"] == "ID:999"
        assert rec["position"] == "UNK"

    def test_unpriced_value_is_none_not_zero(self):
        # None is an absence of opinion; 0 is a price. Collapsing them makes
        # every unvalued rookie sort as worthless.
        assert _player_record("3", PLAYERS, VALUES)["value"] is None

    def test_priced_value_carries_ranks_and_trend(self):
        rec = _player_record("1", PLAYERS, VALUES)
        assert rec["value"] == 5000
        assert rec["overall_rank"] == 10
        assert rec["position_rank"] == 3
        assert rec["trend_30day"] == 120


class TestBuildRosters:
    def setup_method(self):
        self.rosters = [
            {"roster_id": 1, "owner_id": "u1", "players": ["3", "1"],
             "settings": {"wins": 2, "losses": 1}},
            {"roster_id": 2, "owner_id": "u2", "players": ["2"],
             "settings": {"wins": 0, "losses": 3}},
        ]
        self.users = [
            {"user_id": "u1", "display_name": "Me"},
            {"user_id": "u2", "username": "them_no_display"},
        ]

    def test_players_sort_by_value_desc_with_unpriced_last(self):
        board = _build(self.rosters, self.users, owner="u1")
        mine = [t for t in board["teams"] if t["roster_id"] == "1"][0]
        assert [p["name"] for p in mine["players"]] == ["Alpha Back", "Gamma Tight"]

    def test_priced_at_zero_still_outranks_unpriced(self):
        # "worth nothing" and "nobody has an opinion" are different claims, and
        # a sort key that treats None as 0 cannot tell them apart.
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": ["3", "5"]}]
        board = _build(rosters, self.users, owner="u1")
        assert [p["name"] for p in board["teams"][0]["players"]] == ["Zed Zero", "Gamma Tight"]

    def test_an_unrecognised_player_id_stays_on_the_roster(self):
        # Testing _player_record alone does not cover this: the row can still be
        # filtered out by the caller, which silently shrinks somebody's team.
        rosters = [{"roster_id": 1, "owner_id": "u1", "players": ["1", "9999"]}]
        board = _build(rosters, self.users, owner="u1")
        names = [p["name"] for p in board["teams"][0]["players"]]
        assert names == ["Alpha Back", "ID:9999"]
        assert board["player_count"] == 2

    def test_an_ownerless_roster_is_not_flagged_as_yours(self):
        # The satellite passes ownerId as "" when it has none, and a roster with
        # no owner_id also reduces to "". Comparing those two directly makes an
        # abandoned team show up as YOUR team on the board.
        rosters = [
            {"roster_id": 1, "owner_id": None, "players": ["1"]},
            {"roster_id": 2, "owner_id": "u2", "players": ["2"]},
        ]
        board = _build(rosters, self.users, owner="")
        assert not any(t["is_owner"] for t in board["teams"])

    def test_total_value_ignores_unpriced(self):
        board = _build(self.rosters, self.users, owner="u1")
        mine = [t for t in board["teams"] if t["roster_id"] == "1"][0]
        assert mine["total_value"] == 5000
        assert mine["priced_count"] == 1
        assert mine["unpriced_count"] == 1

    def test_owner_flagged_only_on_the_owners_team(self):
        board = _build(self.rosters, self.users, owner="u1")
        flags = {t["roster_id"]: t["is_owner"] for t in board["teams"]}
        assert flags == {"1": True, "2": False}

    def test_no_owner_id_flags_nobody(self):
        board = _build(self.rosters, self.users, owner=None)
        assert not any(t["is_owner"] for t in board["teams"])

    def test_username_used_when_display_name_absent(self):
        board = _build(self.rosters, self.users, owner="u1")
        theirs = [t for t in board["teams"] if t["roster_id"] == "2"][0]
        assert theirs["owner"] == "them_no_display"

    def test_missing_user_gets_a_labeled_owner(self):
        board = _build(self.rosters, [], owner="u1")
        assert all(t["owner"] == "unknown owner" for t in board["teams"])

    def test_teams_sorted_by_total_score_desc(self):
        # Teams rank by the MODEL's score now, not raw market value: the score
        # is the thing the owner asked to sort by. total_value is still
        # reported underneath as the check on it.
        board = _build(self.rosters, self.users, owner="u1")
        scores = [t["total_score"] for t in board["teams"]]
        assert scores == sorted(scores, reverse=True)
        assert all("total_value" in t for t in board["teams"])

    def test_empty_roster_does_not_crash(self):
        board = _build([{"roster_id": 9, "owner_id": "u1", "players": []}], self.users)
        assert board["teams"][0]["players"] == []
        assert board["teams"][0]["total_value"] == 0

    def test_missing_players_key_is_an_empty_roster(self):
        board = _build([{"roster_id": 9, "owner_id": "u1"}], self.users)
        assert board["teams"][0]["players"] == []

    def test_player_count_totals_every_roster(self):
        board = _build(self.rosters, self.users, owner="u1")
        assert board["player_count"] == 3

    def test_values_source_named_when_present(self):
        assert _build(self.rosters, self.users)["values_source"] == "FantasyCalc"

    def test_values_failure_degrades_rather_than_raising(self):
        from trade_scout.values import ValuesError

        board = _build(self.rosters, self.users, owner="u1",
                       values_exc=ValuesError("provider down"))
        # The names and the ownership are still true, and the reason is stated.
        assert board["values_error"] == "provider down"
        assert board["values_source"] is None
        assert board["player_count"] == 3
        assert all(p["value"] is None for t in board["teams"] for p in t["players"])

    def test_unpriced_board_totals_zero_rather_than_crashing(self):
        from trade_scout.values import ValuesError

        board = _build(self.rosters, self.users, values_exc=ValuesError("down"))
        assert all(t["total_value"] == 0 for t in board["teams"])


class TestFlockOnTheBoard:
    """Flock is a rank and a tier. It reaches the board as context and is
    asserted never to move a score, because there is no honest conversion from
    a rank to a price."""

    ROSTERS = [{"roster_id": 1, "owner_id": "u1", "players": ["1", "2"]}]
    USERS = [{"user_id": "u1", "display_name": "Me"}]
    FLOCK = {"alpha back": {"flock_rank": 3.5, "flock_tier": "S",
                            "flock_pos_rank": 1, "rank_delta": 2}}

    def test_flock_reaches_the_player_record(self):
        board = _build(self.ROSTERS, self.USERS, owner="u1", flock=self.FLOCK)
        alpha = [p for p in board["teams"][0]["players"] if p["name"] == "Alpha Back"][0]
        assert alpha["flock"] == {"rank": 3.5, "tier": "S",
                                  "position_rank": 1, "rank_delta": 2}

    def test_a_player_flock_does_not_rank_is_none_not_zero(self):
        board = _build(self.ROSTERS, self.USERS, owner="u1", flock=self.FLOCK)
        beta = [p for p in board["teams"][0]["players"] if p["name"] == "Beta Wide"][0]
        assert beta["flock"] is None

    def test_flock_never_changes_a_score(self):
        # THE invariant. If this fails, a rank has been turned into a price.
        without = _build(self.ROSTERS, self.USERS, owner="u1")
        with_flock = _build(self.ROSTERS, self.USERS, owner="u1", flock=self.FLOCK)
        a = {p["name"]: p["score"] for p in without["teams"][0]["players"]}
        b = {p["name"]: p["score"] for p in with_flock["teams"][0]["players"]}
        assert a == b

    def test_the_board_says_whether_flock_was_available(self):
        assert _build(self.ROSTERS, self.USERS)["flock_source"] is None
        got = _build(self.ROSTERS, self.USERS, flock=self.FLOCK)
        assert got["flock_source"] == "Flock Fantasy"
        assert got["flock_matched"] == 1


class TestPicksAreWithheldRatherThanFabricated:
    """One dead Sleeper endpoint used to produce two confident lies.

    With no traded rows the board asserted that NOBODY has ever traded a pick,
    and with no draft list it could not tell a spent draft from a future one,
    so a completed class came back as live assets on every roster. Empty lists
    look like a safe fallback and are not.
    """

    ROSTERS = [{"roster_id": 1, "owner_id": "u1", "players": ["1"]},
               {"roster_id": 2, "owner_id": "u2", "players": ["2"]}]
    USERS = [{"user_id": "u1", "display_name": "Me"},
             {"user_id": "u2", "display_name": "Them"}]

    def _build_with(self, traded=None, drafts=None, traded_exc=None, drafts_exc=None):
        client = _client(self.ROSTERS, self.USERS)
        client.get_league.return_value = {
            "name": "T", "season": "2026", "settings": {"draft_rounds": 2}}
        if traded_exc:
            client.get_traded_picks.side_effect = traded_exc
        else:
            client.get_traded_picks.return_value = traded or []
        if drafts_exc:
            client.get_drafts.side_effect = drafts_exc
        else:
            client.get_drafts.return_value = drafts or []
        with patch("trade_scout.rosters.SleeperClient", return_value=client), \
                patch("trade_scout.rosters.get_ktc_values", return_value={}), \
                patch("trade_scout.rosters._try_flock_rankings", return_value={}), \
                patch("trade_scout.rosters.get_values", return_value=VALUES):
            return build_rosters("L1", "u1")

    def test_a_dead_traded_picks_endpoint_withholds_every_pick(self):
        board = self._build_with(traded_exc=RuntimeError("boom"))
        assert board["pick_count"] == 0
        assert board["picks_error"]

    def test_a_dead_drafts_endpoint_withholds_too(self):
        # This is the one that resurrected a completed draft.
        board = self._build_with(drafts_exc=RuntimeError("boom"))
        assert board["pick_count"] == 0
        assert board["picks_error"]

    def test_an_unexpected_shape_is_not_treated_as_empty(self):
        client = _client(self.ROSTERS, self.USERS)
        client.get_traded_picks.return_value = {"not": "a list"}
        client.get_drafts.return_value = []
        with patch("trade_scout.rosters.SleeperClient", return_value=client), \
                patch("trade_scout.rosters.get_ktc_values", return_value={}), \
                patch("trade_scout.rosters._try_flock_rankings", return_value={}), \
                patch("trade_scout.rosters.get_values", return_value=VALUES):
            board = build_rosters("L1", "u1")
        assert board["pick_count"] == 0
        assert board["picks_error"]

    def test_healthy_endpoints_still_produce_picks(self):
        board = self._build_with(
            traded=[], drafts=[{"season": "2026", "status": "complete"}])
        assert board["pick_count"] > 0
        assert board["picks_error"] is None

    def test_a_completed_draft_never_appears(self):
        board = self._build_with(
            traded=[], drafts=[{"season": "2026", "status": "complete"}])
        seasons = {p["season"] for t in board["teams"] for p in t["picks"]}
        assert 2026 not in seasons

    def test_players_survive_a_pick_failure(self):
        board = self._build_with(traded_exc=RuntimeError("boom"))
        assert board["player_count"] == 2


class TestTheAgeTermStaysOff:
    """Age is off by measurement, so a silent default must not switch it back.

    Dynasty prices already encode remaining career window. Multiplying by an
    age factor on top charges for it twice. Measured on a reconstructed 30-day
    panel, the age factor was anti-predictive in BOTH leagues by two
    independent routes (IC -0.102 / -0.099 for age alone, and removing it moved
    the composite from -0.0225 to +0.0483 in DD and -0.0713 to -0.0095 in CL).

    Nothing here claims age is proven harmful. It is unproven and points the
    wrong way, which is enough to keep it out. If someone turns it back on,
    that should be a decision with new evidence, not a default drifting back.
    """

    def _weights_used(self):
        captured = {}
        real = R.score_players

        def spy(players, **kw):
            captured["w"] = kw.get("weights")
            return real(players, **kw)

        client = _client([{"roster_id": 1, "owner_id": "u1", "players": ["1"]}],
                         [{"user_id": "u1", "display_name": "Me"}])
        with patch("trade_scout.rosters.SleeperClient", return_value=client), \
                patch("trade_scout.rosters.get_ktc_values", return_value={}), \
                patch("trade_scout.rosters._try_flock_rankings", return_value={}), \
                patch("trade_scout.rosters.get_values", return_value=VALUES), \
                patch("trade_scout.rosters.score_players", side_effect=spy):
            build_rosters("L1", "u1")
        return captured.get("w")

    def test_the_board_scores_with_the_age_term_disabled(self):
        w = self._weights_used()
        assert w is not None, "score_players was never called"
        assert w.age == 0.0, "the age term came back on"

    def test_the_scarcity_term_is_disabled_too(self):
        """Scarcity takes four values, one per position: a bet that the market
        misprices whole positions, against a source already fetched per format.
        Measured IC +0.0156 (DD) / -0.0519 (CL), inconsistent and CI-straddling,
        while leaving a spurious 15% QB discount in both formats."""
        w = self._weights_used()
        assert w.scarcity == 0.0, "the scarcity term came back on"

    def test_the_board_does_not_reprice_whole_positions(self):
        """The property. A model that adjusts individual players must not
        silently restate what an entire position is worth."""
        players, per_pos = [], {}
        for pos in ("QB", "RB", "WR", "TE"):
            for i in range(12):
                players.append({
                    "id": f"{pos}{i}", "name": f"{pos}{i}", "position": pos,
                    "age": 25, "ktc": None, "flock": None,
                    "fantasycalc": {"value": 9000 - i * 600, "trend_30day": 0},
                })
        out = score_players(players, num_teams=12,
                            weights=Weights(age=0.0, scarcity=0.0, trend=0.0))
        for r in out["players"]:
            src = next(p for p in players if p["id"] == r["id"])
            per_pos.setdefault(src["position"], []).append(
                r["score"] / src["fantasycalc"]["value"])
        for pos, ratios in per_pos.items():
            med = sorted(ratios)[len(ratios) // 2]
            assert abs(med - 1.0) < 0.10, f"{pos} repriced to {med:.3f}"

    def test_two_identical_players_of_different_ages_score_the_same(self):
        # The property, not the flag. A future refactor could keep age == 0.0
        # and reintroduce the multiplier by another route.
        base = {"position": "RB", "ktc": None, "flock": None,
                "fantasycalc": {"value": 5000, "trend_30day": 0}}
        players = [dict(base, id="young", name="Young", age=22),
                   dict(base, id="old", name="Old", age=31)]
        out = score_players(players, num_teams=12, weights=Weights(age=0.0))
        by = {r["id"]: r["score"] for r in out["players"]}
        assert by["young"] == by["old"], "age still moved the score"


class TestExpertDivergence:
    """The aggregated market compared against expert consensus, never blended.

    A blended number cannot disagree with itself. Averaging Flock into the
    price destroys the one signal this system can produce that a price cannot:
    two independent reads diverge, so slow down and check the news.
    """

    def _players(self, n=200):
        # market rank i, flock rank i: perfect agreement to start from.
        return [{"id": str(i), "name": f"P{i}", "market": 10000 - i * 10,
                 "flock": {"rank": float(i)}} for i in range(1, n + 1)]

    def test_agreement_produces_no_flags(self):
        ps = self._players()
        assert R.attach_expert_divergence(ps) == 0
        assert all(p["expert"]["gap"] == 0 for p in ps)

    def test_a_player_the_experts_rate_higher_is_labelled_flock_higher(self):
        """Lower rank number is better. Getting this backwards inverts the
        advice, and the first version of this code did exactly that."""
        ps = self._players()
        # Make P100 cheap in the market but keep his strong expert rank.
        target = next(p for p in ps if p["id"] == "100")
        target["market"] = 1.0
        R.attach_expert_divergence(ps)
        assert target["expert"]["market_rank"] > target["expert"]["flock_rank"]
        assert target["expert"]["direction"] == "flock_higher"

    def test_a_player_the_market_rates_higher_is_labelled_market_higher(self):
        ps = self._players()
        target = next(p for p in ps if p["id"] == "100")
        target["flock"] = {"rank": 999.0}
        R.attach_expert_divergence(ps)
        assert target["expert"]["market_rank"] < target["expert"]["flock_rank"]
        assert target["expert"]["direction"] == "market_higher"

    def test_both_sides_are_ranked_inside_the_shared_set(self):
        """Flock ranks against its own 438-player board; the market rank here is
        over ~340 rostered players. Comparing those directly made every player
        look market-inflated: the first run flagged 'market higher' for all six
        top divergences in BOTH leagues."""
        ps = [{"id": str(i), "name": f"P{i}", "market": 1000 - i,
               # Flock ranks from its full board, so these are all inflated.
               "flock": {"rank": float(i) * 1.5 + 90}} for i in range(1, 51)]
        R.attach_expert_divergence(ps)
        # Re-ranked, the two orderings are identical, so nothing diverges.
        assert all(p["expert"]["gap"] == 0 for p in ps)
        assert {p["expert"]["direction"] for p in ps} == {"agree"}

    def test_disagreement_below_the_knee_is_not_flagged(self):
        """Deep values are flat, so a big rank gap there is worth a rounding
        error and eight rankers must put somebody at 287."""
        ps = self._players()
        deep = next(p for p in ps if p["id"] == "190")
        deep["flock"] = {"rank": 1.0}
        R.attach_expert_divergence(ps)
        assert deep["expert"]["market_rank"] > R.EXPERT_TOP_N
        assert not deep["expert"]["flagged"]

    def test_a_big_top_of_board_disagreement_is_flagged(self):
        ps = self._players()
        target = next(p for p in ps if p["id"] == "50")
        target["flock"] = {"rank": 140.0}
        n = R.attach_expert_divergence(ps)
        assert target["expert"]["flagged"]
        assert n >= 1

    def test_players_without_both_reads_are_skipped_not_guessed(self):
        ps = self._players(20)
        ps.append({"id": "x", "name": "NoFlock", "market": 500, "flock": None})
        ps.append({"id": "y", "name": "NoMarket", "market": None,
                   "flock": {"rank": 3.0}})
        R.attach_expert_divergence(ps)
        assert "expert" not in ps[-1]
        assert "expert" not in ps[-2]

    def test_an_empty_board_is_not_a_crash(self):
        assert R.attach_expert_divergence([]) == 0
