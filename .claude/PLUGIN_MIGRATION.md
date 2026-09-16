# Action required: install the sebby-toolkit plugin

This project's generic Claude Code hooks (env-file git-add blocking,
transcript secret scrubbing, post-edit lint, stop-time quality gate,
post-PR-created nudge, post-merge docs-sync nudge) were removed from
`.claude/hooks/` in favor of the shared `sebby-toolkit` plugin. **Until you
install it, none of those hooks will fire.**

## Install

    /plugin marketplace add seby-dev/sebby
    /plugin install sebby-toolkit

## Configure

Add an `"env"` block to `.claude/settings.json` (or set these in your
shell environment) so the plugin's hooks match this project's previous
behavior:

    "env": {
      "SEBBY_LINT_COMMAND": "ruff check --output-format=concise",
      "SEBBY_TYPECHECK_COMMAND": "mypy organist_bot/",
      "SEBBY_DOCS_FILES": "README.md,technical-report.html"
    }

`SEBBY_LINT_COMMAND`'s default already matches what this project used, so
that one is optional; `SEBBY_TYPECHECK_COMMAND` and `SEBBY_DOCS_FILES` need
to be set explicitly to match this project's prior `stop_quality_check.py`
(which checked `organist_bot/` specifically) and `post_merge_docs_update.py`
(which checked both `README.md` and `technical-report.html`) behavior.

Once installed and configured, delete this file.
