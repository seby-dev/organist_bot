#!/usr/bin/env python3
"""Stop hook: run ruff + mypy when Claude stops; block if issues remain."""

import json
import subprocess
from pathlib import Path

proj_root = Path(__file__).parent.parent.parent
issues = []

ruff = subprocess.run(
    ["ruff", "check", "--output-format=concise", str(proj_root)],
    capture_output=True,
    text=True,
    cwd=proj_root,
)
if ruff.returncode != 0 and ruff.stdout.strip():
    lines = ruff.stdout.strip().splitlines()
    preview = "\n".join(lines[:20])
    if len(lines) > 20:
        preview += f"\n… ({len(lines) - 20} more)"
    issues.append(f"ruff:\n{preview}")

mypy = subprocess.run(
    ["mypy", "organist_bot/"],
    capture_output=True,
    text=True,
    cwd=proj_root,
)
if mypy.returncode != 0 and mypy.stdout.strip():
    lines = mypy.stdout.strip().splitlines()
    # Drop the summary line ("Found N errors") to save space
    error_lines = [l for l in lines if "error:" in l][:20]
    if error_lines:
        issues.append("mypy:\n" + "\n".join(error_lines))

if issues:
    reason = "Quality gate failed — fix these before finishing:\n\n" + "\n\n".join(issues)
    print(json.dumps({"decision": "block", "reason": reason}))
