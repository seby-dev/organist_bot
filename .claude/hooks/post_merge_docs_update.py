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

pr_ref = f"PR #{pr_num}" if pr_num else "the PR"
pr_arg = pr_num or "<n>"

print(
    json.dumps(
        {
            "systemMessage": (
                f"`gh pr merge` just ran for {pr_ref}. Drive the docs-sync follow-up:\n"
                f"1. Confirm the merge actually landed (auto-merge may be waiting on CI): "
                f"`gh pr view {pr_arg} --json state,mergedAt`.\n"
                "2. If state != MERGED, stop here — the docs sync only runs after the merge completes.\n"
                f"3. Read the merged diff: `gh pr diff {pr_arg}` and "
                f"`gh pr view {pr_arg} --json files,title,body` for context.\n"
                "4. Update `README.md` if any user-facing surface changed: setup steps, env vars, "
                "commands, feature list, prerequisites.\n"
                "5. Update `technical-report.html` if any architectural surface changed: modules, "
                "data flow, pipeline phases, integrations, tools.\n"
                "6. If neither doc needs an update, say so and stop.\n"
                "7. If you edited either doc, spawn an Agent (subagent_type=general-purpose) with "
                'a prompt like: "Read README.md and technical-report.html against the latest code '
                'on main. Report any factual inconsistency in under 250 words." Address any '
                "findings before declaring done.\n"
                f"8. Commit on a `docs/post-merge-sync-{pr_arg}` branch with a `docs:` "
                "conventional commit and open a PR (same workflow as any other change)."
            )
        }
    )
)
