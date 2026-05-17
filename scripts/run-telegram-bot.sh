#!/usr/bin/env bash
# Wrapper for telegram_bot.py — sources .env so supervisord/launchd can run
# without a pre-configured environment.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(dirname "$SCRIPT_DIR")"

# Export all variables from .env into this process's environment.
set -a
# shellcheck source=../.env
source "$PROJECT_DIR/.env"
set +a

# exec replaces the shell so supervisord tracks the correct PID.
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/telegram_bot.py"
