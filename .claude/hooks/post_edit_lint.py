#!/usr/bin/env python3
"""PostToolUse hook: run ruff check after each file write/edit."""

import json
import subprocess
from pathlib import Path

proj_root = Path(__file__).parent.parent.parent

result = subprocess.run(
    ["ruff", "check", "--output-format=concise", str(proj_root)],
    capture_output=True,
    text=True,
    cwd=proj_root,
)

if result.returncode != 0 and result.stdout.strip():
    lines = result.stdout.strip().splitlines()
    preview = "\n".join(lines[:20])
    if len(lines) > 20:
        preview += f"\n… ({len(lines) - 20} more issues)"
    print(json.dumps({"systemMessage": f"ruff found issues — fix before committing:\n{preview}"}))
