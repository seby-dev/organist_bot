"""Tests for organist_bot.travel."""

from unittest.mock import MagicMock, patch

import organist_bot.travel as travel_mod


def _make_client(minutes: int | None = 30, status: str = "OK") -> MagicMock:
    if status == "OK" and minutes is None:
        raise ValueError("Cannot have status=OK with minutes=None — use a non-OK status instead")
    client = MagicMock()
    if minutes is None:
        client.distance_matrix.return_value = {"rows": [{"elements": [{"status": status}]}]}
    else:
        client.distance_matrix.return_value = {
            "rows": [{"elements": [{"status": status, "duration": {"value": minutes * 60}}]}]
        }
    return client


class TestGetTravelMinutes:
    def test_returns_drive_time_in_minutes(self):
        mock_client = _make_client(minutes=40)
        with patch("organist_bot.travel.settings") as mock_settings:
            mock_settings.google_maps_api_key = "key123"
            mock_settings.travel_home_postcode = "IG11 7ZW"
            mock_settings.home_postcode = "E1 1AA"
            result = travel_mod.get_travel_minutes("CM1 1AA", _client=mock_client)
        assert result == 40

    def test_uses_travel_home_postcode_as_origin(self):
        mock_client = _make_client(minutes=20)
        with patch("organist_bot.travel.settings") as mock_settings:
            mock_settings.google_maps_api_key = "key"
            mock_settings.travel_home_postcode = "IG11 7ZW"
            mock_settings.home_postcode = "E1 1AA"
            travel_mod.get_travel_minutes("SW1A 1AA", _client=mock_client)
        call_kwargs = mock_client.distance_matrix.call_args
        assert call_kwargs.kwargs["origins"] == ["IG11 7ZW"]
        assert call_kwargs.kwargs["destinations"] == ["SW1A 1AA"]

    def test_falls_back_to_home_postcode_when_travel_home_blank(self):
        mock_client = _make_client(minutes=25)
        with patch("organist_bot.travel.settings") as mock_settings:
            mock_settings.google_maps_api_key = "key"
            mock_settings.travel_home_postcode = ""
            mock_settings.home_postcode = "E1 1AA"
            travel_mod.get_travel_minutes("SW1A 1AA", _client=mock_client)
        call_kwargs = mock_client.distance_matrix.call_args
        assert call_kwargs.kwargs["origins"] == ["E1 1AA"]

    def test_returns_none_for_blank_postcode(self):
        with patch("organist_bot.travel.settings") as mock_settings:
            mock_settings.google_maps_api_key = "key"
            mock_settings.travel_home_postcode = "IG11 7ZW"
            result = travel_mod.get_travel_minutes("")
        assert result is None

    def test_returns_none_for_whitespace_postcode(self):
        with patch("organist_bot.travel.settings") as mock_settings:
            mock_settings.google_maps_api_key = "key"
            mock_settings.travel_home_postcode = "IG11 7ZW"
            result = travel_mod.get_travel_minutes("   ")
        assert result is None

    def test_returns_none_when_api_key_missing(self):
        with patch("organist_bot.travel.settings") as mock_settings:
            mock_settings.google_maps_api_key = ""
            result = travel_mod.get_travel_minutes("CM1 1AA")
        assert result is None

    def test_returns_none_on_non_ok_status(self):
        mock_client = _make_client(minutes=None, status="ZERO_RESULTS")
        with patch("organist_bot.travel.settings") as mock_settings:
            mock_settings.google_maps_api_key = "key"
            mock_settings.travel_home_postcode = "IG11 7ZW"
            mock_settings.home_postcode = "E1 1AA"
            result = travel_mod.get_travel_minutes("ZZ1 1ZZ", _client=mock_client)
        assert result is None

    def test_returns_none_on_api_exception(self):
        mock_client = MagicMock()
        mock_client.distance_matrix.side_effect = Exception("network error")
        with patch("organist_bot.travel.settings") as mock_settings:
            mock_settings.google_maps_api_key = "key"
            mock_settings.travel_home_postcode = "IG11 7ZW"
            mock_settings.home_postcode = ""
            result = travel_mod.get_travel_minutes("CM1 1AA", _client=mock_client)
        assert result is None
