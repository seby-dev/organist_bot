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


class TestCheckStaleOrigin:
    """_check_stale_origin: purely read-only, best-effort visibility that
    origin/main has moved on without local main following -- since nothing
    auto-pulls from origin any more, a merged PR would otherwise reach
    origin/main and just sit there with no signal anywhere. Reads the
    cached remote-tracking ref only -- never fetches, never touches local
    main, never affects whether a deploy runs."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            ad, "STALE_ORIGIN_SHA_FILE", tmp_path / "last_stale_origin_alert_sha.txt"
        )

    def test_no_alert_when_origin_ref_is_missing(self):
        with (
            patch.object(
                ad, "run", return_value=_completed(128, stderr="fatal: bad revision")
            ) as mock_run,
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad._check_stale_origin()
        mock_run.assert_called_once()
        mock_alert.assert_not_called()

    def test_no_alert_when_local_main_is_up_to_date(self):
        origin_sha = "a" * 40
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout=origin_sha),  # rev-parse refs/remotes/origin/main
                    _completed(0, stdout="0"),  # rev-list --count
                ],
            ),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad._check_stale_origin()
        mock_alert.assert_not_called()

    def test_alerts_once_when_origin_is_ahead(self):
        origin_sha = "b" * 40
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout=origin_sha),
                    _completed(0, stdout="3"),
                ],
            ),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad._check_stale_origin()
        mock_alert.assert_called_once()
        message = mock_alert.call_args[0][0]
        assert "3" in message
        assert origin_sha[:8] in message
        assert ad.STALE_ORIGIN_SHA_FILE.read_text().strip() == origin_sha

    def test_does_not_realert_same_stuck_origin_sha(self):
        origin_sha = "c" * 40
        ad.STALE_ORIGIN_SHA_FILE.write_text(origin_sha + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout=origin_sha),
                    _completed(0, stdout="1"),
                ],
            ),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad._check_stale_origin()
        mock_alert.assert_not_called()

    def test_realerts_when_origin_advances_further(self):
        old_sha = "d" * 40
        new_sha = "e" * 40
        ad.STALE_ORIGIN_SHA_FILE.write_text(old_sha + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout=new_sha),
                    _completed(0, stdout="2"),
                ],
            ),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad._check_stale_origin()
        mock_alert.assert_called_once()
        assert ad.STALE_ORIGIN_SHA_FILE.read_text().strip() == new_sha

    def test_clears_marker_once_local_main_catches_up(self):
        ad.STALE_ORIGIN_SHA_FILE.write_text(("f" * 40) + "\n")
        with patch.object(
            ad,
            "run",
            side_effect=[
                _completed(0, stdout="g" * 40),
                _completed(0, stdout="0"),
            ],
        ):
            ad._check_stale_origin()
        assert not ad.STALE_ORIGIN_SHA_FILE.exists()

    def test_never_fetches_or_merges(self):
        with patch.object(
            ad,
            "run",
            side_effect=[
                _completed(0, stdout="h" * 40),
                _completed(0, stdout="0"),
            ],
        ) as mock_run:
            ad._check_stale_origin()
        for call in mock_run.call_args_list:
            cmd = call.args[0]
            assert "fetch" not in cmd
            assert "merge" not in cmd

    def test_main_calls_it_even_when_local_main_is_already_deployed(self, tmp_path, monkeypatch):
        """This is exactly the case the whole feature exists for: nothing
        new to deploy locally, but origin/main has moved on -- main() must
        still surface that instead of returning silently."""
        monkeypatch.setattr(ad, "REPO", tmp_path)
        monkeypatch.setattr(ad, "SHA_FILE", tmp_path / "last_deployed_sha.txt")
        sha = "i" * 40
        ad.SHA_FILE.write_text(sha + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),  # rev-parse --abbrev-ref HEAD
                    _completed(0, stdout=sha),  # rev-parse --verify refs/heads/main
                ],
            ),
            patch.object(ad, "_check_stale_origin") as mock_check,
        ):
            ad.main()
        mock_check.assert_called_once()


class TestMainWrongBranchAlert:
    """main()'s "HEAD isn't on main" branch — checked against local main's
    own tip (`git rev-parse main`), never origin/main."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ad, "REPO", tmp_path)
        monkeypatch.setattr(ad, "SHA_FILE", tmp_path / "last_deployed_sha.txt")
        monkeypatch.setattr(
            ad, "WRONG_BRANCH_SHA_FILE", tmp_path / "last_wrong_branch_alert_sha.txt"
        )
        # Redirect every other *_SHA_FILE constant main() might touch too --
        # a test in this class that reaches past the branch check (e.g. the
        # successful-deploy one) would otherwise read/write/delete these in
        # the real ~/Developer/organist_bot/data/ directory.
        monkeypatch.setattr(ad, "DIRTY_TREE_SHA_FILE", tmp_path / "last_dirty_tree_alert_sha.txt")
        monkeypatch.setattr(
            ad, "UV_SYNC_FAILED_SHA_FILE", tmp_path / "last_uv_sync_failed_alert_sha.txt"
        )
        # _check_stale_origin is covered by its own TestCheckStaleOrigin
        # class -- neutralize it here so it doesn't consume the `run` mock
        # sequences these tests set up for the rest of main().
        monkeypatch.setattr(ad, "_check_stale_origin", lambda: None)

    def test_alerts_once_when_head_not_on_main(self):
        main_sha = "a" * 40
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="feature-branch"),  # rev-parse --abbrev-ref HEAD
                    _completed(0, stdout=main_sha),  # rev-parse main
                ],
            ),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        mock_alert.assert_called_once()
        message = mock_alert.call_args[0][0]
        assert "feature-branch" in message
        assert main_sha[:8] in message
        assert ad.WRONG_BRANCH_SHA_FILE.read_text().strip() == main_sha

    def test_does_not_realert_same_stuck_sha_on_next_tick(self):
        main_sha = "b" * 40
        ad.WRONG_BRANCH_SHA_FILE.write_text(main_sha + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="feature-branch"),
                    _completed(0, stdout=main_sha),
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
                    _completed(0, stdout="feature-branch"),
                    _completed(0, stdout=new_sha),
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
        main_sha = "e" * 40
        # A stale marker left over from an earlier stuck-branch period.
        ad.WRONG_BRANCH_SHA_FILE.write_text(("f" * 40) + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),  # rev-parse --abbrev-ref HEAD
                    _completed(0, stdout=main_sha),  # rev-parse --verify refs/heads/main
                    _completed(0),  # uv sync
                ],
            ),
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_run_checks", return_value=None),
        ):
            ad.main()
        assert not ad.WRONG_BRANCH_SHA_FILE.exists()
        assert ad.SHA_FILE.read_text().strip() == main_sha


class TestMainDeployTrigger:
    """main()'s core trigger: a deploy fires off local main's own HEAD
    advancing, never by fetching or merging from origin — local main only
    ever moves via something else (a manual `git pull`/`git merge`, `gh pr
    merge` run in this checkout, ...), and this script only reacts to it."""

    @pytest.fixture(autouse=True)
    def _isolate(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ad, "REPO", tmp_path)
        monkeypatch.setattr(ad, "SHA_FILE", tmp_path / "last_deployed_sha.txt")
        monkeypatch.setattr(ad, "FAILED_SHA_FILE", tmp_path / "last_failed_deploy_sha.txt")
        monkeypatch.setattr(
            ad, "WRONG_BRANCH_SHA_FILE", tmp_path / "last_wrong_branch_alert_sha.txt"
        )
        monkeypatch.setattr(ad, "DIRTY_TREE_SHA_FILE", tmp_path / "last_dirty_tree_alert_sha.txt")
        monkeypatch.setattr(
            ad, "UV_SYNC_FAILED_SHA_FILE", tmp_path / "last_uv_sync_failed_alert_sha.txt"
        )
        monkeypatch.setattr(ad, "PLISTS", [])
        # _check_stale_origin is covered by its own TestCheckStaleOrigin
        # class -- neutralize it here so it doesn't consume the `run` mock
        # sequences these tests set up for the rest of main().
        monkeypatch.setattr(ad, "_check_stale_origin", lambda: None)

    def test_no_deploy_when_local_main_matches_last_deployed(self):
        sha = "a" * 40
        ad.SHA_FILE.write_text(sha + "\n")
        with patch.object(
            ad,
            "run",
            side_effect=[
                _completed(0, stdout="main"),  # rev-parse --abbrev-ref HEAD
                _completed(0, stdout=sha),  # rev-parse --verify refs/heads/main
            ],
        ) as mock_run:
            ad.main()
        assert mock_run.call_count == 2  # never reaches uv sync / checks

    def test_already_deployed_sha_never_alerts_wrong_branch_during_a_rebase(self):
        """The actual invariant behind checking main_sha == last_deployed
        BEFORE branch != "main": a `main` ref mid-rebase (or any other
        in-progress git operation) doesn't move until it completes, so a
        rebase's detached HEAD (branch here would read something other
        than "main") must never be mistaken for "checkout is on the wrong
        branch" when there's nothing new to deploy anyway. Pinning this
        via the actual comparison, not subprocess call order -- swapping
        the two checks in main() would make this test fail correctly."""
        sha = "z" * 40
        ad.SHA_FILE.write_text(sha + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="HEAD"),  # detached, mid-rebase
                    _completed(0, stdout=sha),
                ],
            ),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        mock_alert.assert_not_called()
        assert not ad.WRONG_BRANCH_SHA_FILE.exists()

    def test_no_deploy_when_local_main_ref_is_unresolvable(self):
        """`git rev-parse --verify refs/heads/main` failing (e.g. a corrupt
        or mid-operation repo) must exit cleanly, matching the old fetch
        guard's "exit cleanly when the repo can't be read" behavior."""
        with patch.object(
            ad,
            "run",
            side_effect=[
                _completed(0, stdout="main"),
                _completed(128, stdout="", stderr="fatal: bad revision"),
            ],
        ) as mock_run:
            ad.main()
        assert mock_run.call_count == 2
        assert not ad.SHA_FILE.exists()

    def test_never_fetches_or_merges_from_origin(self):
        sha = "b" * 40
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                    _completed(0),  # uv sync
                ],
            ) as mock_run,
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_run_checks", return_value=None),
        ):
            ad.main()
        for call in mock_run.call_args_list:
            cmd = call.args[0]
            assert "fetch" not in cmd
            assert "merge" not in cmd
            assert not any("origin" in str(part) for part in cmd)

    def test_deploys_when_local_main_advances(self):
        sha = "c" * 40
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                    _completed(0),  # uv sync
                ],
            ),
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_run_checks", return_value=None),
        ):
            ad.main()
        assert ad.SHA_FILE.read_text().strip() == sha

    def test_deploy_restarts_both_bot_plists_before_recording_success(self, tmp_path, monkeypatch):
        """The whole point of this script -- a regression that dropped the
        restart, or reordered it after the SHA_FILE write, would leave every
        other test in this file green while deploys silently stopped
        actually restarting anything."""
        sha = "g" * 40
        fake_plists = [tmp_path / "scheduler.plist", tmp_path / "telegram.plist"]
        monkeypatch.setattr(ad, "PLISTS", fake_plists)
        calls: list[str] = []  # interleaves "launchctl:..." and "sha_write" markers

        class _RecordingShaFile:
            """Path.write_text can't be patch.object'd directly (PosixPath
            attributes are read-only) -- stand in for SHA_FILE entirely so
            the write shows up in the same ordered `calls` list as the
            launchctl invocations."""

            def __init__(self, real_path):
                self._real = real_path

            def exists(self):
                return self._real.exists()

            def read_text(self):
                return self._real.read_text()

            def write_text(self, text):
                calls.append("sha_write")
                return self._real.write_text(text)

        real_sha_file = ad.SHA_FILE
        monkeypatch.setattr(ad, "SHA_FILE", _RecordingShaFile(real_sha_file))

        def _record(cmd, **kwargs):
            if cmd[:2] == ["git", "-C"] and cmd[3:5] == ["rev-parse", "--abbrev-ref"]:
                return _completed(0, stdout="main")
            if cmd[3:6] == ["rev-parse", "--verify", "refs/heads/main"]:
                return _completed(0, stdout=sha)
            if cmd[0] == "launchctl":
                calls.append(f"launchctl:{cmd[1]}:{cmd[3]}")
            return _completed(0)

        with (
            patch.object(ad, "run", side_effect=_record),
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_run_checks", return_value=None),
        ):
            ad.main()

        launchctl_calls = [c for c in calls if c.startswith("launchctl:")]
        assert len(launchctl_calls) == 4  # bootout + bootstrap, per plist
        for plist in fake_plists:
            assert f"launchctl:bootout:{plist}" in launchctl_calls
            assert f"launchctl:bootstrap:{plist}" in launchctl_calls
        assert real_sha_file.read_text().strip() == sha
        # The restart must precede the write that marks this SHA deployed --
        # not the other way around.
        assert calls.index("sha_write") == len(calls) - 1

    def test_uv_sync_failure_alerts_once_but_does_not_block_retry(self):
        """Unlike a deterministic check-gate failure, `uv sync` failing is
        usually environmental (network blip, registry hiccup) -- it must
        not write FAILED_SHA_FILE (that would trip the "already failed,
        roll back" path on this exact commit forever) and a later tick must
        still retry it for real, not just skip straight past."""
        sha = "h" * 40
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                    _completed(1, stderr="network unreachable"),  # uv sync
                ],
            ),
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        mock_alert.assert_called_once()
        assert ad.UV_SYNC_FAILED_SHA_FILE.read_text().strip() == sha
        assert not ad.FAILED_SHA_FILE.exists()
        assert not ad.SHA_FILE.exists()

        # Next tick: uv sync succeeds this time -- must actually retry, not
        # short-circuit past it, and the transient marker must clear.
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                    _completed(0),  # uv sync succeeds
                ],
            ),
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_run_checks", return_value=None),
        ):
            ad.main()
        assert ad.SHA_FILE.read_text().strip() == sha
        assert not ad.UV_SYNC_FAILED_SHA_FILE.exists()

    def test_uv_sync_failure_does_not_realert_same_stuck_sha(self):
        sha = "p" * 40
        ad.UV_SYNC_FAILED_SHA_FILE.write_text(sha + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                    _completed(1, stderr="still unreachable"),
                ],
            ),
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        mock_alert.assert_not_called()

    def test_check_failure_rolls_back_but_saves_a_rescue_branch(self):
        """Both bot launchd jobs set KeepAlive, so a failed commit simply
        left checked out would be loaded anyway by the next unrelated
        restart with no gate at all -- the working tree must roll back to
        the last good deploy. But local main's HEAD here (unlike the old
        origin-driven fast-forward) can carry a commit nothing else has a
        copy of, so that commit must be saved to a real branch first -- and
        specifically BEFORE the reset moves main away from it, not just
        discarded or saved after the fact (too late)."""
        sha = "d" * 40
        last_good = "e" * 40
        ad.SHA_FILE.write_text(last_good + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                    _completed(0),  # uv sync
                    _completed(0),  # git branch --force autodeploy-failed-<sha> <sha>
                    _completed(0),  # git reset --hard <last_good>
                    _completed(0),  # rollback uv sync
                ],
            ) as mock_run,
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_run_checks", return_value="pytest failed:\n..."),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()

        cmds = [c.args[0] for c in mock_run.call_args_list]
        branch_idx = next(i for i, c in enumerate(cmds) if "branch" in c)
        reset_idx = next(i for i, c in enumerate(cmds) if "reset" in c)
        assert branch_idx < reset_idx

        branch_call = cmds[branch_idx]
        assert branch_call[-3:] == ["--force", f"autodeploy-failed-{sha[:8]}", sha]

        reset_call = cmds[reset_idx]
        assert reset_call[-2:] == ["--hard", last_good]

        assert ad.SHA_FILE.read_text().strip() == last_good  # unchanged
        mock_alert.assert_called_once()
        assert ad.FAILED_SHA_FILE.read_text().strip() == sha

    def test_rescue_branch_creation_failure_aborts_without_resetting(self):
        """If `git branch --force` itself fails for any reason, resetting
        anyway would be the exact data loss this mechanism exists to
        prevent -- it must abort loudly instead."""
        sha = "q" * 40
        last_good = "r" * 40
        ad.SHA_FILE.write_text(last_good + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                    _completed(0),  # uv sync
                    _completed(128, stderr="fatal: lock exists"),  # git branch fails
                ],
            ) as mock_run,
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_run_checks", return_value="mypy failed:\n..."),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        for call in mock_run.call_args_list:
            assert "reset" not in call.args[0]
        assert ad.SHA_FILE.read_text().strip() == last_good
        # Two alerts: one for the check-gate failure itself, one for the
        # rescue-branch failure that aborted the rollback.
        assert mock_alert.call_count == 2

    def test_rescue_reset_failure_alerts_and_does_not_claim_success(self):
        sha = "s" * 40
        last_good = "t" * 40
        ad.SHA_FILE.write_text(last_good + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                    _completed(0),  # uv sync
                    _completed(0),  # git branch succeeds
                    _completed(1, stderr="fatal: could not reset"),  # git reset fails
                ],
            ),
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_run_checks", return_value="ruff failed:\n..."),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        assert ad.SHA_FILE.read_text().strip() == last_good
        assert mock_alert.call_count == 2  # check-gate failure + reset failure

    def test_dirty_tree_during_check_run_aborts_rollback(self):
        """The tree was clean at the top of main() but went dirty during the
        minutes-long check run (e.g. the operator started fixing things the
        moment the "deploy blocked" alert landed) -- resetting now would
        discard those uncommitted edits, which the rescue branch can't
        protect (it only saves committed work)."""
        sha = "u" * 40
        last_good = "v" * 40
        ad.SHA_FILE.write_text(last_good + "\n")
        clean_results = iter([True, False])  # clean at top-of-main, dirty by rollback time
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                    _completed(0),  # uv sync
                ],
            ) as mock_run,
            patch.object(ad, "_working_tree_clean", side_effect=lambda repo: next(clean_results)),
            patch.object(ad, "_run_checks", return_value="pytest failed:\n..."),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        for call in mock_run.call_args_list:
            assert "branch" not in call.args[0]
            assert "reset" not in call.args[0]
        assert ad.SHA_FILE.read_text().strip() == last_good
        assert mock_alert.call_count == 2  # check-gate failure + dirty-tree-skip

    def test_already_failed_sha_re_pulled_gets_rolled_back_again(self):
        """A commit that already failed once, then got rolled back, then
        landed back on local main (e.g. re-pulled by an operator who
        followed the stale-origin alert without knowing it was already
        known-bad) must be rolled back again -- not just short-circuited
        past, which would leave it checked out under KeepAlive with no
        gate at all, silently reproducing the exact hazard this whole
        mechanism exists to prevent."""
        sha = "w" * 40
        last_good = "x" * 40
        ad.SHA_FILE.write_text(last_good + "\n")
        ad.FAILED_SHA_FILE.write_text(sha + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                    _completed(0),  # git branch --force autodeploy-failed-<sha> <sha>
                    _completed(0),  # git reset --hard <last_good>
                    _completed(0),  # rollback uv sync
                ],
            ) as mock_run,
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_run_checks") as mock_checks,
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        # The whole point of the short-circuit: the expensive re-run is
        # still skipped.
        mock_checks.assert_not_called()
        # No duplicate alert -- already alerted the first time this SHA
        # failed -- but the rollback itself still runs for real.
        mock_alert.assert_not_called()
        reset_calls = [c for c in mock_run.call_args_list if "reset" in c.args[0]]
        assert len(reset_calls) == 1
        assert reset_calls[0].args[0][-2:] == ["--hard", last_good]
        assert ad.SHA_FILE.read_text().strip() == last_good

    def test_check_failure_with_no_prior_deploy_leaves_commit_checked_out(self):
        """No last-good SHA to roll back to yet (a fresh checkout's very
        first deploy attempt) -- there's nothing safe to reset onto, so the
        failing commit is simply left in place."""
        sha = "i" * 40
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                    _completed(0),  # uv sync
                ],
            ) as mock_run,
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_run_checks", return_value="mypy failed:\n..."),
            patch.object(ad, "_send_alert"),
        ):
            ad.main()
        for call in mock_run.call_args_list:
            assert "reset" not in call.args[0]
            assert "branch" not in call.args[0]

    def test_check_failure_does_not_realert_same_stuck_sha(self):
        sha = "f" * 40
        ad.FAILED_SHA_FILE.write_text(sha + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                ],
            ) as mock_run,
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_run_checks") as mock_checks,
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        mock_alert.assert_not_called()
        # The whole point of the short-circuit: not even the check gate
        # (uv sync + ruff/mypy/pytest) re-runs for a SHA already known stuck.
        mock_checks.assert_not_called()
        assert mock_run.call_count == 2

    def test_check_failure_clears_and_reruns_once_a_new_commit_lands(self):
        """A different SHA than the one on file must not be short-circuited
        -- fixing forward and landing a new commit has to trigger a real
        re-run, not another silent skip."""
        old_sha = "j" * 40
        new_sha = "k" * 40
        ad.FAILED_SHA_FILE.write_text(old_sha + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=new_sha),
                    _completed(0),  # uv sync
                ],
            ),
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_run_checks", return_value=None) as mock_checks,
        ):
            ad.main()
        mock_checks.assert_called_once()
        assert ad.SHA_FILE.read_text().strip() == new_sha
        assert not ad.FAILED_SHA_FILE.exists()

    def test_dirty_tree_blocks_deploy_and_alerts_once(self):
        sha = "l" * 40
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                ],
            ),
            patch.object(ad, "_working_tree_clean", return_value=False),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        mock_alert.assert_called_once()
        assert ad.DIRTY_TREE_SHA_FILE.read_text().strip() == sha
        assert not ad.SHA_FILE.exists()

    def test_dirty_tree_does_not_realert_same_stuck_sha(self):
        sha = "m" * 40
        ad.DIRTY_TREE_SHA_FILE.write_text(sha + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                ],
            ),
            patch.object(ad, "_working_tree_clean", return_value=False),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()
        mock_alert.assert_not_called()

    def test_clean_tree_after_dirty_alert_clears_the_marker_and_deploys(self):
        sha = "n" * 40
        ad.DIRTY_TREE_SHA_FILE.write_text(("o" * 40) + "\n")
        with (
            patch.object(
                ad,
                "run",
                side_effect=[
                    _completed(0, stdout="main"),
                    _completed(0, stdout=sha),
                    _completed(0),  # uv sync
                ],
            ),
            patch.object(ad, "_working_tree_clean", return_value=True),
            patch.object(ad, "_run_checks", return_value=None),
        ):
            ad.main()
        assert not ad.DIRTY_TREE_SHA_FILE.exists()
        assert ad.SHA_FILE.read_text().strip() == sha


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


class TestMainCrashSafety:
    """main() is a thin wrapper around _deploy_tick() specifically so a
    RAISED exception -- as opposed to a git/uv command that merely exits
    non-zero, which every check inside _deploy_tick already handles and
    alerts on -- still reaches an alert instead of just crashing the tick
    with nothing in Telegram to show for it. Every deploy-logic test in
    this file calls ad.main() already, exercising this wrapper for the
    non-crashing case; these two pin the crash path specifically."""

    def test_exception_in_deploy_tick_is_caught_and_alerted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(ad, "REPO", tmp_path)
        with (
            patch.object(ad, "_deploy_tick", side_effect=RuntimeError("disk full")),
            patch.object(ad, "_send_alert") as mock_alert,
        ):
            ad.main()  # must not raise
        mock_alert.assert_called_once()
        assert "disk full" in mock_alert.call_args[0][0]

    def test_no_exception_calls_deploy_tick_normally(self):
        with patch.object(ad, "_deploy_tick") as mock_tick:
            ad.main()
        mock_tick.assert_called_once()


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
