"""Tests for organist_bot.analytics."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock, patch

import pytest

import organist_bot.analytics as analytics
from organist_bot.analytics import _classify_gig_type

# ────────────────────────────────────────────────────────────────────────────
# Shared helpers
# ────────────────────────────────────────────────────────────────────────────


def _make_record(
    status: str,
    applied_at: str,
    updated_at: str,
    header: str = "Sunday Service",
) -> dict:
    return {
        "status": status,
        "applied_at": applied_at,
        "updated_at": updated_at,
        "header": header,
        "url": "https://organistsonline.org/gig/1",
        "organisation": "St Paul's",
        "date": "2026-06-01",
        "fee": "£150",
        "email": "test@example.com",
    }


_T0 = "2026-01-01T12:00:00Z"
_T3 = "2026-01-04T12:00:00Z"  # 3 days after T0
_T7 = "2026-01-08T12:00:00Z"  # 7 days after T0


# Fake TypeSafe response helpers for Choice-based _classify_gig_type tests


@dataclass
class _FakeChoiceAnswer:
    choice: str
    confidence: float = 0.95


@dataclass
class _FakeChoiceResponse:
    choices: dict


def _fake_choice_client(choice: str, *, raises: Exception | None = None):
    """Return a fake sebby.judgement client whose system_one() returns a fixed
    gig_type choice, or raises *raises* if given."""
    client = MagicMock()
    if raises is not None:
        client.system_one.side_effect = raises
    else:
        client.system_one.return_value = _FakeChoiceResponse(
            choices={"gig_type": _FakeChoiceAnswer(choice=choice)}
        )
    return client


# ────────────────────────────────────────────────────────────────────────────
# get_success_metrics
# ────────────────────────────────────────────────────────────────────────────


class TestGetSuccessMetrics:
    def test_empty_records(self):
        with patch.object(analytics.application_store, "list_applications", return_value=[]):
            m = analytics.get_success_metrics()
        assert m["total"] == 0
        assert m["accepted"] == 0
        assert m["rejected"] == 0
        assert m["no_response"] == 0
        assert m["applied"] == 0
        assert m["acceptance_rate"] == 0.0
        assert m["response_rate"] == 0.0
        assert m["avg_response_days"] is None

    def test_acceptance_and_response_rates(self):
        records = [
            _make_record("accepted", _T0, _T3),
            _make_record("accepted", _T0, _T3),
            _make_record("rejected", _T0, _T3),
            _make_record("no_response", _T0, _T0),
        ]
        with patch.object(analytics.application_store, "list_applications", return_value=records):
            m = analytics.get_success_metrics()
        assert m["accepted"] == 2
        assert m["rejected"] == 1
        assert m["no_response"] == 1
        # resolved = 4 (no pending), acceptance = 2/4 = 50%, response = 3/4 = 75%
        assert m["acceptance_rate"] == 50.0
        assert m["response_rate"] == 75.0

    def test_excludes_pending_from_rates(self):
        records = [
            _make_record("accepted", _T0, _T3),
            _make_record("applied", _T0, _T0),  # pending — must NOT shift rates
        ]
        with patch.object(analytics.application_store, "list_applications", return_value=records):
            m = analytics.get_success_metrics()
        # resolved = 1 (accepted only), acceptance = 1/1 = 100%
        assert m["acceptance_rate"] == 100.0
        assert m["applied"] == 1

    def test_declined_grouped_with_rejected(self):
        records = [_make_record("declined", _T0, _T3)]
        with patch.object(analytics.application_store, "list_applications", return_value=records):
            m = analytics.get_success_metrics()
        assert m["rejected"] == 1

    def test_avg_response_days(self):
        records = [
            _make_record("accepted", _T0, _T3),  # 3 days
            _make_record("rejected", _T0, _T7),  # 7 days
        ]
        with patch.object(analytics.application_store, "list_applications", return_value=records):
            m = analytics.get_success_metrics()
        assert m["avg_response_days"] == 5.0

    def test_avg_response_days_none_when_no_resolved(self):
        records = [_make_record("no_response", _T0, _T0)]
        with patch.object(analytics.application_store, "list_applications", return_value=records):
            m = analytics.get_success_metrics()
        assert m["avg_response_days"] is None

    def test_exception_returns_empty_sentinel(self):
        with patch.object(
            analytics.application_store, "list_applications", side_effect=Exception("boom")
        ):
            m = analytics.get_success_metrics()
        assert m["total"] == 0
        assert m["acceptance_rate"] == 0.0
        assert m["avg_response_days"] is None


# ────────────────────────────────────────────────────────────────────────────
# get_gig_type_breakdown
# ────────────────────────────────────────────────────────────────────────────


class TestGetGigTypeBreakdown:
    def test_empty_records_returns_empty_dict(self):
        with patch.object(analytics.application_store, "list_applications", return_value=[]):
            b = analytics.get_gig_type_breakdown()
        assert b == {}

    def test_typesafe_classification_used_when_available(self):
        """get_gig_type_breakdown should call _classify_gig_type, which uses TypeSafe."""
        records = [
            _make_record("applied", _T0, _T0, header="Wedding at St Mary's"),
            _make_record("accepted", _T0, _T0, header="Funeral Service"),
        ]
        with (
            patch.object(analytics.application_store, "list_applications", return_value=records),
            patch("organist_bot.analytics._classify_gig_type") as mock_classify,
        ):
            mock_classify.side_effect = lambda h: "Wedding" if "Wedding" in h else "Funeral"
            b = analytics.get_gig_type_breakdown()
        assert "Wedding" in b
        assert "Funeral" in b
        assert mock_classify.call_count == 2

    def test_acceptance_rate_calculation(self):
        records = [
            _make_record("accepted", _T0, _T0, header="Wedding"),
            _make_record("accepted", _T0, _T0, header="Wedding"),
            _make_record("no_response", _T0, _T0, header="Wedding"),
            _make_record("no_response", _T0, _T0, header="Wedding"),
        ]
        with (
            patch.object(analytics.application_store, "list_applications", return_value=records),
            patch("organist_bot.analytics._classify_gig_type", return_value="Wedding"),
        ):
            b = analytics.get_gig_type_breakdown()
        assert b["Wedding"]["count"] == 4
        assert b["Wedding"]["accepted"] == 2
        assert b["Wedding"]["acceptance_rate"] == 50.0

    def test_exception_returns_empty_dict(self):
        with patch.object(
            analytics.application_store, "list_applications", side_effect=Exception("boom")
        ):
            b = analytics.get_gig_type_breakdown()
        assert b == {}


# ────────────────────────────────────────────────────────────────────────────
# _classify_gig_type (unit tests for the Choice call and fallback)
# ────────────────────────────────────────────────────────────────────────────


class TestClassifyGigType:
    """Unit tests for _classify_gig_type.

    Note: _classify_gig_type uses lru_cache — each test clears the cache so
    patches from one test don't bleed into others.
    """

    def setup_method(self):
        _classify_gig_type.cache_clear()

    def test_choice_key_mapped_to_display_label(self):
        with patch("organist_bot.analytics.make_client") as mock_make_client:
            mock_make_client.return_value = _fake_choice_client("wedding")
            result = _classify_gig_type("Wedding at St Mary's")
        assert result == "Wedding"

    def test_carol_service_label(self):
        with patch("organist_bot.analytics.make_client") as mock_make_client:
            mock_make_client.return_value = _fake_choice_client("carol_service")
            result = _classify_gig_type("Christmas Carol Service")
        assert result == "Carol Service"

    def test_other_label_for_unknown_category(self):
        with patch("organist_bot.analytics.make_client") as mock_make_client:
            mock_make_client.return_value = _fake_choice_client("other")
            result = _classify_gig_type("Organist Required")
        assert result == "Other"

    def test_api_exception_falls_back_to_keyword_matching(self):
        """On API failure, the keyword-based fallback should still classify."""
        with patch("organist_bot.analytics.make_client") as mock_make_client:
            mock_make_client.return_value = _fake_choice_client(
                "wedding", raises=RuntimeError("network down")
            )
            result = _classify_gig_type("Wedding at St Mary's")
        assert result == "Wedding"  # keyword fallback picks it up

    def test_api_exception_falls_back_to_other_when_no_keyword_matches(self):
        with patch("organist_bot.analytics.make_client") as mock_make_client:
            mock_make_client.return_value = _fake_choice_client(
                "other", raises=RuntimeError("down")
            )
            result = _classify_gig_type("Locum Organist Needed")
        assert result == "Other"

    def test_carol_beats_service_in_keyword_fallback(self):
        """Keyword fallback priority order: carol keyword wins over service keyword."""
        with patch("organist_bot.analytics.make_client") as mock_make_client:
            mock_make_client.return_value = _fake_choice_client(
                "carol_service", raises=RuntimeError("down")
            )
            result = _classify_gig_type("Christmas Carol Service")
        assert result == "Carol Service"

    def test_malformed_response_falls_back_to_keyword(self):
        """Missing 'gig_type' key in choices falls back to keyword matching."""
        with patch("organist_bot.analytics.make_client") as mock_make_client:
            client = MagicMock()
            client.system_one.return_value = _FakeChoiceResponse(choices={})  # missing key
            mock_make_client.return_value = client
            result = _classify_gig_type("Funeral Service")
        assert result == "Funeral"

    def test_cache_prevents_repeated_api_calls(self):
        """Calling _classify_gig_type twice with the same header calls make_client once."""
        with patch("organist_bot.analytics.make_client") as mock_make_client:
            mock_make_client.return_value = _fake_choice_client("wedding")
            _classify_gig_type("Wedding at St Paul's")
            _classify_gig_type("Wedding at St Paul's")  # should hit cache
        assert mock_make_client.call_count == 1

    def test_uses_configured_typesafe_api_key(self):
        with (
            patch("organist_bot.analytics.make_client") as mock_make_client,
            patch("organist_bot.analytics.settings") as mock_settings,
        ):
            mock_settings.typesafe_api_key = "ts-analytics-key"
            mock_make_client.return_value = _fake_choice_client("funeral")
            _classify_gig_type("Funeral Service")
        mock_make_client.assert_called_once_with(api_key="ts-analytics-key")


@pytest.mark.live
class TestClassifyGigTypeLive:
    """Opt-in: makes a real call to the TypeSafe API. Run with `pytest -m live`."""

    def setup_method(self):
        _classify_gig_type.cache_clear()

    def test_wedding_header_against_real_api(self):
        assert _classify_gig_type("Wedding at St Mary's Church") == "Wedding"

    def test_funeral_header_against_real_api(self):
        assert _classify_gig_type("Funeral Service — organist required") == "Funeral"
