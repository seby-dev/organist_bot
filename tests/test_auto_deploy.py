"""Tests for scripts/auto_deploy.py's testable helper functions.

Importing scripts.auto_deploy must be side-effect-free — the actual deploy
flow lives in main(), guarded by `if __name__ == "__main__":`. These tests
would previously have triggered a real `git fetch` against the live repo
merely by importing the module.
"""

import os
import subprocess
from unittest.mock import MagicMock, patch

import pytest

import scripts.auto_deploy as ad


def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestRunChecks:
    def test_all_checks_pass_returns_none(self, tmp_path):
        with patch.object(ad, "run", return_value=_completed(0)) as mock_run:
            result = ad._run_checks(tmp_path)
        assert result is None
        assert mock_run.call_count == 4  # ruff check, ruff format --check, mypy, pytest

    def test_first_check_fails_short_circuits_and_reports_label(self, tmp_path):
        # ruff check fails; later checks must not run.
        with patch.object(
            ad, "run", side_effect=[_completed(1, stdout="E501 line too long")]
        ) as mock_run:
            result = ad._run_checks(tmp_path)
        assert result is not None
        assert "ruff check failed" in result
        assert "E501 line too long" in result
        assert mock_run.call_count == 1

    def test_later_check_failure_reports_correct_label(self, tmp_path):
        with patch.object(
            ad,
            "run",
            side_effect=[
                _completed(0),
                _completed(0),
                _completed(1, stderr="error: bad annotation"),
            ],
        ):
            result = ad._run_checks(tmp_path)
        assert result is not None
        assert "mypy failed" in result
        assert "bad annotation" in result

    def test_long_output_truncated_to_last_1500_chars(self, tmp_path):
        huge = "x" * 5000
        with patch.object(ad, "run", return_value=_completed(1, stdout=huge)):
            result = ad._run_checks(tmp_path)
        assert result is not None
        # label + truncated output should be well under the raw 5000 chars
        assert len(result) < 1600


class TestAlreadyAlerted:
    def test_no_file_means_not_alerted(self, tmp_path):
        assert ad._already_alerted("abc123", tmp_path / "missing.txt") is False

    def test_matching_sha_means_alerted(self, tmp_path):
        f = tmp_path / "failed.txt"
        f.write_text("abc123\n")
        assert ad._already_alerted("abc123", f) is True

    def test_different_sha_means_not_alerted(self, tmp_path):
        f = tmp_path / "failed.txt"
        f.write_text("abc123\n")
        assert ad._already_alerted("def456", f) is False


class TestWorkingTreeClean:
    @pytest.fixture(autouse=True)
    def _clear_git_env(self, monkeypatch):
        # Git hooks set GIT_DIR/GIT_WORK_TREE/GIT_INDEX_FILE in the process
        # environment. Those vars override directory detection and cause git
        # commands run in a freshly-inited tmp_path to target the wrong repo.
        # Strip them so _init_repo and _working_tree_clean see a clean slate.
        for key in [k for k in os.environ if k.startswith("GIT_")]:
            monkeypatch.delenv(key, raising=False)

    def _init_repo(self, tmp_path):
        subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
        (tmp_path / "file.txt").write_text("hello\n")
        subprocess.run(["git", "add", "file.txt"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-m", "init", "--quiet"], cwd=tmp_path, check=True)
        return tmp_path

    def test_clean_repo_returns_true(self, tmp_path):
        repo = self._init_repo(tmp_path)
        assert ad._working_tree_clean(repo) is True

    def test_modified_tracked_file_returns_false(self, tmp_path):
        repo = self._init_repo(tmp_path)
        (repo / "file.txt").write_text("changed\n")
        assert ad._working_tree_clean(repo) is False

    def test_untracked_file_returns_false(self, tmp_path):
        repo = self._init_repo(tmp_path)
        (repo / "new_file.txt").write_text("new\n")
        assert ad._working_tree_clean(repo) is False

    def test_nonzero_exit_returns_false_even_with_empty_stdout(self, tmp_path):
        # Pass a path that is not a git repo so `git status` exits non-zero
        # with empty stdout — the function must return False, not True.
        not_a_repo = tmp_path / "not_a_repo"
        not_a_repo.mkdir()
        assert ad._working_tree_clean(not_a_repo) is False


class TestMainWrongBranchAlert:
    """main()'s "HEAD isn't on main" branch — the alert this fix adds."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ad, "REPO", tmp_path)
        monkeypatch.setattr(ad, "SHA_FILE", tmp_path / "last_deployed_sha.txt")
        monkeypatch.setattr(
            ad, "WRONG_BRANCH_SHA_FILE", tmp_path / "last_wrong_branch_alert_sha.txt"
        )

    def test_alerts_once_when_head_not_on_main(self):
        remote_sha = "a" * 40
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0),  # fetch
                    _completed(0, stdout=remote_sha),  # rev-parse origin/main
                    _completed(0, stdout="feature-branch"),  # rev-parse --abbrev-ref HEAD
                ],
            ),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        mock_alert.assert_called_once()
        message = mock_alert.call_args[0][0]
        assert "feature-branch" in message
        assert remote_sha[:8] in message
        assert ad.WRONG_BRANCH_SHA_FILE.read_text().strip() == remote_sha

    def test_does_not_realert_same_stuck_sha_on_next_tick(self):
        remote_sha = "b" * 40
        ad.WRONG_BRANCH_SHA_FILE.write_text(remote_sha + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0),
                    _completed(0, stdout=remote_sha),
                    _completed(0, stdout="feature-branch"),
                ],
            ),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        mock_alert.assert_not_called()

    def test_realerts_when_a_new_commit_lands_while_still_stuck(self):
        old_sha = "c" * 40
        new_sha = "d" * 40
        ad.WRONG_BRANCH_SHA_FILE.write_text(old_sha + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0),
                    _completed(0, stdout=new_sha),
                    _completed(0, stdout="feature-branch"),
                ],
            ),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        mock_alert.assert_called_once()
        assert ad.WRONG_BRANCH_SHA_FILE.read_text().strip() == new_sha

    def test_successful_deploy_clears_stale_wrong_branch_marker(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ad, "FAILED_SHA_FILE", tmp_path / "last_failed_deploy_sha.txt")
        monkeypatch.setattr(ad, "PLISTS", [])
        remote_sha = "e" * 40
        # A stale marker left over from an earlier stuck-branch period.
        ad.WRONG_BRANCH_SHA_FILE.write_text(("f" * 40) + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0),  # fetch
                    _completed(0, stdout=remote_sha),  # rev-parse origin/main
                    _completed(0, stdout="main"),  # rev-parse --abbrev-ref HEAD
                    _completed(0),  # merge --ff-only
                    _completed(0),  # uv sync
                ],
            ),
            patch.object(ad, "_run_checks", return_value=None),
        ):
            ad.main()
        assert not ad.WRONG_BRANCH_SHA_FILE.exists()
        assert ad.SHA_FILE.read_text().strip() == remote_sha


class TestSendAlert:
    def test_posts_when_configured(self, tmp_path):
        (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=123\n")
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(ok=True)
            ad._send_alert("test message", tmp_path)
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "abc" in args[0]
        assert kwargs["json"]["chat_id"] == "123"
        assert kwargs["json"]["text"] == "test message"

    def test_noop_when_not_configured(self, tmp_path):
        (tmp_path / ".env").write_text("SOME_OTHER_VAR=x\n")
        with patch("requests.post") as mock_post:
            ad._send_alert("test message", tmp_path)
        mock_post.assert_not_called()

    def test_never_raises_when_post_fails(self, tmp_path, capsys):
        (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=123\n")
        with patch("requests.post", side_effect=Exception("network down")):
            ad._send_alert("test message", tmp_path)  # must not raise
        assert "alert failed" in capsys.readouterr().out
