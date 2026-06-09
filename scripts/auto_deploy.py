"""
Auto-deploy: run every 60 seconds via launchd.
Fetches origin/main; if origin/main differs from the last deployed SHA,
pulls, syncs the venv, and restarts the bots via launchctl.
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
# Records the SHA that was last successfully deployed so that out-of-band
# advances of the local ref (e.g. gh pr merge fast-forward) don't suppress
# a needed restart.
SHA_FILE = REPO / "data" / "last_deployed_sha.txt"


def run(cmd, **kwargs):
    return subprocess.run(cmd, **kwargs)


def ts():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


# Exit cleanly when offline or fetch fails.
result = run(GIT + ["fetch", "origin", "main", "--quiet"], capture_output=True)
if result.returncode != 0:
    sys.exit(0)

remote = run(GIT + ["rev-parse", "origin/main"], capture_output=True, text=True).stdout.strip()
last_deployed = SHA_FILE.read_text().strip() if SHA_FILE.exists() else ""

if remote == last_deployed:
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

SHA_FILE.write_text(remote + "\n")
print(f"[{ts()}] Deploy complete")
