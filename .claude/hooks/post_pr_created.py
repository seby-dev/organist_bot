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
                "3. Once both pass clean, check CI status with "
                "mcp__github__pull_request_read (method=get_check_runs).\n"
                "   - If all checks have conclusion=success: call mcp__github__merge_pull_request "
                "with mergeMethod=SQUASH to merge immediately.\n"
                "   - If checks are still in_progress or queued: call "
                "mcp__github__subscribe_pr_activity on this PR, then end your turn. "
                "When CI completes and you receive a webhook event showing all checks passed, "
                "merge with mcp__github__merge_pull_request (mergeMethod=SQUASH).\n"
                "   - If any check has conclusion=failure: report the failure to the user "
                "and do not merge."
            )
        }
    )
)
