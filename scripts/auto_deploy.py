"""
Auto-deploy: run every 60 seconds via launchd.
Deploys whenever local main's own HEAD differs from the last deployed SHA:
re-verifies the code locally (ruff/mypy/pytest — the same checks CI runs)
and only then syncs the venv and restarts the bots via launchctl.

This never fetches or merges from origin itself — local main only ever
advances via something else (a manual `git pull`/`git merge`, `gh pr
merge` run directly in this checkout, ...), and this script purely reacts
to that. A deploy therefore always reflects exactly what's checked out
here. This does mean a merge to origin/main alone no longer reaches
production on its own: something still has to advance this checkout's
local main for a deploy to follow -- _check_stale_origin sends one
read-only Telegram alert per such gap (reading the cached
refs/remotes/origin/main, never fetching) so that stall isn't silent, but
it never fetches, merges, or deploys anything itself. This script
otherwise makes no *git* network call of its own (`uv sync` still resolves
dependencies over the network, and a stuck failure there alerts the same
as any other check failure).

REPO is also the interactive dev working copy. A deploy only runs when
HEAD is on `main` (a checkout left on another branch alerts once per stuck
commit — see WRONG_BRANCH_SHA_FILE — rather than failing silently) and the
tree has no uncommitted changes (same alert-once pattern — see
DIRTY_TREE_SHA_FILE — since deploying a dirty tree would ship code that
doesn't correspond to the commit SHA_FILE then records as deployed).

A commit that fails the ruff/mypy/pytest check gate IS rolled back (see
_rescue_and_rollback: `git reset --hard` to the last good deploy, but only
after saving the failing commit to a real branch first — unlike the old
origin-driven fast-forward, always safely re-fetchable, local main's HEAD
here can carry a commit nothing else has a copy of). The rollback exists
because both bot launchd jobs set KeepAlive: leaving a failed commit
simply checked out would mean the *next unrelated restart* (a crash, a
reboot, anything) loads it anyway, with no gate at all. This alerts once
per stuck SHA (see FAILED_SHA_FILE) either way, and re-applies the same
rollback if that exact commit is ever checked out again (e.g. re-pulled
after a previous rollback moved local main away from it) rather than
silently leaving it in place a second time.

`uv sync` failing is handled differently, deliberately not via
FAILED_SHA_FILE: it's usually environmental (a network blip, a registry
hiccup) rather than a property of the commit's code, so it's retried
every tick instead of being treated as permanently stuck (see
UV_SYNC_FAILED_SHA_FILE, which only dedups the alert, never blocks a
retry).

Importing this module must have zero side effects — the real deploy flow
lives in _deploy_tick(), wrapped by main() (only invoked when run as a
script) so an unexpected raised exception -- as opposed to a git/uv
command that merely exits non-zero, which every check above already
handles -- still alerts instead of just crashing the tick silently.
"""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

REPO = Path.home() / "Developer/organist_bot"
UV = Path.home() / ".local/bin/uv"
UID = os.getuid()

GIT = ["git", "-C", str(REPO)]
PLISTS = [
    Path.home() / "Library/LaunchAgents/com.organistbot.scheduler.plist",
    Path.home() / "Library/LaunchAgents/com.organistbot.telegram.plist",
]
# Records the SHA that was last successfully deployed so that out-of-band
# advances of the local ref (e.g. gh pr merge fast-forward) don't suppress
# a needed restart.
SHA_FILE = REPO / "data" / "last_deployed_sha.txt"
# Records the SHA of the last commit that failed the local re-run gate, so a
# stuck failure alerts once rather than every 60-second tick.
FAILED_SHA_FILE = REPO / "data" / "last_failed_deploy_sha.txt"
# Records the SHA of the last commit that couldn't deploy because HEAD wasn't
# on main, so that alert also fires once per stuck commit rather than every
# 60-second tick.
WRONG_BRANCH_SHA_FILE = REPO / "data" / "last_wrong_branch_alert_sha.txt"
# Records the SHA of the last commit that couldn't deploy because REPO had
# uncommitted changes (this is also the interactive dev working copy), so
# that alert too fires once per stuck commit rather than every 60-second tick.
DIRTY_TREE_SHA_FILE = REPO / "data" / "last_dirty_tree_alert_sha.txt"
# Records the origin/main SHA last flagged as ahead of local main by
# _check_stale_origin, so that read-only visibility alert also fires once
# per new gap rather than every 60-second tick.
STALE_ORIGIN_SHA_FILE = REPO / "data" / "last_stale_origin_alert_sha.txt"
# Records the SHA of the last commit whose `uv sync` failed, purely to dedup
# that alert -- unlike FAILED_SHA_FILE (a deterministic ruff/mypy/pytest
# failure, not worth re-running until the code changes), `uv sync` failing
# is usually environmental (a network blip, a registry hiccup) and IS
# worth retrying every tick, so this never short-circuits a re-attempt the
# way FAILED_SHA_FILE does.
UV_SYNC_FAILED_SHA_FILE = REPO / "data" / "last_uv_sync_failed_alert_sha.txt"


def run(cmd, **kwargs):
    return subprocess.run(cmd, **kwargs)


def ts():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_checks(repo: Path) -> str | None:
    """Run the local quality gate in `repo` — the same checks CI runs.

    Returns None if everything passes; otherwise a short failure summary
    (check label + up to the last 1500 chars of its combined output).
    """
    venv_bin = repo / ".venv" / "bin"
    checks = [
        ([str(venv_bin / "ruff"), "check", "."], "ruff check"),
        ([str(venv_bin / "ruff"), "format", "--check", "."], "ruff format --check"),
        ([str(venv_bin / "mypy"), "organist_bot/"], "mypy"),
        ([str(venv_bin / "pytest"), "--tb=short", "-q"], "pytest"),
    ]
    for cmd, label in checks:
        result = run(cmd, cwd=repo, capture_output=True, text=True)
        if result.returncode != 0:
            return f"{label} failed:\n{(result.stdout + result.stderr)[-1500:]}"
    return None


def _send_alert(message: str, repo: Path) -> None:
    """Standalone Telegram alert — deliberately does not import organist_bot,
    so a broken deploy can never take down its own failure-reporting path."""
    try:
        import requests
        from dotenv import dotenv_values

        env = dotenv_values(repo / ".env")
        token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
        if token and chat_id:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=10,
            )
    except Exception as exc:
        print(f"[{ts()}] alert failed: {exc}")  # best-effort; never crash the deploy script


def _already_alerted(sha: str, failed_sha_file: Path) -> bool:
    return failed_sha_file.exists() and failed_sha_file.read_text().strip() == sha


def _working_tree_clean(repo: Path) -> bool:
    result = run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    return result.returncode == 0 and result.stdout.strip() == ""


def _check_stale_origin() -> None:
    """Best-effort, purely read-only visibility that origin/main has moved
    on without local main following it. Since nothing auto-pulls from
    origin any more (see main()'s docstring), a squash-merged PR otherwise
    reaches origin/main and just sits there with no signal anywhere until a
    human happens to `git pull` in this checkout.

    Reads REPO's cached remote-tracking ref (refs/remotes/origin/main) --
    never fetches, never merges, never touches local main or the working
    tree, and never affects whether a deploy runs this tick. The signal is
    therefore only as fresh as the last time something else (a `git
    fetch`/`git push`/`gh` command run directly in this checkout) updated
    that ref; a long-idle checkout can go a while without noticing a gap,
    but it will never claim one exists when it doesn't."""
    origin_result = run(
        GIT + ["rev-parse", "--verify", "refs/remotes/origin/main"], capture_output=True, text=True
    )
    if origin_result.returncode != 0:
        # No remote-tracking ref yet (nothing has ever fetched in this
        # checkout) -- expected on a fresh clone, not itself a problem.
        print(f"[{ts()}] _check_stale_origin: no refs/remotes/origin/main yet -- skipping")
        return
    origin_sha = origin_result.stdout.strip()

    count_result = run(
        GIT + ["rev-list", "--count", f"refs/heads/main..{origin_sha}"],
        capture_output=True,
        text=True,
    )
    if count_result.returncode != 0:
        print(f"[{ts()}] _check_stale_origin: rev-list failed: {count_result.stderr.strip()[:200]}")
        return
    try:
        behind = int(count_result.stdout.strip())
    except ValueError:
        print(
            f"[{ts()}] _check_stale_origin: unexpected rev-list output: "
            f"{count_result.stdout.strip()[:200]!r}"
        )
        return

    if behind == 0:
        if STALE_ORIGIN_SHA_FILE.exists():
            STALE_ORIGIN_SHA_FILE.unlink()
        return

    if not _already_alerted(origin_sha, STALE_ORIGIN_SHA_FILE):
        _send_alert(
            f"ℹ️ origin/main has {behind} commit(s) local main doesn't (up to "
            f"{origin_sha[:8]}) — run `git pull` in {REPO} to deploy them.",
            REPO,
        )
        STALE_ORIGIN_SHA_FILE.write_text(origin_sha + "\n")


def _rescue_and_rollback(main_sha: str, last_deployed: str) -> None:
    """Save `main_sha` to a real branch (`autodeploy-failed-<sha[:8]>`), then
    `git reset --hard` local main back to `last_deployed`.

    Both bot launchd jobs set KeepAlive -- any *unrelated* restart (crash,
    reboot, `launchctl kickstart`, ...) would otherwise load whatever's on
    disk with no gate at all if a commit that failed the check gate were
    simply left checked out. But (unlike the old origin-driven design)
    local main's HEAD here can carry a commit nothing else has a copy of,
    e.g. one made directly in this checkout and never pushed anywhere, so
    it's saved to a branch first -- and every step below is checked and
    alerts loudly rather than silently proceeding, since a `reset --hard`
    that runs without that save having actually succeeded is exactly the
    data loss this whole mechanism exists to prevent.

    No-ops (leaves the commit checked out, exactly as if this were never
    called) when there's no prior deploy to roll back to, or when the
    branch save or the reset itself fails."""
    if not last_deployed:
        print(f"[{ts()}] Leaving broken commit checked out (no prior deploy to roll back to)")
        return

    # Re-checked here, not just once at the top of main(): _run_checks alone
    # takes minutes on this repo (ruff/mypy/pytest), and REPO is also the
    # interactive dev working copy -- if it went dirty during that run (e.g.
    # the operator started fixing things the moment the "deploy blocked"
    # alert landed), resetting now would discard those uncommitted edits,
    # and the rescue branch below only protects *committed* work.
    if not _working_tree_clean(REPO):
        print(f"[{ts()}] Tree went dirty during the check run -- not rolling back")
        _send_alert(
            f"🛑 {main_sha[:8]} failed checks; rollback skipped because {REPO} now has "
            "uncommitted changes. Resolve by hand.",
            REPO,
        )
        return

    rescue_branch = f"autodeploy-failed-{main_sha[:8]}"
    branch_result = run(
        GIT + ["branch", "--force", rescue_branch, main_sha], capture_output=True, text=True
    )
    if branch_result.returncode != 0:
        print(f"[{ts()}] Could not save {main_sha[:8]} to {rescue_branch} -- NOT rolling back")
        _send_alert(
            f"🛑 {main_sha[:8]} failed checks but could not be saved to {rescue_branch} "
            f"({branch_result.stderr.strip()[:200]}) — rollback skipped; the failed commit "
            f"is still checked out. Fix manually in {REPO}.",
            REPO,
        )
        return

    reset_result = run(GIT + ["reset", "--hard", last_deployed], capture_output=True, text=True)
    if reset_result.returncode != 0:
        print(f"[{ts()}] rollback reset failed: {reset_result.stderr.strip()[:200]}")
        _send_alert(
            f"🛑 {main_sha[:8]} failed checks and rolling back to {last_deployed[:8]} itself "
            f"failed — the failed commit may still be checked out. Fix manually in {REPO} "
            f"(it's saved at branch {rescue_branch} either way).",
            REPO,
        )
        return

    result = run([str(UV), "sync", "--project", str(REPO), "--extra", "dev"], capture_output=True)
    if result.returncode != 0:
        print(f"[{ts()}] rollback uv sync failed")
    print(
        f"[{ts()}] Rolled back working tree to last good deploy {last_deployed[:8]} "
        f"-- the failed commit is saved at branch {rescue_branch}"
    )


def _deploy_tick() -> None:
    _check_stale_origin()

    branch_result = run(GIT + ["rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True)
    if branch_result.returncode != 0:
        print(f"[{ts()}] Could not determine current branch -- skipping this tick")
        return
    branch = branch_result.stdout.strip()

    main_sha_result = run(
        GIT + ["rev-parse", "--verify", "refs/heads/main"], capture_output=True, text=True
    )
    if main_sha_result.returncode != 0:
        return
    main_sha = main_sha_result.stdout.strip()

    last_deployed = SHA_FILE.read_text().strip() if SHA_FILE.exists() else ""

    # This no-op fast path is checked before the branch check on purpose: a
    # `main` ref mid-rebase (or any other in-progress git operation) doesn't
    # move until it completes, so this returns before a rebase's detached
    # HEAD could ever be mistaken for "checkout is on the wrong branch".
    if main_sha == last_deployed:
        return

    if branch != "main":
        print(f"[{ts()}] HEAD is on '{branch}', not main -- skipping auto-deploy")
        if not _already_alerted(main_sha, WRONG_BRANCH_SHA_FILE):
            _send_alert(
                f"⚠️ Deploy blocked — checkout is on '{branch}', not main. "
                f"Commit {main_sha[:8]} on local main will not deploy until "
                "this checkout is switched back to main.",
                REPO,
            )
            WRONG_BRANCH_SHA_FILE.write_text(main_sha + "\n")
        return

    # REPO is also the interactive dev working copy. Deploying with
    # uncommitted changes present would ship code that doesn't correspond to
    # `main_sha` -- and, if the checks happen to pass anyway, SHA_FILE would
    # then record main_sha as cleanly deployed even though what's actually
    # running includes those uncommitted edits.
    if not _working_tree_clean(REPO):
        print(f"[{ts()}] Uncommitted changes present -- skipping auto-deploy")
        if not _already_alerted(main_sha, DIRTY_TREE_SHA_FILE):
            _send_alert(
                f"⚠️ Deploy blocked — {main_sha[:8]} on local main won't deploy while "
                "this checkout has uncommitted changes. Commit or discard them to continue.",
                REPO,
            )
            DIRTY_TREE_SHA_FILE.write_text(main_sha + "\n")
        return
    if DIRTY_TREE_SHA_FILE.exists():
        DIRTY_TREE_SHA_FILE.unlink()

    # A commit that already failed the gate need not re-run the full
    # ruff/mypy/pytest suite every 60 seconds for nothing until it changes
    # -- but it must still be rolled back if it's checked out again (e.g.
    # re-pulled after a previous rollback moved local main away from it),
    # since leaving a known-bad commit checked out under KeepAlive is
    # exactly the hazard _rescue_and_rollback exists to prevent.
    if _already_alerted(main_sha, FAILED_SHA_FILE):
        print(f"[{ts()}] {main_sha[:8]} already failed the check gate -- rolling back again")
        _rescue_and_rollback(main_sha, last_deployed)
        return

    print(f"[{ts()}] New commit on local main -- attempting deploy")

    result = run([str(UV), "sync", "--project", str(REPO), "--extra", "dev"])
    if result.returncode != 0:
        print(f"[{ts()}] uv sync failed")
        # No FAILED_SHA_FILE / rollback here on purpose: `uv sync` failing
        # is usually environmental (network blip, registry hiccup), not a
        # property of this commit's code, so it's worth retrying next tick
        # rather than treated as permanently stuck -- see
        # UV_SYNC_FAILED_SHA_FILE's own comment.
        if not _already_alerted(main_sha, UV_SYNC_FAILED_SHA_FILE):
            _send_alert(f"⚠️ Deploy blocked — {main_sha[:8]}: `uv sync` failed (will retry).", REPO)
            UV_SYNC_FAILED_SHA_FILE.write_text(main_sha + "\n")
        return
    if UV_SYNC_FAILED_SHA_FILE.exists():
        UV_SYNC_FAILED_SHA_FILE.unlink()

    failure = _run_checks(REPO)
    if failure is not None:
        print(f"[{ts()}] Deploy gate failed:\n{failure}")
        if not _already_alerted(main_sha, FAILED_SHA_FILE):
            _send_alert(f"⚠️ Deploy blocked — {main_sha[:8]} failed checks:\n{failure[:500]}", REPO)
            FAILED_SHA_FILE.write_text(main_sha + "\n")
        _rescue_and_rollback(main_sha, last_deployed)
        return

    for plist in PLISTS:
        run(["launchctl", "bootout", f"gui/{UID}", str(plist)], capture_output=True)
        run(["launchctl", "bootstrap", f"gui/{UID}", str(plist)])

    SHA_FILE.write_text(main_sha + "\n")
    if FAILED_SHA_FILE.exists():
        FAILED_SHA_FILE.unlink()
    if WRONG_BRANCH_SHA_FILE.exists():
        WRONG_BRANCH_SHA_FILE.unlink()
    print(f"[{ts()}] Deploy complete -- now at {main_sha}")


def main() -> None:
    """Thin wrapper: every check above this line guards against a *checked*
    git/uv failure (a non-zero exit code) and alerts accordingly, but none
    of that protects against a *raised* exception -- git/uv missing
    entirely, a disk-full OSError writing one of the *_SHA_FILE markers,
    and so on. Catch anything that slips past all of that here so a crash
    still alerts instead of launchd just silently relaunching this every
    60 seconds with nothing in Telegram to show for it."""
    try:
        _deploy_tick()
    except Exception as exc:
        print(f"[{ts()}] auto_deploy crashed: {exc}")
        _send_alert(f"🛑 auto_deploy.py crashed unexpectedly: {exc}", REPO)


if __name__ == "__main__":
    main()
