# =========================================================================
# Makefile — project-agnostic. No source path list, no module list, no coverage
# threshold. Every target delegates to a tool that reads pyproject.toml.
#
# This is the difference from our older repos, where SOURCES and COV_MODULES are
# spelled out here AND in ci.yml AND in .pre-commit-config.yaml. Adding a
# directory there means editing three files; here it means editing none.
#
#   make            # same as `make help`
#   make setup      # one command, fresh clone to ready
# =========================================================================

.DEFAULT_GOAL := help
.PHONY: help setup lint format type-check test test-cov test-contract check \
        hooks secrets-baseline lock outdated clean

## setup: Fresh clone -> ready to work (venv, deps, git hooks, secrets baseline)
# Plain python3 on purpose: this is the one target that must work BEFORE a venv
# exists, so it cannot go through `uv run`. bootstrap.py uses only the stdlib.
setup:
	python3 scripts/bootstrap.py

## lint: ruff check, no auto-fix (what CI runs)
lint:
	uv run --frozen ruff check --no-fix .
	uv run --frozen ruff format --check .

## format: Auto-fix lint violations and format
format:
	uv run --frozen ruff check --fix .
	uv run --frozen ruff format .

## type-check: mypy, strict
type-check:
	uv run --frozen mypy

## test: Fast suite — excludes tests needing live credentials
test:
	uv run --frozen pytest -m "not contract and not e2e"

## test-cov: Fast suite with the coverage gate and an HTML report
test-cov:
	uv run --frozen pytest --cov --cov-report=term-missing --cov-report=html \
		-m "not contract and not e2e"
	@echo "HTML report: htmlcov/index.html"

## test-contract: Live-service tests. Needs real credentials. Never runs in CI.
test-contract:
	uv run --frozen pytest -m contract -v

## check: Everything CI runs, in CI's order. Run before opening a PR.
check: lint type-check test-cov

## hooks: Run every pre-commit hook over the whole tree, including pre-push ones
hooks:
	uv run --frozen pre-commit run --all-files --hook-stage pre-commit
	uv run --frozen pre-commit run --all-files --hook-stage pre-push

## secrets-baseline: Re-audit and rewrite .secrets.baseline after a false positive
secrets-baseline:
	uv run --frozen detect-secrets scan --baseline .secrets.baseline
	@echo "Review the diff before committing — never baseline a real secret."

## lock: Re-resolve uv.lock after editing dependencies in pyproject.toml
lock:
	uv lock

## outdated: Show dependencies with newer releases available
outdated:
	uv tree --outdated

## clean: Remove caches and coverage artefacts
clean:
	find . -type d -name __pycache__ -not -path './.venv/*' -exec rm -rf {} + 2>/dev/null || true
	rm -rf htmlcov .coverage coverage.xml requirements.audit.txt \
	       .pytest_cache .ruff_cache .mypy_cache

## help: Show this message
help:
	@grep -E '^## ' Makefile | sed 's/## //' | awk -F': ' '{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
