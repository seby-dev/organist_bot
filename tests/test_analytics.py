"""Tests for organist_bot.analytics."""

from __future__ import annotations

from unittest.mock import patch

import organist_bot.analytics as analytics

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

    def test_keyword_matching(self):
        records = [
            _make_record("applied", _T0, _T0, header="Wedding at St Mary's"),
            _make_record("accepted", _T0, _T0, header="Funeral Service"),
            _make_record("applied", _T0, _T0, header="Organist Required"),
        ]
        with patch.object(analytics.application_store, "list_applications", return_value=records):
            b = analytics.get_gig_type_breakdown()
        assert "Wedding" in b
        assert "Funeral" in b
        assert "Other" in b

    def test_carol_beats_service_in_priority(self):
        records = [
            _make_record("applied", _T0, _T0, header="Christmas Carol Service"),
        ]
        with patch.object(analytics.application_store, "list_applications", return_value=records):
            b = analytics.get_gig_type_breakdown()
        assert "Carol Service" in b
        assert "Service" not in b

    def test_acceptance_rate_calculation(self):
        records = [
            _make_record("accepted", _T0, _T0, header="Wedding"),
            _make_record("accepted", _T0, _T0, header="Wedding"),
            _make_record("no_response", _T0, _T0, header="Wedding"),
            _make_record("no_response", _T0, _T0, header="Wedding"),
        ]
        with patch.object(analytics.application_store, "list_applications", return_value=records):
            b = analytics.get_gig_type_breakdown()
        assert b["Wedding"]["count"] == 4
        assert b["Wedding"]["accepted"] == 2
        assert b["Wedding"]["acceptance_rate"] == 50.0
