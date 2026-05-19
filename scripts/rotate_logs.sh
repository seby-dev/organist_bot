#!/bin/bash
# Rotate OrganistBot launchd stdout/stderr logs.
# Runs daily via launchd. Uses copytruncate so running processes keep
# their file descriptors open without interruption.
# The Python JSON log (gigs.log) is already handled by RotatingFileHandler.

LOG_DIR="$HOME/Documents/Dev/organist_bot/logs"
MAX_BYTES=$((5 * 1024 * 1024))  # rotate at 5 MB
KEEP=3

rotate() {
    local base="$1"
    local path="$LOG_DIR/$base"
    [ -f "$path" ] || return

    local size
    size=$(stat -f%z "$path" 2>/dev/null || echo 0)
    [ "$size" -lt "$MAX_BYTES" ] && return

    echo "[$(date -u +%Y-%m-%dT%H:%M:%SZ)] Rotating $base ($(( size / 1024 ))K)"

    # Drop the oldest backup to make room
    rm -f "${path}.${KEEP}"

    # Shift existing numbered backups up by one
    for i in $(seq $((KEEP - 1)) -1 1); do
        [ -f "${path}.${i}" ] && mv "${path}.${i}" "${path}.$((i + 1))"
    done

    # copytruncate: copy current file, then zero the original in place.
    # The running process keeps writing to the same inode; we capture a
    # point-in-time snapshot as .1 without any gap in coverage.
    cp "$path" "${path}.1"
    : > "$path"
}

rotate "scheduler.log"
rotate "telegram.log"
rotate "scheduler.error.log"
rotate "telegram.error.log"
rotate "autodeploy.error.log"
