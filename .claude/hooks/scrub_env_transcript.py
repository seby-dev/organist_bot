#!/usr/bin/env python3
"""Scrub .env secret values from the session transcript on disk.

Registered as a PostToolUse hook on Read|Write|Edit (fires when a .env file
is touched) and as a Stop hook (end-of-turn safety net). Every value defined
in .env / .env.* at the project root is replaced with [REDACTED:<KEY>] in the
transcript JSONL, so secrets never persist in transcript files.
"""

import json
import os
import sys
import time
from pathlib import Path

# Values shorter than this are skipped to avoid redacting trivial strings
# like "true", port numbers, or fee defaults that happen to appear elsewhere.
MIN_VALUE_LEN = 6


def collect_secrets(root: Path) -> dict[str, str]:
    secrets: dict[str, str] = {}
    for env_file in root.glob(".env*"):
        if not env_file.is_file():
            continue
        try:
            lines = env_file.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            value = value.strip().strip("'\"")
            if len(value) >= MIN_VALUE_LEN:
                secrets[value] = key.strip()
    return secrets


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        payload = {}

    tool_name = payload.get("tool_name")
    if tool_name:
        # PostToolUse: only act when the touched file is .env or .env.*
        file_path = (payload.get("tool_input") or {}).get("file_path", "")
        if not Path(file_path).name.startswith(".env"):
            sys.exit(0)

    transcript = payload.get("transcript_path")
    if not transcript or not os.path.exists(transcript):
        sys.exit(0)

    root = Path(os.environ.get("CLAUDE_PROJECT_DIR", "."))
    secrets = collect_secrets(root)
    if not secrets:
        sys.exit(0)

    if tool_name:
        # Hook runs async; give the transcript writer a moment to flush
        # the tool result before rewriting the file.
        time.sleep(2)

    with open(transcript, encoding="utf-8") as fh:
        text = fh.read()

    original = text
    # Longest values first so overlapping substrings can't leave fragments.
    for value in sorted(secrets, key=len, reverse=True):
        replacement = f"[REDACTED:{secrets[value]}]"
        text = text.replace(value, replacement)
        # Transcripts are JSONL — also match the JSON-escaped form.
        escaped = json.dumps(value)[1:-1]
        if escaped != value:
            text = text.replace(escaped, replacement)

    if text != original:
        tmp = transcript + ".scrub.tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            fh.write(text)
        os.replace(tmp, transcript)


if __name__ == "__main__":
    main()
