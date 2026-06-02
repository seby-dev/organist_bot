"""Shared pytest fixtures for the organist_bot test suite."""

import pytest


@pytest.fixture(autouse=True)
def _silence_telegram_alerts(monkeypatch):
    """Stop the test suite from sending REAL Telegram alerts.

    Many error-path tests deliberately exercise corrupt-data / API-failure code
    paths that call ``alert.send_alert(...)`` (e.g. atomic_store corrupt-read,
    SheetsLogger batch-append failure, CalendarFilter/PostcodeFilter API errors).
    With a real ``TELEGRAM_BOT_TOKEN`` in the dev ``.env`` those calls POST to the
    live chat on every run — and the edit-triggered test hook turns that into a
    flood during development.

    Every module accesses the alert function as ``alert.send_alert`` (module-
    attribute access, never ``from organist_bot.alert import send_alert``), so
    patching the single source ``organist_bot.alert.send_alert`` neutralises all
    of them. Tests that assert an alert *was* sent re-patch ``send_alert`` locally
    with a capturing callable; that patch runs after this autouse fixture and so
    takes precedence within those tests.
    """
    monkeypatch.setattr("organist_bot.alert.send_alert", lambda *args, **kwargs: None)
