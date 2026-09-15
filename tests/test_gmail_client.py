import base64
from unittest.mock import MagicMock, patch

import pytest
from googleapiclient.errors import HttpError


def _make_message_dict(msg_id, sender, recipient, body, direction):
    return {
        "message_id": msg_id,
        "sender": sender,
        "recipient": recipient,
        "body": body,
        "direction": direction,
    }


class TestFetchReplyMessages:
    def _make_client(self, tmp_path):
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text('{"installed": {}}')
        token_file = tmp_path / "token.json"
        from organist_bot.integrations.gmail_client import GmailClient

        return GmailClient(str(creds_file), str(token_file))

    def test_returns_inbox_messages_from_church_emails(self, tmp_path):
        client = self._make_client(tmp_path)
        expected = _make_message_dict(
            "msg1", "church@example.com", "me@example.com", "We'd love to have you", "incoming"
        )
        with (
            patch.object(client, "_build_service"),
            patch.object(client, "_search_messages", return_value=[{"id": "msg1"}]),
            patch.object(client, "_get_message_details", return_value=expected),
        ):
            result = client.fetch_reply_messages(
                applied_emails=["church@example.com"],
                accepted_emails=[],
            )
        assert len(result) == 1
        assert result[0]["message_id"] == "msg1"
        assert result[0]["direction"] == "incoming"

    def test_returns_sent_messages_to_accepted_record_emails(self, tmp_path):
        client = self._make_client(tmp_path)
        expected = _make_message_dict(
            "msg2",
            "me@example.com",
            "accepted_church@example.com",
            "I need to cancel",
            "outgoing",
        )

        def search_side_effect(service, query):
            # Only return results for the sent-folder query
            return [{"id": "msg2"}] if "in:sent" in query else []

        with (
            patch.object(client, "_build_service"),
            patch.object(client, "_search_messages", side_effect=search_side_effect),
            patch.object(client, "_get_message_details", return_value=expected),
        ):
            result = client.fetch_reply_messages(
                applied_emails=[],
                accepted_emails=["accepted_church@example.com"],
            )
        assert len(result) == 1
        assert result[0]["direction"] == "outgoing"

    def test_does_not_search_sent_for_applied_only_emails(self, tmp_path):
        client = self._make_client(tmp_path)
        with (
            patch.object(client, "_build_service"),
            patch.object(client, "_search_messages", return_value=[]) as mock_search,
        ):
            client.fetch_reply_messages(
                applied_emails=["applied_only@example.com"],
                accepted_emails=[],
            )
        for call_args in mock_search.call_args_list:
            query = call_args[0][1]
            assert "in:sent" not in query, "should not search sent folder for applied-only emails"

    def test_fails_open_on_api_error(self, tmp_path):
        client = self._make_client(tmp_path)
        with patch.object(client, "_build_service", side_effect=Exception("API down")):
            result = client.fetch_reply_messages(
                applied_emails=["church@example.com"],
                accepted_emails=[],
            )
        assert result == []


class TestFetchInvoiceReplies:
    def _make_client(self, tmp_path):
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text('{"installed": {}}')
        token_file = tmp_path / "token.json"
        from organist_bot.integrations.gmail_client import GmailClient

        return GmailClient(str(creds_file), str(token_file))

    def test_returns_inbox_replies_from_client_email(self, tmp_path):
        client = self._make_client(tmp_path)
        expected = _make_message_dict(
            "msg1",
            "client@example.com",
            "me@example.com",
            "Thank you, payment has been sent.",
            "incoming",
        )
        with (
            patch.object(client, "_build_service"),
            patch.object(client, "_search_messages", return_value=[{"id": "msg1"}]),
            patch.object(client, "_get_message_details", return_value=expected),
        ):
            result = client.fetch_invoice_replies(
                invoice_number="INV-2026-001",
                client_email="client@example.com",
            )
        assert len(result) == 1
        assert result[0]["message_id"] == "msg1"

    def test_search_query_includes_invoice_number_and_client_email(self, tmp_path):
        client = self._make_client(tmp_path)
        captured_queries = []

        def capture_search(service, query):
            captured_queries.append(query)
            return []

        with (
            patch.object(client, "_build_service"),
            patch.object(client, "_search_messages", side_effect=capture_search),
        ):
            client.fetch_invoice_replies(
                invoice_number="INV-2026-001",
                client_email="client@example.com",
            )

        assert any("INV-2026-001" in q for q in captured_queries)
        assert any("client@example.com" in q for q in captured_queries)

    def test_since_date_appended_to_query(self, tmp_path):
        client = self._make_client(tmp_path)
        captured_queries = []

        def capture_search(service, query):
            captured_queries.append(query)
            return []

        with (
            patch.object(client, "_build_service"),
            patch.object(client, "_search_messages", side_effect=capture_search),
        ):
            client.fetch_invoice_replies(
                invoice_number="INV-2026-001",
                client_email="client@example.com",
                since_date="2026/06/01",
            )

        assert any("2026/06/01" in q for q in captured_queries)

    def test_returns_empty_list_on_api_error(self, tmp_path):
        client = self._make_client(tmp_path)
        with (
            patch.object(client, "_build_service", side_effect=Exception("auth error")),
        ):
            result = client.fetch_invoice_replies("INV-2026-001", "client@example.com")
        assert result == []


class TestCreateDraft:
    def _make_client(self, tmp_path):
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text('{"installed": {}}')
        token_file = tmp_path / "token.json"
        from organist_bot.integrations.gmail_client import GmailClient

        return GmailClient(str(creds_file), str(token_file))

    def test_builds_mime_message_and_returns_new_draft_id(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_service = MagicMock()
        mock_service.users().drafts().create().execute.return_value = {"id": "draft123"}
        with patch.object(client, "_get_service", return_value=mock_service):
            draft_id = client.create_draft(
                sender="bot@test.com",
                recipient="church@test.com",
                cc=["cc@test.com"],
                subject="Test Subject",
                body_html="<p>Body</p>",
            )
        assert draft_id == "draft123"
        create_mock = mock_service.users().drafts().create
        _, kwargs = create_mock.call_args
        assert kwargs["userId"] == "me"
        raw = kwargs["body"]["message"]["raw"]
        decoded = base64.urlsafe_b64decode(raw).decode()
        assert "Test Subject" in decoded
        assert "church@test.com" in decoded
        assert "cc@test.com" in decoded
        assert "<p>Body</p>" in decoded

    def test_omits_cc_header_when_cc_is_none(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_service = MagicMock()
        mock_service.users().drafts().create().execute.return_value = {"id": "draft456"}
        with patch.object(client, "_get_service", return_value=mock_service):
            client.create_draft(
                sender="bot@test.com",
                recipient="church@test.com",
                cc=None,
                subject="No CC",
                body_html="<p>Body</p>",
            )
        create_mock = mock_service.users().drafts().create
        raw = create_mock.call_args.kwargs["body"]["message"]["raw"]
        decoded = base64.urlsafe_b64decode(raw).decode()
        assert "Cc:" not in decoded


class TestSendDraft:
    def _make_client(self, tmp_path):
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text('{"installed": {}}')
        token_file = tmp_path / "token.json"
        from organist_bot.integrations.gmail_client import GmailClient

        return GmailClient(str(creds_file), str(token_file))

    def test_calls_drafts_send_with_draft_id(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_service = MagicMock()
        with patch.object(client, "_get_service", return_value=mock_service):
            client.send_draft("draft123")
        send_mock = mock_service.users().drafts().send
        _, kwargs = send_mock.call_args
        assert kwargs["userId"] == "me"
        assert kwargs["body"] == {"id": "draft123"}
        mock_service.users().drafts().send().execute.assert_called()

    def test_propagates_api_errors(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_service = MagicMock()
        mock_service.users().drafts().send().execute.side_effect = RuntimeError("boom")
        with patch.object(client, "_get_service", return_value=mock_service):
            with pytest.raises(RuntimeError, match="boom"):
                client.send_draft("draft123")


class TestDeleteDraft:
    def _make_client(self, tmp_path):
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text('{"installed": {}}')
        token_file = tmp_path / "token.json"
        from organist_bot.integrations.gmail_client import GmailClient

        return GmailClient(str(creds_file), str(token_file))

    def test_calls_drafts_delete_with_draft_id(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_service = MagicMock()
        with patch.object(client, "_get_service", return_value=mock_service):
            client.delete_draft("draft123")
        delete_mock = mock_service.users().drafts().delete
        _, kwargs = delete_mock.call_args
        assert kwargs["userId"] == "me"
        assert kwargs["id"] == "draft123"


class TestHasComposeAccess:
    def _make_client(self, tmp_path):
        creds_file = tmp_path / "credentials.json"
        creds_file.write_text('{"installed": {}}')
        token_file = tmp_path / "token.json"
        from organist_bot.integrations.gmail_client import GmailClient

        return GmailClient(str(creds_file), str(token_file))

    def test_true_when_create_and_delete_round_trip_succeeds(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_service = MagicMock()
        mock_service.users().drafts().create().execute.return_value = {"id": "scope-check-1"}
        with patch.object(client, "_get_service", return_value=mock_service):
            assert client.has_compose_access() is True
        delete_mock = mock_service.users().drafts().delete
        assert delete_mock.call_args.kwargs["id"] == "scope-check-1"

    def test_false_when_create_raises(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_service = MagicMock()
        mock_service.users().drafts().create().execute.side_effect = RuntimeError(
            "403 insufficient scope"
        )
        with patch.object(client, "_get_service", return_value=mock_service):
            assert client.has_compose_access() is False

    def test_does_not_raise_on_failure(self, tmp_path):
        client = self._make_client(tmp_path)
        mock_service = MagicMock()
        mock_service.users().drafts().create.side_effect = Exception("network down")
        with patch.object(client, "_get_service", return_value=mock_service):
            client.has_compose_access()  # must not raise


class TestIsNotFoundError:
    def test_true_for_real_http_404(self):
        from organist_bot.integrations.gmail_client import is_not_found_error

        resp = MagicMock()
        resp.status = 404
        exc = HttpError(resp, b"not found")
        assert is_not_found_error(exc) is True

    def test_false_for_real_http_500(self):
        from organist_bot.integrations.gmail_client import is_not_found_error

        resp = MagicMock()
        resp.status = 500
        exc = HttpError(resp, b"server error")
        assert is_not_found_error(exc) is False

    def test_true_for_fake_not_found_error(self):
        from organist_bot.integrations.gmail_client import (
            GmailNotFoundError,
            is_not_found_error,
        )

        assert is_not_found_error(GmailNotFoundError("gone")) is True

    def test_false_for_unrelated_exception(self):
        from organist_bot.integrations.gmail_client import is_not_found_error

        assert is_not_found_error(ValueError("nope")) is False


class TestFakeGmailClient:
    def test_create_draft_records_call_and_returns_unique_ids(self):
        from organist_bot.integrations.gmail_client import FakeGmailClient

        fake = FakeGmailClient()
        id1 = fake.create_draft(
            sender="a@test.com", recipient="b@test.com", cc=None, subject="S1", body_html="B1"
        )
        id2 = fake.create_draft(
            sender="a@test.com", recipient="c@test.com", cc=None, subject="S2", body_html="B2"
        )
        assert id1 != id2
        assert len(fake.created) == 2
        assert fake.created[0]["subject"] == "S1"

    def test_send_and_delete_record_ids(self):
        from organist_bot.integrations.gmail_client import FakeGmailClient

        fake = FakeGmailClient()
        draft_id = fake.create_draft(
            sender="a@test.com", recipient="b@test.com", cc=None, subject="S", body_html="B"
        )
        fake.send_draft(draft_id)
        assert fake.sent == [draft_id]

    def test_simulate_not_found_raises_on_send_and_delete(self):
        from organist_bot.integrations.gmail_client import (
            FakeGmailClient,
            is_not_found_error,
        )

        fake = FakeGmailClient()
        draft_id = fake.create_draft(
            sender="a@test.com", recipient="b@test.com", cc=None, subject="S", body_html="B"
        )
        fake.simulate_not_found(draft_id)
        with pytest.raises(Exception) as exc_info:
            fake.send_draft(draft_id)
        assert is_not_found_error(exc_info.value) is True

    def test_has_compose_access_configurable(self):
        from organist_bot.integrations.gmail_client import FakeGmailClient

        assert FakeGmailClient(compose_access=True).has_compose_access() is True
        assert FakeGmailClient(compose_access=False).has_compose_access() is False
