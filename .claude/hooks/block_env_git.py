#!/usr/bin/env python3
"""PreToolUse hook: block git operations that would commit .env files.

.gitignore already excludes .env and .env.*, so this guards the remaining
paths around it: explicit `git add .env`, force-adds (`git add -f`), and
commits where a .env file somehow ended up staged.
"""

import json
import os
import re
import shlex
import subprocess
import sys


def deny(reason: str) -> None:
    print(
        json.dumps(
            {
                "hookSpecificOutput": {
                    "hookEventName": "PreToolUse",
                    "permissionDecision": "deny",
                    "permissionDecisionReason": reason,
                }
            }
        )
    )
    sys.exit(0)


def is_env_path(path: str) -> bool:
    name = path.rstrip("/").rsplit("/", 1)[-1]
    return name == ".env" or name.startswith(".env.")


def main() -> None:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        sys.exit(0)

    command = (payload.get("tool_input") or {}).get("command", "")
    if "git" not in command:
        sys.exit(0)

    try:
        tokens = shlex.split(command)
    except ValueError:
        tokens = command.split()

    if "add" in tokens and any(is_env_path(t) for t in tokens):
        deny(
            "Blocked: .env files contain secrets and must never be staged. "
            "They are gitignored — do not add them, with or without -f."
        )

    if re.search(r"\bgit\b[^|;&]*\bcommit\b", command):
        result = subprocess.run(
            ["git", "diff", "--cached", "--name-only"],
            capture_output=True,
            text=True,
            cwd=os.environ.get("CLAUDE_PROJECT_DIR", "."),
        )
        offenders = [p for p in result.stdout.splitlines() if is_env_path(p)]
        if offenders:
            deny(
                "Blocked: staged .env files detected "
                f"({', '.join(offenders)}). Run `git restore --staged <file>` "
                "before committing."
            )

    sys.exit(0)


if __name__ == "__main__":
    main()
