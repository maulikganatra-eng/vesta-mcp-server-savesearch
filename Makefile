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
        hooks check-pins check-python-version secrets-baseline lock outdated clean

## setup: Fresh clone -> ready to work (venv, deps, git hooks, secrets baseline)
# Must work BEFORE a venv exists, so it cannot go through `uv run`.
# bootstrap.py uses only the stdlib. Tries python3 first (macOS/Linux, and
# Windows via WSL or the py-launcher's python3 alias), falls back to python
# (the name the official Windows installer actually puts on PATH).
setup:
	@command -v python3 >/dev/null 2>&1 && python3 scripts/bootstrap.py || python scripts/bootstrap.py

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

## test-cov: Fast suite with the coverage gate, terminal/HTML/XML reports
# XML is for CI's artifact upload; HTML is for local browsing. One target for
# both so the pre-commit pytest hook, this target and CI's `tests` job all run
# literally the same command — see the note on the pytest hook in
# .pre-commit-config.yaml.
test-cov:
	uv run --frozen pytest --cov --cov-report=term-missing --cov-report=html \
		--cov-report=xml -m "not contract and not e2e"
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

## check-pins: Verify tool versions agree between pyproject.toml and pre-commit
# Routed through `uv run` (project venv), not a bare `python3`/`python` — the
# script imports pyyaml/packaging, which only exist in the project venv, and
# `uv run` sidesteps the python3-vs-python naming problem entirely since uv
# manages its own interpreter rather than relying on what happens to be on PATH.
check-pins:
	uv run --frozen python scripts/check_tool_pins.py

## check-python-version: Verify .python-version agrees with ruff/mypy/Dockerfile
check-python-version:
	uv run --frozen python scripts/check_python_version.py

## secrets-baseline: Re-audit and rewrite .secrets.baseline after a false positive
# Delegates to scripts/secrets_baseline.py so the --exclude-files pattern is
# defined in exactly one place, shared with scripts/bootstrap.py's initial scan.
secrets-baseline:
	uv run --frozen python scripts/secrets_baseline.py .secrets.baseline
	@echo "Review the diff before committing — never baseline a real secret."

## lock: Re-resolve uv.lock after editing dependencies in pyproject.toml
lock:
	uv lock

## outdated: Show dependencies with newer releases available
outdated:
	uv tree --outdated

## clean: Remove every gitignored file except .venv (caches, coverage artefacts)
# Reads .gitignore via `git ls-files --ignored` instead of hand-duplicating its
# list here — a cache dir added to .gitignore later but not here would silently
# stop being cleaned, the exact class of drift this whole setup exists to avoid.
# `git clean -e` does NOT mean "exclude from cleaning" (it means the opposite:
# an extra pattern to also clean), so .venv is filtered out by hand instead.
clean:
	git ls-files --others --ignored --exclude-standard --directory -z \
		| grep -zv '^\.venv/$$' \
		| xargs -0 rm -rf --

## help: Show this message
help:
	@grep -E '^## ' Makefile | sed 's/## //' | awk -F': ' '{printf "  \033[36m%-18s\033[0m %s\n", $$1, $$2}'
