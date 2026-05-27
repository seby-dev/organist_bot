from unittest.mock import patch


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
