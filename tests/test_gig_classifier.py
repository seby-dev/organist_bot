from unittest.mock import MagicMock, patch

import pytest

from organist_bot.gig_classifier import Classification, classify_gig
from organist_bot.models import Gig


def _make_gig(header="Sunday Service", musical_requirements=None, time="10:00 AM", fee="£120"):
    return Gig(
        header=header,
        organisation="St Mary's",
        locality="London",
        date="Sunday, July 12, 2026",
        time=time,
        fee=fee,
        link="https://e.com/1",
        musical_requirements=musical_requirements,
    )


def _mock_response(choice: str, confidence: float = 0.95, status_ok: bool = True):
    resp = MagicMock()
    resp.raise_for_status = (
        MagicMock() if status_ok else MagicMock(side_effect=RuntimeError("HTTP error"))
    )
    resp.json.return_value = {
        "answers": {
            "eligibility": {
                "type": "choice",
                "choice": choice,
                "confidence": confidence,
            }
        }
    }
    return resp


class TestClassifyGig:
    def test_auto_eligible_high_confidence_maps_to_auto_send(self):
        with patch("organist_bot.gig_classifier.requests.post") as mock_post:
            mock_post.return_value = _mock_response("auto_eligible", confidence=0.95)
            result = classify_gig(_make_gig())
        assert result == Classification(
            decision="auto_send", reason="auto_eligible", confidence=0.95
        )

    def test_auto_eligible_low_confidence_maps_to_hold_for_review(self):
        with patch("organist_bot.gig_classifier.requests.post") as mock_post:
            mock_post.return_value = _mock_response("auto_eligible", confidence=0.6)
            result = classify_gig(_make_gig())
        assert result == Classification(
            decision="hold_for_review", reason="auto_eligible", confidence=0.6
        )

    def test_multi_service_maps_to_hold_for_review(self):
        with patch("organist_bot.gig_classifier.requests.post") as mock_post:
            mock_post.return_value = _mock_response("multi_service", confidence=0.9)
            result = classify_gig(_make_gig(time="9:00 AM & 6:00 PM"))
        assert result == Classification(
            decision="hold_for_review", reason="multi_service", confidence=0.9
        )

    def test_other_service_type_maps_to_hold_for_review(self):
        with patch("organist_bot.gig_classifier.requests.post") as mock_post:
            mock_post.return_value = _mock_response("other_service_type", confidence=0.9)
            result = classify_gig(_make_gig(header="Evensong"))
        assert result == Classification(
            decision="hold_for_review", reason="other_service_type", confidence=0.9
        )

    def test_unexpected_choice_normalises_to_hold_for_review(self):
        with patch("organist_bot.gig_classifier.requests.post") as mock_post:
            mock_post.return_value = _mock_response("banana", confidence=0.9)
            result = classify_gig(_make_gig())
        assert result.decision == "hold_for_review"
        assert result.reason == "other_service_type"

    def test_api_exception_returns_hold_for_review_without_raising(self):
        with patch("organist_bot.gig_classifier.requests.post") as mock_post:
            mock_post.side_effect = RuntimeError("network down")
            result = classify_gig(_make_gig())  # must not raise
        assert result.decision == "hold_for_review"

    def test_http_error_returns_hold_for_review_without_raising(self):
        with patch("organist_bot.gig_classifier.requests.post") as mock_post:
            mock_post.return_value = _mock_response("auto_eligible", status_ok=False)
            result = classify_gig(_make_gig())  # must not raise
        assert result.decision == "hold_for_review"

    def test_malformed_response_returns_hold_for_review_without_raising(self):
        with patch("organist_bot.gig_classifier.requests.post") as mock_post:
            resp = MagicMock()
            resp.raise_for_status = MagicMock()
            resp.json.return_value = {"answers": {}}  # missing "eligibility" key
            mock_post.return_value = resp
            result = classify_gig(_make_gig())  # must not raise
        assert result.decision == "hold_for_review"

    def test_state_includes_header_musical_requirements_date_time_and_fee(self):
        with patch("organist_bot.gig_classifier.requests.post") as mock_post:
            mock_post.return_value = _mock_response("auto_eligible")
            classify_gig(
                _make_gig(
                    header="Wedding at St Mary's",
                    musical_requirements="Traditional hymns",
                    time="8:15 AM & 6:45 PM",
                    fee="£80 per service",
                )
            )
        state = mock_post.call_args.kwargs["json"]["state"]
        assert state["header"] == "Wedding at St Mary's"
        assert state["musical_requirements"] == "Traditional hymns"
        assert state["date"] == "Sunday, July 12, 2026"
        assert state["time"] == "8:15 AM & 6:45 PM"
        assert state["fee"] == "£80 per service"

    def test_uses_jev_latest_model_and_bearer_auth(self):
        with patch("organist_bot.gig_classifier.requests.post") as mock_post:
            mock_post.return_value = _mock_response("auto_eligible")
            classify_gig(_make_gig())
        call = mock_post.call_args
        assert call.kwargs["json"]["model"] == "jev-latest"
        assert call.kwargs["headers"]["Authorization"].startswith("Bearer ")


@pytest.mark.live
class TestClassifyGigLive:
    """Opt-in: makes a real call to the TypeSafe API. Run with `pytest -m live`."""

    def test_auto_eligible_gig_against_real_api(self):
        result = classify_gig(_make_gig())
        assert result.reason == "auto_eligible"
        assert result.decision == "auto_send"
        assert result.confidence >= 0.85

    def test_multi_service_gig_against_real_api(self):
        result = classify_gig(_make_gig(time="9:00 AM & 6:00 PM", fee="£100 per service"))
        assert result.reason == "multi_service"
        assert result.decision == "hold_for_review"
