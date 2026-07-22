#!/usr/bin/env bash
# Pushes the current feature branch, opens a PR (or reuses an existing one),
# and enables squash auto-merge — automating this repo's documented PR
# workflow (see CLAUDE.md) for both human use and Claude Code.
set -euo pipefail

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" = "main" ]; then
    echo "ERROR: Do not ship from main — use a feature branch" >&2
    exit 1
fi

git push -u origin "$BRANCH"

if PR_URL="$(gh pr view --json url -q .url 2>/dev/null)"; then
    echo "PR already exists: $PR_URL"
else
    TITLE="$(git log --reverse main.."$BRANCH" --format=%s | head -1)"
    if [ -z "$TITLE" ]; then
        echo "ERROR: No commits ahead of main — nothing to ship" >&2
        exit 1
    fi
    BODY="$(git log --reverse main.."$BRANCH" --format='- %s')"
    PR_URL="$(gh pr create --title "$TITLE" --body "$BODY" --draft=false)"
fi

gh pr merge --squash --auto --delete-branch
echo "Shipped: $PR_URL"
echo "Auto-merge enabled — will merge once CI passes."
