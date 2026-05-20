#!/bin/bash
# Auto-deploy: run every 60 seconds via launchd.
# Fetches origin/main; if local main is behind, pulls, syncs the venv, and restarts the bots.

REPO="$HOME/Documents/Dev/organist_bot"
VENV="$HOME/.venvs/organist_bot"
UV="$HOME/.local/bin/uv"

# Exit cleanly when offline or if the fetch fails for any reason.
git -C "$REPO" fetch origin main --quiet 2>/dev/null || exit 0

LOCAL=$(git -C "$REPO" rev-parse main 2>/dev/null)
REMOTE=$(git -C "$REPO" rev-parse origin/main 2>/dev/null)

[ "$LOCAL" = "$REMOTE" ] && exit 0

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] New commits on main — deploying"

git -C "$REPO" checkout main --quiet 2>/dev/null || true
git -C "$REPO" reset --hard origin/main || { echo "git reset failed"; exit 1; }

UV_PROJECT_ENVIRONMENT="$VENV" "$UV" sync --project "$REPO"

UID_VAL=$(id -u)
for PLIST in \
    "$HOME/Library/LaunchAgents/com.organistbot.scheduler.plist" \
    "$HOME/Library/LaunchAgents/com.organistbot.telegram.plist"; do
    launchctl bootout "gui/$UID_VAL" "$PLIST" 2>/dev/null || true
    launchctl bootstrap "gui/$UID_VAL" "$PLIST"
done

echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Deploy complete (v2)"
