#!/usr/bin/env bash
# Pushes the current feature branch, opens a PR (or reuses an existing one),
# and enables squash auto-merge — automating this repo's documented PR
# workflow (see CLAUDE.md) for both human use and Claude Code.
set -euo pipefail

BASE_BRANCH="main"
BASE_REF="origin/$BASE_BRANCH"

BRANCH="$(git rev-parse --abbrev-ref HEAD)"
if [ "$BRANCH" = "$BASE_BRANCH" ]; then
    echo "ERROR: Do not ship from $BASE_BRANCH — use a feature branch" >&2
    exit 1
fi

git push -u origin "$BRANCH"

if PR_URL="$(gh pr view --json url -q .url 2>/dev/null)"; then
    echo "PR already exists: $PR_URL"
else
    # Derive the title/body from the REMOTE base, never the local ref. A local
    # `main` lagging origin leaves already-merged commits inside the range, and
    # the oldest of those becomes the title — which GitHub reuses verbatim as
    # the squash commit message, writing a wrong subject into main permanently.
    # (This is what mistitled PR #72.)
    git fetch --quiet origin "$BASE_BRANCH"
    if ! git rev-parse --verify --quiet "$BASE_REF" >/dev/null; then
        echo "ERROR: $BASE_REF not found — cannot determine the PR base" >&2
        exit 1
    fi
    RANGE="$BASE_REF..$BRANCH"

    # `git log` is newest-first, so `tail -1` is the branch's OLDEST commit —
    # the one that sets the theme, later ones usually being review fixups.
    # Deliberately not `--reverse | head -1`: head closes the pipe after one
    # line, so once the log outgrows the pipe buffer git dies of SIGPIPE and
    # `set -o pipefail` turns that into a hard exit. `tail` drains the stream.
    TITLE="$(git log --no-merges "$RANGE" --format=%s | tail -1)"
    if [ -z "$TITLE" ]; then
        echo "ERROR: No commits ahead of $BASE_REF — nothing to ship" >&2
        exit 1
    fi
    BODY="$(git log --no-merges --reverse "$RANGE" --format='- %s')"
    PR_URL="$(gh pr create --base "$BASE_BRANCH" --title "$TITLE" --body "$BODY" --draft=false)"
fi

gh pr merge --squash --auto --delete-branch
echo "Shipped: $PR_URL"
echo "Auto-merge enabled — will merge once CI passes."
