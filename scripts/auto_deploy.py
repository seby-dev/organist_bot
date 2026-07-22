"""
Auto-deploy: run every 60 seconds via launchd.
Fetches origin/main; if origin/main differs from the last deployed SHA,
fast-forwards onto it (never destroys local work), syncs the venv, and
restarts the bots via launchctl.

REPO is also the interactive dev working copy, so this deliberately never
does a hard reset: a fast-forward-only merge just refuses (leaving
everything untouched) if there are local commits or uncommitted changes
that would be overwritten, or if HEAD isn't on main.
"""

import os
import subprocess
import sys
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

branch = run(
    GIT + ["rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
).stdout.strip()
if branch != "main":
    print(f"[{ts()}] HEAD is on '{branch}', not main -- skipping auto-deploy")
    sys.exit(0)

print(f"[{ts()}] New commits on main -- attempting deploy")

result = run(GIT + ["merge", "--ff-only", "origin/main"], capture_output=True, text=True)
if result.returncode != 0:
    print(f"[{ts()}] Fast-forward not possible (local changes or divergence) -- skipping deploy")
    print(result.stdout)
    print(result.stderr)
    sys.exit(0)

result = run([str(UV), "sync", "--project", str(REPO), "--extra", "dev"])
if result.returncode != 0:
    print(f"[{ts()}] uv sync failed")
    sys.exit(1)

for plist in PLISTS:
    run(["launchctl", "bootout", f"gui/{UID}", str(plist)], capture_output=True)
    run(["launchctl", "bootstrap", f"gui/{UID}", str(plist)])

SHA_FILE.write_text(remote + "\n")
print(f"[{ts()}] Deploy complete -- now at {remote}")
