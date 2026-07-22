# Ship Workflow — Design Spec

**Date:** 2026-07-22
**Status:** Approved

## Problem

organist_bot has no real safety net between "code is written" and "code is running live on the Mac":

- `.github/workflows/ci.yml` exists but has failed on **every single run for months** (4 pre-existing test failures — `TestCalendarFilterCompeting` × 3 in `tests/test_filters.py`, `TestNegDrafts::test_neg_gig_is_recorded_as_pending_and_alerts_telegram` in `tests/test_main.py`). Nothing is gated on it, so a permanently-red CI has gone unnoticed.
- `main` has no branch protection (`404 Branch not protected`).
- Deploy is `scripts/auto_deploy.py`, polled every 60s via `com.organistbot.autodeploy` (launchd). It fast-forward-merges `origin/main` and restarts the bots on **any** new commit, whether or not that commit is broken.
- Every commit this session went straight to `main` with a direct push — no branch, no PR — despite the user's global CLAUDE.md documenting a branch → PR → auto-merge workflow.
- There is no local pre-push quality gate at all.

The sibling project (`~/Developer/pmp-project`) has a more mature "ship" workflow worth partially adopting: `make ship` (pre-push quality gate → push), GitHub Actions CI, and a deploy step gated on CI via a self-hosted runner. Investigating it surfaced that PMP's own `.githooks/pre-push` is never actually wired up (no `core.hooksPath`, nothing in `.git/hooks/`) — `make ship` is the only thing that actually enforces it there.

## Goal

Make it structurally impossible for broken code to reach the live bots, using pieces that build on what organist_bot already has (launchd-polled `auto_deploy.py`) rather than adding a new self-hosted GitHub Actions runner:

1. Fix the 4 pre-existing test failures so CI can be trusted.
2. A local pre-push gate that can't be silently skipped (unlike PMP's).
3. CI modernized to match the project's actual `uv`-based workflow.
4. Branch protection so PR auto-merge genuinely waits for green CI.
5. The actual deploy step re-verifies the code locally before restarting anything live.
6. Security scanning (bandit + semgrep), matching PMP, local-only (not in CI, matching PMP's own setup).

Going forward, both the user and Claude Code ship changes via feature branch → `make ship` → PR → auto-merge, not direct pushes to `main`.

## Approach

Three independent gates, each cheap and each catching a different failure mode:

| Gate | Where | Catches |
|---|---|---|
| Pre-push hook | Developer's Mac, before `git push` | Fast local feedback, can't be forgotten |
| CI | GitHub-hosted runner, on every push/PR | Same checks, visible in GitHub UI, required for merge |
| Deploy-time re-run | `auto_deploy.py`, on the production Mac, before restarting bots | The actual hard stop — doesn't trust CI/GitHub reachability, doesn't need a self-hosted runner |

Considered gating deploy on GitHub's CI conclusion via `gh api` (matching PMP's `workflow_run` + self-hosted-runner model more literally) but rejected it: it adds a dependency on `gh` auth working from a non-interactive launchd process and on GitHub API reachability, plus polling logic to wait for CI to conclude (~15-40s lag). Re-running the same checks locally is self-contained, deterministic, and the whole suite takes ~15s — cheap enough to just do every time a new commit is detected.

---

## Design

### 1. Fix the 4 pre-existing test failures

Root cause not yet diagnosed — done via `superpowers:systematic-debugging` during implementation. Must land and be verified green in CI **before** branch protection or the deploy-time gate are enabled, or every gate added here would permanently block all future deploys.

### 2. Local pre-push gate

**`Makefile`** (new, project root):

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

# Rather than only documenting that core.hooksPath needs setting (PMP's gap —
# its .githooks/pre-push is never actually wired up), install it automatically
# the first time anyone runs the checks that matter.
_ensure-hooks:
	@git config core.hooksPath >/dev/null 2>&1 || git config core.hooksPath .githooks

pre-push: _ensure-hooks lint format-check type-check security test
	@echo "All pre-push checks passed."

ship: pre-push
	@./scripts/ship.sh
```

**`.githooks/pre-push`** (new, executable): identical checks to `make pre-push`, run automatically on every `git push` — including a push that didn't go through `make ship` — so it can't be silently bypassed the way PMP's currently is.

**`scripts/ship.sh`** (new): automates this repo's already-documented PR workflow (CLAUDE.md's "Pull request workflow" section — ready-for-review, squash auto-merge) end to end, for both the user and Claude:

```bash
#!/usr/bin/env bash
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

`make ship` refuses on `main` (pre-push's checks already ran as a `ship` prerequisite before `ship.sh` even executes).

### 3. Modernize CI (`.github/workflows/ci.yml`)

Currently installs deps via `pip install -r requirements-dev.txt` — a hand-maintained shadow of `pyproject.toml`'s `[project.optional-dependencies] dev` that can silently drift (already has: e.g. `bandit` will need adding to both if this isn't fixed). Switch both jobs to:

```yaml
- name: Install uv
  uses: astral-sh/setup-uv@v4
  with:
    enable-cache: true

- name: Install dependencies
  run: uv sync --extra dev
```

Then `uv run ruff check .` / `uv run ruff format --check .` / `uv run mypy organist_bot/` / `uv run pytest --tb=short -q` in place of the bare commands. Delete `requirements.txt` and `requirements-dev.txt` (no longer referenced anywhere). No Playwright browser install needed — the one Playwright-touching test (`tests/test_invoice_generator_browser.py`) fully mocks `async_playwright`, confirmed by inspection. Job names (`Lint & type-check`, `Tests`) stay as-is — branch protection references them by name. Security scanning is **not** added to CI, matching PMP's actual setup (bandit/semgrep are pre-push-only there too).

### 4. Branch protection on `main`

Require status checks `Lint & type-check` and `Tests` to pass before merging. Configured via `gh api repos/seby-dev/organist_bot/branches/main/protection` (or the GitHub UI). Turned on **after** step 1 lands and CI is confirmed green — turning it on while CI is still red would permanently block all merges.

### 5. Deploy-time local re-run gate (`scripts/auto_deploy.py`)

After the existing `git merge --ff-only origin/main` succeeds, before `uv sync` + restarting the bots:

```python
VENV_BIN = REPO / ".venv" / "bin"
FAILED_SHA_FILE = REPO / "data" / "last_failed_deploy_sha.txt"

CHECKS = [
    ([str(VENV_BIN / "ruff"), "check", "."], "ruff check"),
    ([str(VENV_BIN / "ruff"), "format", "--check", "."], "ruff format --check"),
    ([str(VENV_BIN / "mypy"), "organist_bot/"], "mypy"),
    ([str(VENV_BIN / "pytest"), "--tb=short", "-q"], "pytest"),
]

def _run_checks() -> str | None:
    """None if everything passes; otherwise a short failure summary."""
    for cmd, label in CHECKS:
        result = run(cmd, cwd=REPO, capture_output=True, text=True)
        if result.returncode != 0:
            return f"{label} failed:\n{(result.stdout + result.stderr)[-1500:]}"
    return None
```

No dummy env vars needed — `Settings()` loads the real `.env` already present in `REPO`, same as the live bots.

Sequencing after a successful `merge --ff-only`:

1. `uv sync --extra dev` (installs/refreshes bandit etc. too — reuses the fix from earlier today that keeps dev deps in the shared venv).
2. Run `_run_checks()`.
3. **On failure**: don't restart the bots (old processes keep running their old in-memory code). Alert via a **standalone** Telegram notification (see below) — but only once per distinct failing SHA (tracked in `FAILED_SHA_FILE`), so a stuck failure doesn't spam every 60s. If `SHA_FILE` exists (a prior successful deploy to roll back to) **and** `git status --porcelain` is empty (nothing uncommitted to protect), reset the working tree back to that SHA — keeps disk state at a known-good commit so an unrelated crash-restart can't accidentally pick up untested code. If the tree is dirty (someone's mid-edit) or there's no prior deploy yet (fresh checkout, nothing safe to roll back to), skip the reset and leave the merged code in place — never touch uncommitted work, matching this session's established rule. Don't update `SHA_FILE`, so the same broken SHA is retried (self-healing if it was transient) without re-alerting.
4. **On success**: proceed exactly as today — `launchctl bootout`/`bootstrap` the scheduler + telegram plists, write `SHA_FILE`, clear `FAILED_SHA_FILE` if present.

**Standalone alert helper** — deliberately does *not* import `organist_bot.alert` or `organist_bot.config`, so a broken deploy can never take down its own failure-reporting path:

```python
def _send_alert(message: str) -> None:
    try:
        from dotenv import dotenv_values
        import requests
        env = dotenv_values(REPO / ".env")
        token, chat_id = env.get("TELEGRAM_BOT_TOKEN"), env.get("TELEGRAM_CHAT_ID")
        if token and chat_id:
            requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": message},
                timeout=10,
            )
    except Exception as exc:
        print(f"[{ts()}] alert failed: {exc}")  # best-effort; never crash the deploy script
```

### 6. Security scanning — bandit + semgrep, pre-push only

Add to `pyproject.toml`:

```toml
dev = [
    "pytest>=9.0.2",
    "pytest-mock>=3.14",
    "pytest-asyncio>=0.23",
    "mypy>=1.0",
    "ruff>=0.4",
    "types-requests",
    "bandit>=1.7",
]

[tool.bandit]
exclude_dirs = ["tests", ".venv"]
```

`make security` / the pre-push hook run bandit unconditionally and semgrep only `if command -v semgrep` (not currently installed on this Mac — skips gracefully, identical to PMP's actual behavior today).

### 7. Documentation

Update organist_bot's `CLAUDE.md`:
- New "Ship workflow" section: `make ship` usage, the branch/PR requirement, one-time note that `core.hooksPath` self-installs on first `make pre-push`/`ship`.
- Note in the pipeline/deploy description that `auto_deploy.py` now re-verifies lint/type/tests locally before restarting, and alerts (once per SHA) on failure.
- Add `data/last_failed_deploy_sha.txt` to the Data files table.

---

## Error Handling

- `_run_checks()` failure → bots keep running old code; Telegram alert (deduped by SHA); safe conditional working-tree rollback only when clean; retried every tick until a new commit supersedes the broken one.
- `_send_alert()` exceptions are caught and logged to `autodeploy.log`, never raised — the deploy script's own robustness must not depend on Telegram being reachable.
- `scripts/ship.sh` exits non-zero (and doesn't push) if run from `main`, or if any pre-push check fails (via `make`'s dependency chain) — no partial pushes.
- Branch protection is enabled only after CI is confirmed green (step 1 verified before step 4), to avoid permanently locking merges.

## Testing

- `scripts/auto_deploy.py`: unit-testable pieces are `_run_checks()` (mock `subprocess.run` for pass/fail per check) and the SHA-dedup logic for `FAILED_SHA_FILE` (first failure alerts + writes; repeat failure of the same SHA doesn't re-alert; a new SHA re-alerts). The working-tree-rollback conditional (clean vs dirty) is tested against a temp git repo fixture.
- `.githooks/pre-push`: manually verified end to end (a shell script, not unit-testable in the pytest sense) — confirm it actually blocks a push when a check fails, and that `core.hooksPath` auto-installs from a fresh clone.
- CI change: verified by pushing a branch and confirming the `uv sync --extra dev` based jobs pass identically to the current `pip`-based ones.
- The 4 pre-existing test fixes get their own targeted tests/fixes per whatever `systematic-debugging` finds.

## Files Changed

| File | Change |
|---|---|
| `tests/test_filters.py`, `tests/test_main.py`, and whichever source file(s) systematic-debugging identifies | Fix the 4 pre-existing failures |
| `Makefile` | New — lint/format/format-check/type-check/security/test/pre-push/ship targets |
| `.githooks/pre-push` | New — same checks, real git hook |
| `scripts/ship.sh` | New — push, create PR, enable auto-merge |
| `.github/workflows/ci.yml` | Switch to `uv sync --extra dev`; delete pip-based install |
| `requirements.txt`, `requirements-dev.txt` | Deleted (superseded by `pyproject.toml` + `uv`) |
| `pyproject.toml` | Add `bandit>=1.7` to `dev` extra; add `[tool.bandit]` |
| `scripts/auto_deploy.py` | Add `_run_checks()`, `_send_alert()`, `FAILED_SHA_FILE`, conditional safe rollback |
| GitHub repo settings (`main` branch protection) | Require `Lint & type-check` + `Tests` status checks |
| `CLAUDE.md` | Document ship workflow, hook self-install, deploy-gate behavior, new data file |

## Out of Scope

- A new self-hosted GitHub Actions runner / `workflow_run`-triggered deploy (PMP's model) — explicitly rejected in favor of the local re-run gate.
- `make verify` (PMP has a `scripts/verify.sh` post-deploy smoke check) — no clear organist_bot analog exists yet (no HTTP health endpoint); can be added later as its own change if wanted.
- Retroactively converting past commits into the branch/PR flow — this only changes how *future* work ships.
- Rolling back the *running* bot processes if a crash-restart ever does pick up untested code between the merge and the check completing — the design minimizes this window (checks run immediately after merge, working tree is rolled back on failure when safe) but doesn't eliminate it entirely; treated as an accepted, narrow, pre-existing risk rather than solved here.
