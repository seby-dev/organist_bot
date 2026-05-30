#!/usr/bin/env python3
"""PostToolUse hook: after `gh pr create`, nudge the auto-merge step.

Reviews are enforced *before* PR creation by require_pr_reviewers.sh
(PreToolUse), so this hook only drives the post-create merge flow via the
`gh` CLI. The GitHub MCP server is not available in this environment, so the
older mcp__github__* merge path is intentionally not used here.
"""

import json
import re
import sys

# Try to pull the PR URL out of the gh stdout so the reminder is specific.
try:
    payload = json.load(sys.stdin)
    blob = json.dumps(payload.get("tool_response", ""))
    m = re.search(r"https://github\.com/[^\s\"']+/pull/\d+", blob)
    pr_ref = m.group(0) if m else "the PR"
except Exception:
    pr_ref = "the PR"

print(
    json.dumps(
        {
            "systemMessage": (
                f"{pr_ref} created. Finish the ship step (gh CLI — GitHub MCP is unavailable):\n"
                "1. Confirm CI: `gh pr checks` (or `gh pr checks --watch`).\n"
                "2. If every check passed, enable squash auto-merge: "
                "`gh pr merge --squash --auto --delete-branch`.\n"
                "3. If any check failed, report it to the user and do NOT merge."
            )
        }
    )
)
