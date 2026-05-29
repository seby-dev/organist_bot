---
name: feature
description: End-to-end feature workflow — autopilot (plan+implement+PR) → simplify → code-review → security-review → verify. Use when implementing a new feature from scratch with full quality gates.
---

# Feature Implementation Workflow

## Overview

Full pipeline for shipping a new feature with quality gates baked in. Steps run
sequentially; any issues found in review steps are fixed before proceeding to
the next step.

## Steps

### 1. Implement — `/autopilot`
Invoke the `autopilot` skill with the feature description from args. Wait for it
to complete — it scopes, plans, implements, and opens a PR.

### 2. Simplify — `/simplify`
Invoke the `simplify` skill on the changed files. Apply all suggested cleanups
(reuse, dead code, altitude). Re-run only this step if fixes are needed.

### 3. Code review — `/code-review`
Invoke the `code-review` skill at **medium** effort with `--fix` to apply
findings automatically. If significant rework is needed, loop back to step 2
after fixing.

### 4. Security review — `/security-review`
Invoke the `security-review` skill on the pending branch diff. Fix any findings,
then re-run steps 3–4 until both pass clean.

### 5. Verify — `/verify`
Invoke the `verify` skill to run the app and confirm the feature works end-to-end
on the golden path and key edge cases. Document any regressions found and fix
them before marking the workflow complete.

### 6. Report
Summarise in a single message:
- What was built (feature name, files changed)
- What each review step caught and fixed
- PR URL

## Usage

```
/feature <task description>
```

**Examples:**
```
/feature Add a KeywordFilter that rejects gigs whose organisation name matches a configurable keyword blocklist
/feature Add a get_pending_applications tool to the unified agent so the user can ask which gigs are awaiting a reply
```

## Notes

- If autopilot opens a PR early, continue running steps 2–5 and push fixes to
  the same branch — auto-merge will pick them up.
- Skip `/verify` only if the change is purely internal (no user-visible
  behaviour, no runtime path changed) and say so explicitly in the report.
- The post-edit lint hook runs ruff automatically after every file write, so
  style issues are surfaced inline — you don't need a separate ruff step.
