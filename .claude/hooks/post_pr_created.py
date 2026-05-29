#!/usr/bin/env python3
"""PostToolUse hook: after PR creation, trigger the review + auto-merge pipeline."""

import json
import sys

# Pull the PR number from the tool response so Claude's message is specific.
try:
    payload = json.load(sys.stdin)
    pr_url = payload.get("tool_response", {}).get("url", "the PR")
    pr_number = payload.get("tool_response", {}).get("number")
    pr_ref = f"PR #{pr_number}" if pr_number else pr_url
except Exception:
    pr_ref = "the PR"

print(
    json.dumps(
        {
            "systemMessage": (
                f"{pr_ref} has been created. Run the post-PR review pipeline now:\n"
                "1. Run the code-review skill at medium effort with --fix and apply all findings.\n"
                "2. Run the security-review skill on the pending branch diff and fix any findings.\n"
                "3. If both pass clean (or after fixes are pushed), call "
                "mcp__github__enable_pr_auto_merge with mergeMethod=SQUASH on this PR so it "
                "merges automatically once CI passes.\n"
                "If enable_pr_auto_merge fails because auto-merge is disabled on the repo, "
                "report that to the user and stop — do not retry."
            )
        }
    )
)
