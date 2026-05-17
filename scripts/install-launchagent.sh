#!/usr/bin/env bash
# Installs a launchd LaunchAgent that starts supervisord at login.
# supervisord in turn manages both the scraper and the Telegram bot.
#
# Usage:
#   ./scripts/install-launchagent.sh          # install / re-install
#   ./scripts/install-launchagent.sh uninstall # remove
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"
LABEL="com.organistbot.supervisord"
PLIST="$HOME/Library/LaunchAgents/${LABEL}.plist"
SUPERVISORD="$PROJECT_DIR/.venv/bin/supervisord"
CONF="$PROJECT_DIR/supervisord.conf"
LOGS_DIR="$PROJECT_DIR/logs"

# ── Uninstall ──────────────────────────────────────────────────────────────────
if [[ "${1:-}" == "uninstall" ]]; then
    if launchctl list "$LABEL" &>/dev/null; then
        launchctl unload "$PLIST"
        echo "Unloaded $LABEL"
    fi
    rm -f "$PLIST"
    echo "Removed $PLIST"
    exit 0
fi

# ── Pre-flight checks ──────────────────────────────────────────────────────────
if [[ ! -f "$SUPERVISORD" ]]; then
    echo "Error: supervisord not found at $SUPERVISORD"
    echo "Run 'uv sync' or 'pip install supervisor' first."
    exit 1
fi

if [[ ! -f "$CONF" ]]; then
    echo "Error: supervisord.conf not found at $CONF"
    exit 1
fi

mkdir -p "$LOGS_DIR"

# ── Write plist ────────────────────────────────────────────────────────────────
cat > "$PLIST" <<PLIST_EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>${LABEL}</string>

    <key>ProgramArguments</key>
    <array>
        <string>${SUPERVISORD}</string>
        <string>-c</string>
        <string>${CONF}</string>
        <string>--nodaemon</string>
    </array>

    <key>WorkingDirectory</key>
    <string>${PROJECT_DIR}</string>

    <!-- Load .env so supervisord itself (and the programs it spawns via the  -->
    <!-- wrapper scripts) inherit the correct environment variables.           -->
    <key>EnvironmentVariables</key>
    <dict>
        <key>HOME</key>
        <string>${HOME}</string>
        <key>PATH</key>
        <string>${PROJECT_DIR}/.venv/bin:/usr/local/bin:/usr/bin:/bin</string>
    </dict>

    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>

    <key>StandardOutPath</key>
    <string>${LOGS_DIR}/launchd-supervisord.log</string>
    <key>StandardErrorPath</key>
    <string>${LOGS_DIR}/launchd-supervisord-err.log</string>
</dict>
</plist>
PLIST_EOF

echo "Wrote $PLIST"

# ── Load (or reload) ───────────────────────────────────────────────────────────
if launchctl list "$LABEL" &>/dev/null; then
    launchctl unload "$PLIST"
    echo "Unloaded existing agent"
fi

launchctl load "$PLIST"
echo "Loaded $LABEL — supervisord will start now and at every login"
echo ""
echo "Check status:  supervisorctl -c $CONF status"
echo "Stop all:      supervisorctl -c $CONF stop all"
echo "Uninstall:     $SCRIPT_DIR/install-launchagent.sh uninstall"
