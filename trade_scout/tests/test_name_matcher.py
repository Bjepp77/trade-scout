"""Tests for fuzzy name matching."""

from trade_scout.name_matcher import NameMatcher, _normalize


def _sample_players():
    return {
        "4866": {
            "first_name": "Saquon",
            "last_name": "Barkley",
            "position": "RB",
        },
        "6794": {
            "first_name": "Bijan",
            "last_name": "Robinson",
            "position": "RB",
        },
        "4034": {
            "first_name": "Patrick",
            "last_name": "Mahomes",
            "position": "QB",
        },
        "2133": {
            "first_name": "Christian",
            "last_name": "McCaffrey",
            "position": "RB",
        },
    }


class TestNormalize:
    def test_lowercase(self):
        assert _normalize("Bijan Robinson") == "bijan robinson"

    def test_strips_jr(self):
        assert _normalize("Marvin Harrison Jr") == "marvin harrison"

    def test_strips_punctuation(self):
        assert _normalize("Ja'Marr Chase") == "jamarr chase"


class TestNameMatcher:
    def test_exact_match(self):
        matcher = NameMatcher(_sample_players())
        assert matcher.match("Bijan Robinson") == "6794"

    def test_case_insensitive(self):
        matcher = NameMatcher(_sample_players())
        assert matcher.match("bijan robinson") == "6794"

    def test_no_match_returns_none(self):
        matcher = NameMatcher(_sample_players())
        assert matcher.match("Fake McPlayer") is None

    def test_position_filter(self):
        matcher = NameMatcher(_sample_players())
        # Patrick Mahomes is QB, not RB
        assert matcher.match("Patrick Mahomes", position="RB") is None
        assert matcher.match("Patrick Mahomes", position="QB") == "4034"

    def test_fuzzy_match(self):
        matcher = NameMatcher(_sample_players())
        # "Saquon" should fuzzy match
        result = matcher.match("Saquon Barkly")  # Typo in last name
        assert result == "4866"
