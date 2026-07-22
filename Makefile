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
	EMAIL_SENDER=ci@test.com EMAIL_PASSWORD=x CC_EMAIL=ci@test.com $(VENV)/pytest --tb=short -q

# Rather than only documenting that core.hooksPath needs setting, install it
# automatically the first time anyone runs the checks that matter.
_ensure-hooks:
	@git config core.hooksPath >/dev/null 2>&1 || git config core.hooksPath .githooks

pre-push: _ensure-hooks lint format-check type-check security test
	@echo "All pre-push checks passed."

ship: pre-push
	@./scripts/ship.sh
