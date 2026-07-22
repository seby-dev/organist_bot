"""
Auto-deploy: run every 60 seconds via launchd.
Fetches origin/main; if origin/main differs from the last deployed SHA,
fast-forwards onto it (never destroys local work), re-verifies the code
locally (ruff/mypy/pytest — the same checks CI runs), and only then syncs
the venv and restarts the bots via launchctl.

REPO is also the interactive dev working copy, so this deliberately never
does a hard reset on a dirty tree: a fast-forward-only merge just refuses
(leaving everything untouched) if there are local commits or uncommitted
changes that would be overwritten, or if HEAD isn't on main. The one
exception is the local-check-failure path below, which only resets when
the tree is already clean — see _working_tree_clean.

Importing this module must have zero side effects — the real deploy flow
lives in main(), only invoked when run as a script.
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


def main() -> None:
    # Exit cleanly when offline or fetch fails.
    result = run(GIT + ["fetch", "origin", "main", "--quiet"], capture_output=True)
    if result.returncode != 0:
        return

    remote = run(GIT + ["rev-parse", "origin/main"], capture_output=True, text=True).stdout.strip()
    last_deployed = SHA_FILE.read_text().strip() if SHA_FILE.exists() else ""

    if remote == last_deployed:
        return

    branch = run(
        GIT + ["rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    if branch != "main":
        print(f"[{ts()}] HEAD is on '{branch}', not main -- skipping auto-deploy")
        return

    print(f"[{ts()}] New commits on main -- attempting deploy")

    result = run(GIT + ["merge", "--ff-only", "origin/main"], capture_output=True, text=True)
    if result.returncode != 0:
        print(
            f"[{ts()}] Fast-forward not possible (local changes or divergence) -- skipping deploy"
        )
        print(result.stdout)
        print(result.stderr)
        return

    result = run([str(UV), "sync", "--project", str(REPO), "--extra", "dev"])
    if result.returncode != 0:
        print(f"[{ts()}] uv sync failed")
        return

    failure = _run_checks(REPO)
    if failure is not None:
        print(f"[{ts()}] Deploy gate failed:\n{failure}")
        if not _already_alerted(remote, FAILED_SHA_FILE):
            _send_alert(f"⚠️ Deploy blocked — {remote[:8]} failed checks:\n{failure[:500]}", REPO)
            FAILED_SHA_FILE.write_text(remote + "\n")
        if last_deployed and _working_tree_clean(REPO):
            run(GIT + ["reset", "--hard", last_deployed])
            result = run(
                [str(UV), "sync", "--project", str(REPO), "--extra", "dev"], capture_output=True
            )
            if result.returncode != 0:
                print(f"[{ts()}] rollback uv sync failed")
            print(f"[{ts()}] Rolled back working tree to last good deploy {last_deployed[:8]}")
        else:
            print(
                f"[{ts()}] Leaving broken commit checked out "
                "(no prior deploy, or uncommitted changes present)"
            )
        return

    for plist in PLISTS:
        run(["launchctl", "bootout", f"gui/{UID}", str(plist)], capture_output=True)
        run(["launchctl", "bootstrap", f"gui/{UID}", str(plist)])

    SHA_FILE.write_text(remote + "\n")
    if FAILED_SHA_FILE.exists():
        FAILED_SHA_FILE.unlink()
    print(f"[{ts()}] Deploy complete -- now at {remote}")


if __name__ == "__main__":
    main()
