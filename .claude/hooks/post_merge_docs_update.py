#!/usr/bin/env python3
"""PostToolUse hook: after `gh pr merge`, drive a docs-synchronisation pass.

Fires on any `gh pr merge` invocation (auto-merge or immediate). The injected
system message instructs Claude to:
  1. Confirm the PR is actually merged (auto-merge may still be pending CI).
  2. Read the merged diff.
  3. Update README.md and technical-report.html if any documented surface
     changed.
  4. Spawn a reviewer agent to verify the docs match the latest code on main.
  5. Open a follow-up PR with the docs updates.
"""

import json
import re
import sys

try:
    payload = json.load(sys.stdin)
    command = payload.get("tool_input", {}).get("command", "")
    response = json.dumps(payload.get("tool_response", ""))
    m = re.search(r"gh pr merge\s+(\d+)", command) or re.search(r"/pull/(\d+)", response)
    pr_num = m.group(1) if m else None
except Exception:
    pr_num = None

if pr_num:
    pr_ref = f"PR #{pr_num}"
    resolve_step = f"`gh pr view {pr_num} --json state,mergedAt`"
    diff_step = f"`gh pr diff {pr_num}` and `gh pr view {pr_num} --json files,title,body`"
    branch_name = f"docs/post-merge-sync-{pr_num}"
else:
    pr_ref = "the most recently merged PR"
    resolve_step = (
        "first resolve the PR number with "
        "`gh pr list --state merged --limit 1 --json number,mergedAt,title` "
        "(call this $PR), then check state with `gh pr view $PR --json state,mergedAt`"
    )
    diff_step = "`gh pr diff $PR` and `gh pr view $PR --json files,title,body`"
    branch_name = "docs/post-merge-sync-$PR"

print(
    json.dumps(
        {
            "systemMessage": (
                f"`gh pr merge` just ran for {pr_ref}. Drive the docs-sync follow-up:\n"
                f"1. Confirm the merge actually landed (auto-merge may be waiting on CI): "
                f"{resolve_step}.\n"
                "2. If state != MERGED, stop here — the docs sync only runs after the merge completes.\n"
                f"3. Read the merged diff: {diff_step} for context.\n"
                "4. Update `README.md` if any user-facing surface changed: setup steps, env vars, "
                "commands, feature list, prerequisites.\n"
                "5. Update `technical-report.html` if any architectural surface changed: modules, "
                "data flow, pipeline phases, integrations, tools.\n"
                "6. If neither doc needs an update, say so and stop.\n"
                "7. If you edited either doc, spawn an Agent (subagent_type=general-purpose) with "
                'a prompt like: "Read README.md and technical-report.html against the latest code '
                'on main. Report any factual inconsistency in under 250 words." Address any '
                "findings before declaring done.\n"
                f"8. Commit on a `{branch_name}` branch with a `docs:` conventional commit and "
                "open a PR (same workflow as any other change)."
            )
        }
    )
)
