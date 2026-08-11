# =========================================================================
# Makefile — a thin, OPTIONAL convenience wrapper. Every target below just
# calls `uv run --frozen python scripts/tasks.py <name>`, which is the actual
# single source of truth for each command AND for each command's description.
# `setup` is the one exception, for the reason given at that target.
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
#   uv run --frozen python scripts/tasks.py help     # list every task
#
# NOTE: this file deliberately carries no task DESCRIPTIONS and no help-text
# generator. `make help` forwards to `tasks.py help`, which renders the
# descriptions stored beside each task definition. The previous version built
# its help output with a grep/sed/awk pipeline — none of which is on a stock
# Windows PATH either — and kept a second copy of every description in `## `
# comments that could drift from the real ones.
#
# TASKS below must match scripts/tasks.py's registry; tests/test_toolchain.py
# asserts that it does, so a task added to one and not the other fails the suite
# rather than being discovered by someone typing `make` and getting
# "No rule to make target".
# =========================================================================

TASKS := lint format type-check test test-cov test-contract test-e2e check \
         hooks hooks-commit hooks-push check-pins check-python-version \
         secrets-baseline secrets-audit audit lock outdated clean help

.DEFAULT_GOAL := help
.PHONY: setup $(TASKS)

# The one target that must work BEFORE a venv (and therefore `uv run`) exists,
# so it is the only one that cannot go through tasks.py. bootstrap.py uses only
# the standard library.
#
# Prefers python3 (macOS/Linux, and Windows via WSL or the py-launcher alias)
# and falls back to python (the name the official Windows installer puts on
# PATH). Written as if/else, NOT `command -v python3 && python3 ... || python
# ...`: in that form the `||` branch also fires when bootstrap.py itself exits
# non-zero, so a genuine failure (e.g. uv missing — the very first thing
# bootstrap checks) would silently re-run the whole script under a second
# interpreter and bury the real error under a confusing second one.
setup:
	@if command -v python3 >/dev/null 2>&1; then \
	  python3 scripts/bootstrap.py; \
	else \
	  python scripts/bootstrap.py; \
	fi

# One rule for every task — no per-target boilerplate to keep in step.
$(TASKS):
	@uv run --frozen python scripts/tasks.py $@
