"""Tests for the values seam and FantasyCalc provider."""

from unittest.mock import patch

import httpx
import pytest

from trade_scout.providers.fantasycalc import ValuesSourceError, fantasycalc_provider
from trade_scout.values import get_values


def _sample_fantasycalc_response():
    """Realistic FantasyCalc response shape."""
    return [
        {
            "player": {
                "sleeperId": "6794",
                "name": "Bijan Robinson",
                "position": "RB",
                "team": "ATL",
            },
            "value": 9500,
            "redraftValue": 7200,
            "overallRank": 1,
            "positionRank": 1,
            "trend30Day": 200,
        },
        {
            "player": {
                "sleeperId": "4034",
                "name": "Patrick Mahomes",
                "position": "QB",
                "team": "KC",
            },
            "value": 8200,
            "redraftValue": 5500,
            "overallRank": 2,
            "positionRank": 1,
            "trend30Day": -100,
        },
        {
            "player": {
                "sleeperId": None,
                "name": "No Sleeper ID Player",
                "position": "WR",
            },
            "value": 100,
            "overallRank": 500,
            "positionRank": 200,
            "trend30Day": 0,
        },
    ]


LEAGUE = {"num_qbs": 2, "num_teams": 12, "ppr": 1, "is_dynasty": True}


class TestFantasyCalcProvider:
    @patch("trade_scout.providers.fantasycalc.httpx.get")
    def test_parses_response(self, mock_get):
        mock_get.return_value = httpx.Response(
            200,
            json=_sample_fantasycalc_response(),
            request=httpx.Request("GET", "https://example.com"),
        )

        result = fantasycalc_provider(LEAGUE)

        assert "6794" in result
        assert result["6794"]["value"] == 9500
        assert result["6794"]["source"] == "FantasyCalc"
        assert result["6794"]["overall_rank"] == 1

    @patch("trade_scout.providers.fantasycalc.httpx.get")
    def test_skips_missing_sleeper_id(self, mock_get):
        mock_get.return_value = httpx.Response(
            200,
            json=_sample_fantasycalc_response(),
            request=httpx.Request("GET", "https://example.com"),
        )

        result = fantasycalc_provider(LEAGUE)
        # The player with sleeperId=None should not be in results
        assert len(result) == 2

    @patch("trade_scout.providers.fantasycalc.httpx.get")
    def test_http_error_raises(self, mock_get):
        mock_get.return_value = httpx.Response(
            500,
            json={"error": "server error"},
            request=httpx.Request("GET", "https://example.com"),
        )

        with pytest.raises(ValuesSourceError, match="HTTP"):
            fantasycalc_provider(LEAGUE)

    @patch("trade_scout.providers.fantasycalc.httpx.get")
    def test_bad_shape_raises(self, mock_get):
        mock_get.return_value = httpx.Response(
            200,
            json={"not": "a list"},
            request=httpx.Request("GET", "https://example.com"),
        )

        with pytest.raises(ValuesSourceError, match="unexpected shape"):
            fantasycalc_provider(LEAGUE)


class TestGetValues:
    @patch("trade_scout.providers.fantasycalc.httpx.get")
    def test_uses_fantasycalc(self, mock_get):
        """get_values should return FantasyCalc data directly."""
        mock_get.return_value = httpx.Response(
            200,
            json=_sample_fantasycalc_response(),
            request=httpx.Request("GET", "https://example.com"),
        )

        result = get_values(LEAGUE)
        assert "6794" in result
        assert result["6794"]["source"] == "FantasyCalc"
