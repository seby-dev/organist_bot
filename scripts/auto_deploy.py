"""
Auto-deploy: run every 60 seconds via launchd.
Fetches origin/main; if local main is behind, pulls, syncs the venv,
and restarts the bots via launchctl.
"""

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path.home() / "Documents/Dev/organist_bot"
VENV = Path.home() / ".venvs/organist_bot"
UV = Path.home() / ".local/bin/uv"
HOME = str(Path.home())
UID = os.getuid()

GIT = ["git", "-C", str(REPO)]
PLISTS = [
    Path.home() / "Library/LaunchAgents/com.organistbot.scheduler.plist",
    Path.home() / "Library/LaunchAgents/com.organistbot.telegram.plist",
]


def run(cmd, **kwargs):
    return subprocess.run(cmd, **kwargs)


def ts():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Exit cleanly when offline or fetch fails.
result = run(GIT + ["fetch", "origin", "main", "--quiet"], capture_output=True)
if result.returncode != 0:
    sys.exit(0)

local = run(GIT + ["rev-parse", "main"], capture_output=True, text=True).stdout.strip()
remote = run(GIT + ["rev-parse", "origin/main"], capture_output=True, text=True).stdout.strip()

if local == remote:
    sys.exit(0)

print(f"[{ts()}] New commits on main -- deploying")

run(GIT + ["checkout", "main", "--quiet"], capture_output=True)
result = run(GIT + ["reset", "--hard", "origin/main"])
if result.returncode != 0:
    print("git reset failed")
    sys.exit(1)

run(
    [str(UV), "sync", "--project", str(REPO)],
    env={**os.environ, "UV_PROJECT_ENVIRONMENT": str(VENV)},
)

for plist in PLISTS:
    run(["launchctl", "bootout", f"gui/{UID}", str(plist)], capture_output=True)
    run(["launchctl", "bootstrap", f"gui/{UID}", str(plist)])

print(f"[{ts()}] Deploy complete")
