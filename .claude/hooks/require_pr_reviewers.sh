#!/bin/bash
# PreToolUse hook: block `gh pr create` until both reviewers have run.
#
# Bypass sentinel is keyed to session_id + HEAD commit SHA, so:
#   - Re-running the same PR command in the same session: bypassed (no re-review).
#   - Making a new commit then attempting another PR: blocked (must re-review).
#   - Different session: blocked (must re-review).
#
# To bypass manually: touch the sentinel path printed in the block message.

set -eu

input="$(cat)"

# Self-filter: only gate `gh pr create`. Any other Bash command is allowed
# immediately, so this hook can never block unrelated work even if the
# settings matcher/if-filter is broader than intended.
cmd=$(echo "$input" | jq -r '.tool_input.command // ""')
case "$cmd" in
  *"gh pr create"*) ;;   # fall through to the review-gate check below
  *) exit 0 ;;           # not a PR-create command — allow
esac

session=$(echo "$input" | jq -r '.session_id // "unknown"')
repo_root="$(cd "$(dirname "$0")/../.." && pwd)"
head=$(git -C "$repo_root" rev-parse HEAD 2>/dev/null || echo unknown)
sentinel="/tmp/organistbot_reviewers_done_${session}_${head}"

if [ -f "$sentinel" ]; then
  exit 0
fi

jq -n --arg s "$sentinel" '{
  hookSpecificOutput: {
    hookEventName: "PreToolUse",
    permissionDecision: "deny",
    permissionDecisionReason: (
      "⚠️  Before opening a PR for organist_bot you MUST first run BOTH reviewers against the current HEAD:\n\n" +
      "  1. CodeRabbit:   invoke Skill(coderabbit:coderabbit-review)\n" +
      "  2. Local agent:  Agent(subagent_type=\"pipeline-impact-reviewer\", prompt=\"Review git diff main...HEAD for organist_bot cross-file invariants\")\n\n" +
      "Address all findings from BOTH reviewers. Then bypass and retry the SAME gh pr create command:\n" +
      "  touch " + $s + "\n" +
      "  # retry gh pr create ...\n\n" +
      "The sentinel is keyed to session_id + HEAD SHA, so new commits invalidate it and the reviewers must run again."
    )
  }
}'
