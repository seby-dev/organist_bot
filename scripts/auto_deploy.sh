#!/bin/bash
# Auto-deploy: run every 60 seconds via launchd.
# Fetches origin/main; if local main is behind, pulls, syncs the venv, and restarts the bots.

REPO="$(cd "$(dirname "$0")/.." && pwd)"
VENV="$REPO/.venv"
UV="$HOME/.local/bin/uv"
SUPERVISORCTL="$VENV/bin/supervisorctl"
CONF="$REPO/supervisord.conf"

# Exit cleanly when offline or if the fetch fails for any reason.
git -C "$REPO" fetch origin main --quiet 2>/dev/null || exit 0

LOCAL=$(git -C "$REPO" rev-parse main 2>/dev/null)
REMOTE=$(git -C "$REPO" rev-parse origin/main 2>/dev/null)

[ "$LOCAL" = "$REMOTE" ] && exit 0

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] New commits on main — deploying"

git -C "$REPO" checkout main --quiet 2>/dev/null || true
git -C "$REPO" reset --hard origin/main || { echo "git reset failed"; exit 1; }

"$UV" sync --project "$REPO"

"$SUPERVISORCTL" -c "$CONF" restart all || { echo "supervisorctl restart failed"; exit 1; }

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Deploy complete"
