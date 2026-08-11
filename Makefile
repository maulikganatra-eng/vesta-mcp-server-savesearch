# =========================================================================
# Makefile — a thin, OPTIONAL convenience wrapper. Every target below just
# calls `uv run --frozen python scripts/tasks.py <name>`, which is the actual
# single source of truth for each command.
#
# Why the indirection: .pre-commit-config.yaml and ci.yml call scripts/tasks.py
# directly, with no `make` involved at all. GNU Make is not on a stock Windows
# machine's PATH (Python + Git for Windows only, no WSL/MinGW/choco make) —
# confirmed empirically, and it broke every commit and push the moment those
# hooks were changed to run `make <target>` instead of the command itself.
# Moving the one-source-of-truth job from this Makefile to scripts/tasks.py
# (plain Python, runs anywhere `uv` runs) keeps that property without
# requiring `make` anywhere on the automatic (hook/CI) path.
#
# If you don't have `make`, run the exact same commands directly:
#   uv run --frozen python scripts/tasks.py <task>
#
#   make            # same as `make help`
#   make setup      # one command, fresh clone to ready
# =========================================================================

.DEFAULT_GOAL := help
.PHONY: help setup lint format type-check test test-cov test-contract check \
        hooks check-pins check-python-version secrets-baseline lock outdated clean

## setup: Fresh clone -> ready to work (venv, deps, git hooks, secrets baseline)
# The one target that must work BEFORE a venv (and so `uv run`) exists.
# bootstrap.py uses only the stdlib. Tries python3 first (macOS/Linux, and
# Windows via WSL or the py-launcher's python3 alias), falls back to python
# (the name the official Windows installer actually puts on PATH).
setup:
	@command -v python3 >/dev/null 2>&1 && python3 scripts/bootstrap.py || python scripts/bootstrap.py

## lint: ruff check, no auto-fix (what CI runs)
lint:
	uv run --frozen python scripts/tasks.py lint

## format: Auto-fix lint violations and format
format:
	uv run --frozen python scripts/tasks.py format

## type-check: mypy, strict
type-check:
	uv run --frozen python scripts/tasks.py type-check

## test: Fast suite — excludes tests needing live credentials
test:
	uv run --frozen python scripts/tasks.py test

## test-cov: Fast suite with the coverage gate, terminal/HTML/XML reports
test-cov:
	uv run --frozen python scripts/tasks.py test-cov
	@echo "HTML report: htmlcov/index.html"

## test-contract: Live-service tests. Needs real credentials. Never runs in CI.
test-contract:
	uv run --frozen python scripts/tasks.py test-contract

## check: Everything CI runs, in CI's order. Run before opening a PR.
check:
	uv run --frozen python scripts/tasks.py check

## hooks: Run every pre-commit hook over the whole tree, including pre-push ones
hooks:
	uv run --frozen python scripts/tasks.py hooks

## check-pins: Verify tool versions agree between pyproject.toml and pre-commit
check-pins:
	uv run --frozen python scripts/tasks.py check-pins

## check-python-version: Verify .python-version agrees with ruff/mypy/Dockerfile
check-python-version:
	uv run --frozen python scripts/tasks.py check-python-version

## secrets-baseline: Re-audit and rewrite .secrets.baseline after a false positive
secrets-baseline:
	uv run --frozen python scripts/tasks.py secrets-baseline
	@echo "Review the diff before committing — never baseline a real secret."

## lock: Re-resolve uv.lock after editing dependencies in pyproject.toml
lock:
	uv run --frozen python scripts/tasks.py lock

## outdated: Show dependencies with newer releases available
outdated:
	uv run --frozen python scripts/tasks.py outdated

## clean: Remove every gitignored file except .venv (caches, coverage artefacts)
clean:
	uv run --frozen python scripts/tasks.py clean

## help: Show this message
help:
	@grep -E '^## ' Makefile | sed 's/## //' | awk -F': ' '{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
