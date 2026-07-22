"""Shared pytest fixtures for the organist_bot test suite."""

import pytest


@pytest.fixture(autouse=True)
def _isolate_scheduler_lock_file(monkeypatch, tmp_path):
    """Point main.main()'s scheduler lock at a tmp_path, not the real shared file.

    main._LOCK_FILE defaults to /tmp/organistbot_scheduler.lock — the exact path
    the live, currently-running production scheduler (launchd job
    com.organistbot.scheduler) locks every tick. Any test that calls
    main_module.main(...) without this fixture takes a real fcntl.flock() on
    that same file, which intermittently collides with the live process and
    fails the test with unrelated "Previous run still in progress" behavior —
    not a bug in the code under test, just two processes fighting over one path.

    main() reads main._LOCK_FILE live inside its body (not as a bound default
    argument), so patching the module attribute here isolates every call site
    across the whole suite without threading a lock_file= override through each one.
    """
    monkeypatch.setattr("main._LOCK_FILE", str(tmp_path / "scheduler.lock"))


@pytest.fixture(autouse=True)
def _silence_telegram_alerts(monkeypatch):
    """Stop the test suite from sending REAL Telegram alerts.

    Many error-path tests deliberately exercise corrupt-data / API-failure code
    paths that call ``alert.send_alert(...)`` (e.g. atomic_store corrupt-read,
    SheetsLogger batch-append failure, CalendarFilter/PostcodeFilter API errors).
    With a real ``TELEGRAM_BOT_TOKEN`` in the dev ``.env`` those calls POST to the
    live chat on every run — and the edit-triggered test hook turns that into a
    flood during development.

    Every PRODUCTION module in ``organist_bot/`` accesses the alert function as
    ``alert.send_alert`` (module-attribute access), so patching the single source
    ``organist_bot.alert.send_alert`` neutralises all of them. Tests that assert an
    alert *was* sent re-patch ``send_alert`` locally with a capturing callable;
    that patch runs after this autouse fixture and so takes precedence.

    Exception: ``tests/test_alert.py`` deliberately imports ``send_alert`` directly
    to test the real function, so it bypasses this patch — that file mocks
    ``_requests.post`` itself (and has a class-scoped safety net) so it never POSTs.
    """
    monkeypatch.setattr("organist_bot.alert.send_alert", lambda *args, **kwargs: None)
