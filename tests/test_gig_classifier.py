from unittest.mock import MagicMock, patch

import anthropic

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


def _mock_response(text: str):
    resp = MagicMock()
    block = MagicMock(spec=anthropic.types.TextBlock)
    block.text = text
    resp.content = [block]
    return resp


class TestClassifyGig:
    def test_auto_eligible_maps_to_auto_send(self):
        with patch("organist_bot.gig_classifier.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = _mock_response("auto_eligible")
            result = classify_gig(_make_gig())
        assert result == Classification(decision="auto_send", reason="auto_eligible")

    def test_multi_service_maps_to_hold_for_review(self):
        with patch("organist_bot.gig_classifier.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = _mock_response("multi_service")
            result = classify_gig(_make_gig(time="9:00 AM & 6:00 PM"))
        assert result == Classification(decision="hold_for_review", reason="multi_service")

    def test_other_service_type_maps_to_hold_for_review(self):
        with patch("organist_bot.gig_classifier.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = _mock_response(
                "other_service_type"
            )
            result = classify_gig(_make_gig(header="Evensong"))
        assert result == Classification(decision="hold_for_review", reason="other_service_type")

    def test_unexpected_label_normalises_to_hold_for_review(self):
        with patch("organist_bot.gig_classifier.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = _mock_response("banana")
            result = classify_gig(_make_gig())
        assert result.decision == "hold_for_review"
        assert result.reason == "other_service_type"

    def test_whitespace_and_case_insensitive(self):
        with patch("organist_bot.gig_classifier.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = _mock_response(
                "  Auto_Eligible \n"
            )
            result = classify_gig(_make_gig())
        assert result.decision == "auto_send"

    def test_api_exception_returns_hold_for_review_without_raising(self):
        with patch("organist_bot.gig_classifier.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.side_effect = RuntimeError("network down")
            result = classify_gig(_make_gig())  # must not raise
        assert result.decision == "hold_for_review"

    def test_prompt_includes_header_musical_requirements_time_and_fee(self):
        # Use a time NOT equal to _CLASSIFY_PROMPT's own hard-coded example
        # ("9:00 AM & 6:00 PM") — reusing that exact string would make the
        # `time` assertion pass even if gig.time were never interpolated at
        # all, since it's already baked into the template text.
        with patch("organist_bot.gig_classifier.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = _mock_response("auto_eligible")
            classify_gig(
                _make_gig(
                    header="Wedding at St Mary's",
                    musical_requirements="Traditional hymns",
                    time="8:15 AM & 6:45 PM",
                    fee="£80 per service",
                )
            )
        call_kwargs = mock_cls.return_value.messages.create.call_args.kwargs
        prompt_text = call_kwargs["messages"][0]["content"]
        assert "Wedding at St Mary's" in prompt_text
        assert "Traditional hymns" in prompt_text
        assert "8:15 AM & 6:45 PM" in prompt_text
        assert "£80 per service" in prompt_text

    def test_uses_fixed_haiku_model(self):
        with patch("organist_bot.gig_classifier.anthropic.Anthropic") as mock_cls:
            mock_cls.return_value.messages.create.return_value = _mock_response("auto_eligible")
            classify_gig(_make_gig())
        assert (
            mock_cls.return_value.messages.create.call_args.kwargs["model"]
            == "claude-haiku-4-5-20251001"
        )
