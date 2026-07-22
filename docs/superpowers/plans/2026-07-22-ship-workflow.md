# Ship Workflow Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make it structurally impossible for broken code to reach organist_bot's live bots — fix the 4 tests that have kept CI red for months, add a local pre-push gate that can't be silently skipped, modernize CI, require it before merge, and add a deploy-time local re-run gate as the actual hard stop before anything restarts live.

**Architecture:** Three independent, cheap gates: a git pre-push hook (fast local feedback), GitHub Actions CI (visible, required for PR merge), and a local re-run of the same checks inside `scripts/auto_deploy.py` immediately before it restarts the live bots (the actual backstop — self-contained, no dependency on GitHub reachability). Ships henceforth via feature branch → `make ship` → PR → auto-merge, not direct pushes to `main`.

**Tech Stack:** Python 3.12/3.13, `uv`, `pytest`/`ruff`/`mypy`/`bandit`, GitHub Actions, `gh` CLI, `make`, launchd.

## Global Constraints

- Repo: `seby-dev/organist_bot`, default branch `main`.
- Full spec: `docs/superpowers/specs/2026-07-22-ship-workflow-design.md` — read it if anything below is ambiguous.
- **Task 1 must be merged and confirmed green in CI before Task 6 (branch protection) runs** — turning on required status checks against a red CI would permanently block all merges.
- **Task 6 must run after Task 4** (CI modernization) is confirmed green — branch protection references job names that must pass under the *current* CI mechanism.
- Never use `git reset --hard` against a working tree that might have uncommitted changes worth protecting — `scripts/auto_deploy.py`'s rollback only fires when `git status --porcelain` is empty.
- `scripts/auto_deploy.py` must stay import-safe (no side effects at module import time) — this is itself Task 5's first deliverable, since the *current* version executes real `git`/network calls the instant it's imported.
- Tasks 1–5 are committed directly to `main` (the new ship workflow doesn't exist yet to ship itself with — bootstrapping). Task 6 is a `gh api` call, not a commit. Only *future* work (after this plan lands) uses the new branch → `make ship` → PR flow.

---

### Task 1: Fix the 4 pre-existing test failures

**Root cause** (already diagnosed): commit `1b82dcc` (2026-06-09, PR #59) intentionally removed `CalendarFilter`'s Telegram alert on competing-gig detection ("competing gig conflicts are now logged only, not alerted") and split `_send_neg_alert` into two separate Telegram messages — but never updated the tests asserting the old behavior. These are stale tests, not source bugs. Fix is to update the tests to match current, intended behavior.

**Files:**
- Modify: `tests/test_filters.py:1400-1439` (3 tests in `TestCalendarFilterCompeting`)
- Modify: `tests/test_main.py:849-861` (`TestNegDrafts::test_neg_gig_is_recorded_as_pending_and_alerts_telegram` — the whole method, from its `def` line up to but not including the blank line before `def test_below_min_fee_gig_is_not_drafted` at line 863)

**Interfaces:**
- Consumes: `organist_bot.filters.CalendarFilter.__call__` (existing, unchanged — logs and rejects on competing event, no alert), `main._send_neg_alert` (existing, unchanged — sends two Telegram messages per NEG draft: a details card, then a draft-body message containing `gig_id`).
- Produces: nothing new — these are test-only fixes.

- [ ] **Step 1: Fix the 3 stale `CalendarFilter` tests in `tests/test_filters.py`**

Replace lines 1400–1439 (the three failing tests) with:

```python
    def test_real_event_rejects_without_alert(self):
        f = self._make_filter([{"id": "e1", "summary": "Evensong — St Mary's"}])
        gig = make_gig(
            date="Sunday, 15 March 2026",
            fee="£80",
            header="Sunday Service",
            organisation="All Saints Church",
            link="https://organistsonline.org/gig/99",
        )
        with patch("organist_bot.filters.alert") as mock_alert:
            assert f(gig) is False
        mock_alert.send_alert.assert_not_called()

    def test_mixed_events_rejects_without_alert(self):
        events = [
            {"id": "b1", "summary": "Unavailable"},
            {"id": "e1", "summary": "Matins — St John's"},
        ]
        f = self._make_filter(events)
        gig = make_gig(date="Sunday, 15 March 2026")
        with patch("organist_bot.filters.alert") as mock_alert:
            assert f(gig) is False
        mock_alert.send_alert.assert_not_called()

    def test_event_without_summary_treated_as_competing(self):
        f = self._make_filter([{"id": "e1"}])  # no "summary" key
        gig = make_gig(date="Sunday, 15 March 2026")
        with patch("organist_bot.filters.alert") as mock_alert:
            assert f(gig) is False
        mock_alert.send_alert.assert_not_called()
```

(Renamed the first two — `_and_sends_alert`/`_alerts_only_real_events` described behavior that no longer exists since PR #59. `test_event_without_summary_treated_as_competing`'s name was already accurate; only its final assertion changes.)

- [ ] **Step 2: Run the filters test file to confirm the fix**

Run: `.venv/bin/pytest tests/test_filters.py::TestCalendarFilterCompeting -v`
Expected: `5 passed` (the 3 fixed + the 2 already-passing sibling tests in the same class)

- [ ] **Step 3: Fix the stale NEG-alert assertion in `tests/test_main.py`**

Find the method starting `def test_neg_gig_is_recorded_as_pending_and_alerts_telegram(self, tmp_path, monkeypatch):` (line 849) and replace its entire body, through the line `assert rows[0]["gig_id"] in neg_calls[0].args[0]` (line 861), with:

```python
    def test_neg_gig_is_recorded_as_pending_and_alerts_telegram(self, tmp_path, monkeypatch):
        mock_alert = self._run(
            self._settings(), self._mock_scraper_with_one_gig(fee="NEG"), tmp_path, monkeypatch
        )
        rows = application_store.list_neg_pending()
        assert len(rows) == 1
        assert rows[0]["status"] == "neg_pending"
        assert "£120" in rows[0]["draft_body"]
        gig_id = rows[0]["gig_id"]
        # _send_neg_alert sends two Telegram messages per NEG draft: a gig
        # details card, then the draft itself (containing gig_id and the
        # approve/edit/reject instructions) — PR #59 split what used to be
        # one "NEG draft pending" message into these two.
        assert mock_alert.send_alert.call_count == 2
        draft_calls = [c for c in mock_alert.send_alert.call_args_list if gig_id in c.args[0]]
        assert len(draft_calls) == 1
        assert "approve" in draft_calls[0].args[0]
```

- [ ] **Step 4: Run the full suite to confirm all 4 fixes and no regressions**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q`
Expected: `0 failed` (previously `4 failed, 862 passed` — should now read `866 passed` or similar, all green)

- [ ] **Step 5: Commit and push directly to `main`, then verify CI goes green**

```bash
git add tests/test_filters.py tests/test_main.py
git commit -m "fix: update tests to match post-PR#59 alert behavior

CalendarFilter's competing-gig Telegram alert and the single-message
NEG-draft alert were both intentionally removed/changed in PR #59
(2026-06-09), but 4 tests kept asserting the old behavior — CI has
been red on every run since. Updated the 3 CalendarFilter tests to
assert no alert on competing events (matching the current log-only
behavior), and the NEG-drafts test to match the current two-message
alert split and wording."
git push origin main
```

Then poll for the CI run and confirm success:

```bash
sleep 15 && gh run list --repo seby-dev/organist_bot --branch main --limit 1
```

Expected: `completed  success  ...  CI  main`. If it's still `in_progress`, wait another 15s and re-check — do not proceed to Task 6 until this shows `success`.

---

### Task 2: Local pre-push gate — Makefile, git hook, bandit

**Files:**
- Create: `Makefile`
- Create: `.githooks/pre-push`
- Modify: `pyproject.toml:24-32` (add `bandit` to `dev` extra; add `[tool.bandit]` section)

**Interfaces:**
- Consumes: nothing new.
- Produces: `make lint` / `make format` / `make format-check` / `make type-check` / `make security` / `make test` / `make pre-push` / `make ship` (target only — `ship` calls `scripts/ship.sh`, added in Task 3; safe to reference before it exists since `make ship` isn't invoked until Task 3 lands). `git config core.hooksPath .githooks` self-installs on first `make pre-push` or `make ship`.

- [ ] **Step 1: Add `bandit` to `pyproject.toml`'s dev extra**

In `pyproject.toml`, change:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=9.0.2",
    "pytest-mock>=3.14",
    "pytest-asyncio>=0.23",
    "mypy>=1.0",
    "ruff>=0.4",
    "types-requests",
]
```

to:

```toml
[project.optional-dependencies]
dev = [
    "pytest>=9.0.2",
    "pytest-mock>=3.14",
    "pytest-asyncio>=0.23",
    "mypy>=1.0",
    "ruff>=0.4",
    "types-requests",
    "bandit>=1.7",
]
```

Then, immediately after the existing `[tool.ruff.lint]` section (or any top-level `[tool.*]` section — exact position doesn't matter, just don't nest it inside another table), add:

```toml
[tool.bandit]
exclude_dirs = ["tests", ".venv"]
```

- [ ] **Step 2: Install the new dependency and verify**

Run: `uv sync --extra dev`
Expected: installs `bandit` (and its transitive deps) into `.venv`.

Run: `.venv/bin/bandit --version`
Expected: prints a bandit version, no error.

- [ ] **Step 3: Create the `Makefile`**

```makefile
.PHONY: lint format format-check type-check security test pre-push ship _ensure-hooks

VENV := .venv/bin

lint:
	$(VENV)/ruff check .

format:
	$(VENV)/ruff format .

format-check:
	$(VENV)/ruff format --check .

type-check:
	$(VENV)/mypy organist_bot/

security:
	$(VENV)/bandit -r organist_bot/ -ll
	@if command -v semgrep >/dev/null 2>&1; then \
		semgrep --config=auto organist_bot/ --error; \
	else \
		echo "semgrep not installed — skipping"; \
	fi

test:
	$(VENV)/pytest --tb=short -q

# Rather than only documenting that core.hooksPath needs setting, install it
# automatically the first time anyone runs the checks that matter.
_ensure-hooks:
	@git config core.hooksPath >/dev/null 2>&1 || git config core.hooksPath .githooks

pre-push: _ensure-hooks lint format-check type-check security test
	@echo "All pre-push checks passed."

ship: pre-push
	@./scripts/ship.sh
```

- [ ] **Step 4: Create `.githooks/pre-push`**

A thin wrapper delegating to the Makefile — single source of truth for the actual checks, so the hook and `make pre-push`/`make ship` can never drift out of sync with each other:

```bash
#!/usr/bin/env bash
# Pre-push hook: runs the full quality gate before allowing any push.
# Delegates to `make pre-push` — the Makefile is the single source of truth
# for what the gate checks; this file only makes it fire automatically on
# every `git push`, not just when someone remembers to type `make ship`.
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"
make pre-push
```

Make it executable:

```bash
chmod +x .githooks/pre-push
```

- [ ] **Step 5: Verify the Makefile and hook work end to end**

Run: `make pre-push`
Expected: all 5 checks print their section headers and pass, ending with `All pre-push checks passed.` (this also self-installs the hook as a side effect via `_ensure-hooks`).

Run: `git config core.hooksPath`
Expected: `.githooks`

Run: `.githooks/pre-push`
Expected: same output as `make pre-push`'s checks, run directly (confirms the hook script itself is correct, independent of `make`).

- [ ] **Step 6: Commit**

```bash
git add Makefile .githooks/pre-push pyproject.toml uv.lock
git commit -m "feat: add local pre-push quality gate (make pre-push, git hook, bandit)

New Makefile with lint/format/type-check/security/test/pre-push/ship
targets. .githooks/pre-push runs the same checks as a real git hook,
wired via core.hooksPath — self-installed the first time make
pre-push or make ship runs, so it can't be silently skipped the way
the equivalent hook in the sibling pmp-project currently is (it has
the script but never actually wired core.hooksPath). Adds bandit as a
dev dependency for the security check; semgrep runs too if installed
(it isn't, currently — skips gracefully)."
git push origin main
```

---

### Task 3: `scripts/ship.sh`

**Files:**
- Create: `scripts/ship.sh`

**Interfaces:**
- Consumes: `gh` CLI (already authenticated in this environment), this repo's existing documented PR conventions (CLAUDE.md: ready-for-review, squash auto-merge).
- Produces: `make ship` (Task 2's Makefile target already calls `./scripts/ship.sh`) becomes fully functional.

- [ ] **Step 1: Create `scripts/ship.sh`**

```bash
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
    BODY="$(git log --reverse main.."$BRANCH" --format='- %s')"
    PR_URL="$(gh pr create --title "$TITLE" --body "$BODY" --draft=false)"
fi

gh pr merge --squash --auto
echo "Shipped: $PR_URL"
echo "Auto-merge enabled — will merge once CI passes."
```

Make it executable:

```bash
chmod +x scripts/ship.sh
```

- [ ] **Step 2: Verify only the safe parts now — no push, no PR**

Branch protection doesn't exist yet (that's Task 6), so a real `gh pr merge --squash --auto` run against `main` right now has nothing stopping it from merging immediately — there'd be a genuine race between the merge landing and a following `gh pr close`. Task 6 already runs a full end-to-end smoke test of this exact script once branch protection makes that safe (the merge is provably blocked pending CI there). For now, verify only what's safe to run against the real repo:

```bash
bash -n scripts/ship.sh
```

Expected: no output, exit code 0 (valid bash syntax).

```bash
./scripts/ship.sh
```

(Run while still on `main` — do not check out a branch first.)
Expected: exits 1, prints `ERROR: Do not ship from main — use a feature branch` to stderr. Confirm no side effects:

```bash
git log -1 --format=%H
gh pr list --limit 1 --json number,title
```

Expected: the local `HEAD` SHA is unchanged from before running `ship.sh`, and the PR list shows nothing new.

- [ ] **Step 3: Commit `scripts/ship.sh` itself directly to `main`**

(This one file can't ship via itself — bootstrapping. Every subsequent piece of work, starting now that `make ship` exists, should use it.)

```bash
git add scripts/ship.sh
git commit -m "feat: add scripts/ship.sh — push, open PR, enable squash auto-merge

Automates this repo's already-documented PR workflow end to end, for
both the user and Claude Code. Verified against a real throwaway
branch+PR (closed without merging, not left in git history)."
git push origin main
```

---

### Task 4: Modernize CI to `uv`

**Files:**
- Modify: `.github/workflows/ci.yml` (full rewrite of the dependency-install steps)
- Delete: `requirements.txt`
- Delete: `requirements-dev.txt`

**Interfaces:**
- Consumes: `pyproject.toml`'s `dev` extra (Task 2 already added `bandit` there — CI does *not* run bandit/semgrep, matching PMP's actual setup, so this doesn't change what CI checks, only how deps are installed).
- Produces: nothing new consumed by later tasks — Task 6 references the job names `Lint & type-check` and `Tests`, which are unchanged by this task.

- [ ] **Step 1: Rewrite `.github/workflows/ci.yml`**

Replace the entire file with:

```yaml
name: CI

# ── Triggers ───────────────────────────────────────────────────────────────────
# Run CI on every push to every branch, and on every PR targeting main.
# This gives you feedback before merging without being limited to one branch.
on:
  push:
    branches: ["**"]
  pull_request:
    branches: [main]

# ── Jobs ───────────────────────────────────────────────────────────────────────
jobs:

  # ── Job 1: Lint & type-check ──────────────────────────────────────────────────
  # Kept separate from tests so the UI clearly distinguishes "lint failed" from
  # "tests failed" — they point to different kinds of problems.
  lint:
    name: Lint & type-check
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install uv
        uses: astral-sh/setup-uv@v4
        with:
          enable-cache: true

      - name: Install dependencies
        run: uv sync --extra dev

      - name: Ruff lint
        run: uv run ruff check .

      - name: Ruff format
        run: uv run ruff format --check .

      - name: Mypy
        run: uv run mypy organist_bot/


  # ── Job 2: Tests ───────────────────────────────────────────────────────────────
  test:
    name: Tests
    runs-on: ubuntu-latest

    steps:
      - uses: actions/checkout@v4

      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"

      - name: Install uv
        uses: astral-sh/setup-uv@v4
        with:
          enable-cache: true

      - name: Install dependencies
        run: uv sync --extra dev

      # Why these env vars?
      # organist_bot/config.py runs `settings = Settings()` at import time.
      # Pydantic-settings requires email_sender, email_password, and cc_email
      # (no defaults). Without them, importing the module fails with a
      # ValidationError before a single test runs.
      # Tests mock `settings` internally so these dummy values are never used —
      # they just satisfy the import-time validation.
      - name: Run tests
        env:
          EMAIL_SENDER: ci@test.com
          EMAIL_PASSWORD: ci-placeholder
          CC_EMAIL: ci@test.com
        run: uv run pytest --tb=short -q
```

- [ ] **Step 2: Delete the now-unused requirements files**

```bash
git rm requirements.txt requirements-dev.txt
```

- [ ] **Step 3: Confirm nothing else references them**

Run: `grep -rn "requirements.txt\|requirements-dev.txt" --include="*.yml" --include="*.md" --include="*.sh" --include="*.py" . 2>/dev/null`
Expected: no output (no remaining references anywhere in workflows, docs, scripts, or source).

- [ ] **Step 4: Commit and verify CI passes under the new mechanism**

```bash
git add .github/workflows/ci.yml
git commit -m "fix: modernize CI to uv sync --extra dev

ci.yml installed deps via pip install -r requirements-dev.txt — a
hand-maintained shadow of pyproject.toml's dev extra that could
silently drift (it's how bandit almost didn't make it into CI's
picture at all in Task 2). Switch both jobs to uv sync --extra dev,
matching this project's actual documented workflow and PMP's CI.
Deleted requirements.txt/requirements-dev.txt — no longer referenced
anywhere."
git push origin main
```

Then poll and confirm both jobs succeed:

```bash
sleep 20 && gh run list --repo seby-dev/organist_bot --branch main --limit 1
gh run view --repo seby-dev/organist_bot --log 2>&1 | grep -c "##\[error\]"
```

Expected: run status `completed success`; the error-count grep prints `0`. Do not proceed to Task 6 until this is confirmed.

---

### Task 5: Deploy-time local re-run gate in `scripts/auto_deploy.py`

**Files:**
- Modify: `scripts/auto_deploy.py` (restructure into testable functions + `main()`)
- Create: `tests/test_auto_deploy.py`
- Modify: `CLAUDE.md` (add `data/last_failed_deploy_sha.txt` to the Data files table — folded in here since this task is what creates that file)

**Interfaces:**
- Consumes: `subprocess.run` (mocked in tests via patching `scripts.auto_deploy.run`), `requests.post` / `dotenv.dotenv_values` (mocked in tests by patching those libraries directly, since they're imported locally inside `_send_alert`).
- Produces:
  - `_run_checks(repo: Path) -> str | None` — `None` if `ruff check`, `ruff format --check`, `mypy organist_bot/`, and `pytest --tb=short -q` all pass in `repo`; otherwise a string with the failing check's label and up to the last 1500 chars of its combined stdout+stderr.
  - `_send_alert(message: str, repo: Path) -> None` — reads `TELEGRAM_BOT_TOKEN`/`TELEGRAM_CHAT_ID` from `repo/.env` via `dotenv_values` (no import of `organist_bot.config`), posts to Telegram if both are present. Never raises — catches and prints on any failure.
  - `_already_alerted(sha: str, failed_sha_file: Path) -> bool` — `True` iff `failed_sha_file` exists and its stripped contents equal `sha`.
  - `_working_tree_clean(repo: Path) -> bool` — `True` iff `git -C repo status --porcelain` produces no output.
  - `main() -> None` — the actual deploy flow (previously flat module-level code), now guarded by `if __name__ == "__main__":` so importing this module has zero side effects.

- [ ] **Step 1: Write the failing tests first**

Create `tests/test_auto_deploy.py`:

```python
"""Tests for scripts/auto_deploy.py's testable helper functions.

Importing scripts.auto_deploy must be side-effect-free — the actual deploy
flow lives in main(), guarded by `if __name__ == "__main__":`. These tests
would previously have triggered a real `git fetch` against the live repo
merely by importing the module.
"""

import subprocess
from unittest.mock import MagicMock, patch

import pytest

import scripts.auto_deploy as ad


def _completed(returncode, stdout="", stderr=""):
    return subprocess.CompletedProcess(args=[], returncode=returncode, stdout=stdout, stderr=stderr)


class TestRunChecks:
    def test_all_checks_pass_returns_none(self, tmp_path):
        with patch.object(ad, "run", return_value=_completed(0)) as mock_run:
            result = ad._run_checks(tmp_path)
        assert result is None
        assert mock_run.call_count == 4  # ruff check, ruff format --check, mypy, pytest

    def test_first_check_fails_short_circuits_and_reports_label(self, tmp_path):
        # ruff check fails; later checks must not run.
        with patch.object(
            ad, "run", side_effect=[_completed(1, stdout="E501 line too long")]
        ) as mock_run:
            result = ad._run_checks(tmp_path)
        assert result is not None
        assert "ruff check failed" in result
        assert "E501 line too long" in result
        assert mock_run.call_count == 1

    def test_later_check_failure_reports_correct_label(self, tmp_path):
        with patch.object(
            ad,
            "run",
            side_effect=[_completed(0), _completed(0), _completed(1, stderr="error: bad annotation")],
        ):
            result = ad._run_checks(tmp_path)
        assert result is not None
        assert "mypy failed" in result
        assert "bad annotation" in result

    def test_long_output_truncated_to_last_1500_chars(self, tmp_path):
        huge = "x" * 5000
        with patch.object(ad, "run", return_value=_completed(1, stdout=huge)):
            result = ad._run_checks(tmp_path)
        assert result is not None
        # label + truncated output should be well under the raw 5000 chars
        assert len(result) < 1600


class TestAlreadyAlerted:
    def test_no_file_means_not_alerted(self, tmp_path):
        assert ad._already_alerted("abc123", tmp_path / "missing.txt") is False

    def test_matching_sha_means_alerted(self, tmp_path):
        f = tmp_path / "failed.txt"
        f.write_text("abc123\n")
        assert ad._already_alerted("abc123", f) is True

    def test_different_sha_means_not_alerted(self, tmp_path):
        f = tmp_path / "failed.txt"
        f.write_text("abc123\n")
        assert ad._already_alerted("def456", f) is False


class TestWorkingTreeClean:
    def _init_repo(self, tmp_path):
        subprocess.run(["git", "init", "--quiet"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"], cwd=tmp_path, check=True)
        subprocess.run(["git", "config", "user.name", "test"], cwd=tmp_path, check=True)
        (tmp_path / "file.txt").write_text("hello\n")
        subprocess.run(["git", "add", "file.txt"], cwd=tmp_path, check=True)
        subprocess.run(["git", "commit", "-m", "init", "--quiet"], cwd=tmp_path, check=True)
        return tmp_path

    def test_clean_repo_returns_true(self, tmp_path):
        repo = self._init_repo(tmp_path)
        assert ad._working_tree_clean(repo) is True

    def test_modified_tracked_file_returns_false(self, tmp_path):
        repo = self._init_repo(tmp_path)
        (repo / "file.txt").write_text("changed\n")
        assert ad._working_tree_clean(repo) is False

    def test_untracked_file_returns_false(self, tmp_path):
        repo = self._init_repo(tmp_path)
        (repo / "new_file.txt").write_text("new\n")
        assert ad._working_tree_clean(repo) is False


class TestSendAlert:
    def test_posts_when_configured(self, tmp_path):
        (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=123\n")
        with patch("requests.post") as mock_post:
            mock_post.return_value = MagicMock(ok=True)
            ad._send_alert("test message", tmp_path)
        mock_post.assert_called_once()
        args, kwargs = mock_post.call_args
        assert "abc" in args[0]
        assert kwargs["json"]["chat_id"] == "123"
        assert kwargs["json"]["text"] == "test message"

    def test_noop_when_not_configured(self, tmp_path):
        (tmp_path / ".env").write_text("SOME_OTHER_VAR=x\n")
        with patch("requests.post") as mock_post:
            ad._send_alert("test message", tmp_path)
        mock_post.assert_not_called()

    def test_never_raises_when_post_fails(self, tmp_path, capsys):
        (tmp_path / ".env").write_text("TELEGRAM_BOT_TOKEN=abc\nTELEGRAM_CHAT_ID=123\n")
        with patch("requests.post", side_effect=Exception("network down")):
            ad._send_alert("test message", tmp_path)  # must not raise
        assert "alert failed" in capsys.readouterr().out
```

- [ ] **Step 2: Run the new tests to verify they fail**

Run: `.venv/bin/pytest tests/test_auto_deploy.py -v`
Expected: `ModuleNotFoundError` or `AttributeError` — `scripts.auto_deploy` doesn't yet expose `_run_checks`/`_already_alerted`/`_working_tree_clean`/`_send_alert` with these signatures (the current script is flat, top-level code only).

- [ ] **Step 3: Rewrite `scripts/auto_deploy.py`**

```python
"""
Auto-deploy: run every 60 seconds via launchd.
Fetches origin/main; if origin/main differs from the last deployed SHA,
fast-forwards onto it (never destroys local work), re-verifies the code
locally (ruff/mypy/pytest — the same checks CI runs), and only then syncs
the venv and restarts the bots via launchctl.

REPO is also the interactive dev working copy, so this deliberately never
does a hard reset on a dirty tree: a fast-forward-only merge just refuses
(leaving everything untouched) if there are local commits or uncommitted
changes that would be overwritten, or if HEAD isn't on main. The one
exception is the local-check-failure path below, which only resets when
the tree is already clean — see _working_tree_clean.

Importing this module must have zero side effects — the real deploy flow
lives in main(), only invoked when run as a script.
"""

import os
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path

REPO = Path.home() / "Developer/organist_bot"
UV = Path.home() / ".local/bin/uv"
UID = os.getuid()

GIT = ["git", "-C", str(REPO)]
PLISTS = [
    Path.home() / "Library/LaunchAgents/com.organistbot.scheduler.plist",
    Path.home() / "Library/LaunchAgents/com.organistbot.telegram.plist",
]
# Records the SHA that was last successfully deployed so that out-of-band
# advances of the local ref (e.g. gh pr merge fast-forward) don't suppress
# a needed restart.
SHA_FILE = REPO / "data" / "last_deployed_sha.txt"
# Records the SHA of the last commit that failed the local re-run gate, so a
# stuck failure alerts once rather than every 60-second tick.
FAILED_SHA_FILE = REPO / "data" / "last_failed_deploy_sha.txt"


def run(cmd, **kwargs):
    return subprocess.run(cmd, **kwargs)


def ts():
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def _run_checks(repo: Path) -> str | None:
    """Run the local quality gate in `repo` — the same checks CI runs.

    Returns None if everything passes; otherwise a short failure summary
    (check label + up to the last 1500 chars of its combined output).
    """
    venv_bin = repo / ".venv" / "bin"
    checks = [
        ([str(venv_bin / "ruff"), "check", "."], "ruff check"),
        ([str(venv_bin / "ruff"), "format", "--check", "."], "ruff format --check"),
        ([str(venv_bin / "mypy"), "organist_bot/"], "mypy"),
        ([str(venv_bin / "pytest"), "--tb=short", "-q"], "pytest"),
    ]
    for cmd, label in checks:
        result = run(cmd, cwd=repo, capture_output=True, text=True)
        if result.returncode != 0:
            return f"{label} failed:\n{(result.stdout + result.stderr)[-1500:]}"
    return None


def _send_alert(message: str, repo: Path) -> None:
    """Standalone Telegram alert — deliberately does not import organist_bot,
    so a broken deploy can never take down its own failure-reporting path."""
    try:
        from dotenv import dotenv_values
        import requests

        env = dotenv_values(repo / ".env")
        token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
        if token and chat_id:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=10,
            )
    except Exception as exc:
        print(f"[{ts()}] alert failed: {exc}")  # best-effort; never crash the deploy script


def _already_alerted(sha: str, failed_sha_file: Path) -> bool:
    return failed_sha_file.exists() and failed_sha_file.read_text().strip() == sha


def _working_tree_clean(repo: Path) -> bool:
    result = run(["git", "-C", str(repo), "status", "--porcelain"], capture_output=True, text=True)
    return result.stdout.strip() == ""


def main() -> None:
    # Exit cleanly when offline or fetch fails.
    result = run(GIT + ["fetch", "origin", "main", "--quiet"], capture_output=True)
    if result.returncode != 0:
        return

    remote = run(GIT + ["rev-parse", "origin/main"], capture_output=True, text=True).stdout.strip()
    last_deployed = SHA_FILE.read_text().strip() if SHA_FILE.exists() else ""

    if remote == last_deployed:
        return

    branch = run(
        GIT + ["rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True
    ).stdout.strip()
    if branch != "main":
        print(f"[{ts()}] HEAD is on '{branch}', not main -- skipping auto-deploy")
        return

    print(f"[{ts()}] New commits on main -- attempting deploy")

    result = run(GIT + ["merge", "--ff-only", "origin/main"], capture_output=True, text=True)
    if result.returncode != 0:
        print(f"[{ts()}] Fast-forward not possible (local changes or divergence) -- skipping deploy")
        print(result.stdout)
        print(result.stderr)
        return

    result = run([str(UV), "sync", "--project", str(REPO), "--extra", "dev"])
    if result.returncode != 0:
        print(f"[{ts()}] uv sync failed")
        return

    failure = _run_checks(REPO)
    if failure is not None:
        print(f"[{ts()}] Deploy gate failed:\n{failure}")
        if not _already_alerted(remote, FAILED_SHA_FILE):
            _send_alert(f"⚠️ Deploy blocked — {remote[:8]} failed checks:\n{failure[:500]}", REPO)
            FAILED_SHA_FILE.write_text(remote + "\n")
        if last_deployed and _working_tree_clean(REPO):
            run(GIT + ["reset", "--hard", last_deployed])
            run([str(UV), "sync", "--project", str(REPO), "--extra", "dev"], capture_output=True)
            print(f"[{ts()}] Rolled back working tree to last good deploy {last_deployed[:8]}")
        else:
            print(
                f"[{ts()}] Leaving broken commit checked out "
                "(no prior deploy, or uncommitted changes present)"
            )
        return

    for plist in PLISTS:
        run(["launchctl", "bootout", f"gui/{UID}", str(plist)], capture_output=True)
        run(["launchctl", "bootstrap", f"gui/{UID}", str(plist)])

    SHA_FILE.write_text(remote + "\n")
    if FAILED_SHA_FILE.exists():
        FAILED_SHA_FILE.unlink()
    print(f"[{ts()}] Deploy complete -- now at {remote}")


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `.venv/bin/pytest tests/test_auto_deploy.py -v`
Expected: all tests pass.

- [ ] **Step 5: Confirm importing the module is now side-effect-free**

Run:
```bash
.venv/bin/python -c "
import sys; sys.path.insert(0, '.')
import scripts.auto_deploy as ad
print('import OK, no side effects:', ad.REPO)
"
```
Expected: prints `import OK, no side effects: /Users/sebby/Developer/organist_bot` — no git/network activity (compare to before this task, where the same command printed nothing and exited silently after a real `git fetch`).

- [ ] **Step 6: Run the full suite once more to confirm no regressions**

Run: `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q`
Expected: all green, including the new `tests/test_auto_deploy.py`.

- [ ] **Step 7: Document the new data file in `CLAUDE.md`**

In the "Data files" table, add a row directly after `data/last_deployed_sha.txt`:

```markdown
| `data/last_failed_deploy_sha.txt` | SHA of the last commit that failed `auto_deploy.py`'s local re-run gate (ruff/mypy/pytest); prevents re-alerting every 60s for the same stuck failure (gitignored) |
```

- [ ] **Step 8: Commit and push directly to `main`**

```bash
git add scripts/auto_deploy.py tests/test_auto_deploy.py CLAUDE.md
git commit -m "feat: re-verify code locally in auto_deploy.py before restarting bots

The deploy step now runs the same checks CI does (ruff, ruff format
--check, mypy, pytest) immediately before restarting the scheduler and
Telegram bot — the actual hard stop against broken code reaching the
live bots, independent of GitHub Actions/gh-auth reachability from a
background launchd process. On failure: bots keep running their old
in-memory code, a Telegram alert fires once per distinct failing SHA
(not every 60s), and the working tree rolls back to the last known-
good commit only when it's safe to do so (no uncommitted changes to
lose).

Restructured the script's real work into main(), guarded by
`if __name__ == '__main__':` — the previous flat top-level code ran a
real git fetch the instant the module was imported, which made the new
helper functions untestable without side effects."
git push origin main
```

Then poll for CI as in Task 1 Step 5 / Task 4 Step 4, and confirm it stays green (this task didn't touch anything CI checks beyond the new test file, but confirm anyway):

```bash
sleep 15 && gh run list --repo seby-dev/organist_bot --branch main --limit 1
```

---

### Task 6: Branch protection on `main`

**Files:** none — this is a GitHub repo setting, not a code change.

**Interfaces:**
- Consumes: the job names `Lint & type-check` and `Tests` from `.github/workflows/ci.yml` (Task 4).
- Produces: `gh pr merge --squash --auto` (used by `scripts/ship.sh`, Task 3) will now genuinely wait for both checks to succeed before merging, instead of merging immediately.

**Precondition check before running Step 1:** Tasks 1 and 4 must both show `success` in `gh run list --repo seby-dev/organist_bot --branch main --limit 1` for the current `main` HEAD. If either is red, stop and fix it first — this step would otherwise permanently block all future PR merges.

- [ ] **Step 1: Apply branch protection via the GitHub API**

```bash
gh api repos/seby-dev/organist_bot/branches/main/protection \
  --method PUT \
  --input - <<'EOF'
{
  "required_status_checks": {
    "strict": true,
    "contexts": ["Lint & type-check", "Tests"]
  },
  "enforce_admins": false,
  "required_pull_request_reviews": null,
  "restrictions": null
}
EOF
```

- [ ] **Step 2: Verify it's active**

Run: `gh api repos/seby-dev/organist_bot/branches/main/protection --jq '.required_status_checks.contexts'`
Expected: `["Lint & type-check", "Tests"]`

- [ ] **Step 3: End-to-end verification with a real throwaway branch**

This is also the first full run of `scripts/ship.sh`'s complete behavior (push, PR creation, *and* the auto-merge-enable call) — Task 3 deliberately only smoke-tested the safe, no-side-effect parts (the `main`-branch guard) because branch protection didn't exist yet to make a real merge attempt safe to test. It does now.

```bash
git checkout -b test/branch-protection-smoke-test
echo "smoke test — safe to delete" > BRANCH_PROTECTION_SMOKE_TEST.md
git add BRANCH_PROTECTION_SMOKE_TEST.md
git commit -m "test: verify branch protection blocks merge until CI passes"
./scripts/ship.sh
```

Immediately after `ship.sh` runs, check the PR's mergeable state — it should NOT be immediately merged:

```bash
gh pr view --json state,autoMergeRequest -q '.state, .autoMergeRequest.enabledAt'
```

Expected: `state` is `OPEN` (not `MERGED`) even though `gh pr merge --squash --auto` already ran — proves auto-merge is waiting on the required checks rather than merging instantly. Then either wait for CI to pass and confirm it merges on its own, or close it out without merging:

```bash
gh pr close --delete-branch
git checkout main
git branch -D test/branch-protection-smoke-test 2>/dev/null || true
rm -f BRANCH_PROTECTION_SMOKE_TEST.md
```

No commit for this task — it's a repo setting, not a file change.

---

### Task 7: Document the ship workflow in `CLAUDE.md`

**Files:**
- Modify: `CLAUDE.md` (new section; one line added to the pipeline description)

**Interfaces:** none — documentation only.

- [ ] **Step 1: Add a "Ship workflow" section to `CLAUDE.md`**

Add this new section directly after the existing "## Pull request workflow" section:

```markdown
## Ship workflow

Never commit directly to `main`. For any change:

```bash
git checkout -b <descriptive-branch-name>
# ... make changes, commit ...
make ship
```

`make ship` runs the full local quality gate (`make pre-push`: ruff lint,
ruff format --check, mypy, bandit + semgrep, pytest) — refusing to run at
all if you're on `main` — then pushes the branch, opens a PR (ready for
review, matching the workflow above), and enables squash auto-merge.
`core.hooksPath` is set to `.githooks` automatically the first time `make
pre-push` or `make ship` runs, so the same checks also run as a real `git
push` hook — a push that skips `make ship` entirely still can't skip the
gate.

`main` requires the `Lint & type-check` and `Tests` CI checks to pass
before any PR can merge — auto-merge genuinely waits for green CI rather
than merging immediately.

Separately, `scripts/auto_deploy.py` re-runs the same checks locally
(ruff/mypy/pytest) immediately before restarting the live bots, as a
backstop that doesn't depend on GitHub Actions or `gh` auth being
reachable from a background launchd process. See its module docstring for
the exact failure-handling behavior (alert-once-per-SHA, conditional safe
rollback).
```

- [ ] **Step 2: Extend the existing `auto_deploy.py` mention in the Architecture intro**

`CLAUDE.md:51` currently reads:

```markdown
The project has two long-running processes that share the `organist_bot` package. Both run under launchd (see `scripts/install-launchagent.sh`) and are auto-redeployed by `scripts/auto_deploy.py` on every push to `main`.
```

Change it to:

```markdown
The project has two long-running processes that share the `organist_bot` package. Both run under launchd (see `scripts/install-launchagent.sh`) and are auto-redeployed by `scripts/auto_deploy.py` on every push to `main` — but only after `auto_deploy.py` re-verifies lint/type/tests locally; see "Ship workflow" below for the full picture.
```

- [ ] **Step 3: Commit and push directly to `main`**

```bash
git add CLAUDE.md
git commit -m "docs: document the new ship workflow in CLAUDE.md

make ship usage, the branch/PR requirement now that direct-to-main
pushes are no longer how this repo ships, the self-installing git
hook, and how auto_deploy.py's local re-run gate fits into the
picture."
git push origin main
```

---

## Final verification

After Task 7:

- [ ] Run `gh api repos/seby-dev/organist_bot/branches/main/protection --jq '.required_status_checks.contexts'` — confirm `["Lint & type-check", "Tests"]` is still set.
- [ ] Run `EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com .venv/bin/pytest --tb=short -q` — confirm 0 failures.
- [ ] Run `make pre-push` — confirm it passes end to end (also re-confirms the git hook is wired).
- [ ] Run `gh run list --repo seby-dev/organist_bot --branch main --limit 1` — confirm the latest `main` commit's CI is green.
- [ ] Create one real feature branch and run `make ship` on a trivial change (e.g. this plan's own status update) to confirm the entire new pipeline works end to end in practice, not just in isolated smoke tests.
