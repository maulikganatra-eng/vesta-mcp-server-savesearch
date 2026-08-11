#!/usr/bin/env python3
"""Cross-platform task runner — the one place that defines every command this
repo runs, whether triggered by a git hook, by CI, or by a human typing `make`.

One deliberate exception: `setup`. It has to run before a virtualenv exists, so it
cannot go through `uv run`; the Makefile calls scripts/bootstrap.py directly for it.

Why this exists. Pre-commit hooks and CI must never depend on GNU Make: it is
not on PATH on a stock Windows machine (Python + Git for Windows only, no
WSL/MinGW/choco make) — confirmed empirically, and it broke every commit and
every push for exactly that reason, the moment the mypy/pytest/check-* hooks
were changed to run `make <target>` instead of spelling out the command.

But the reason those hooks were routed through one place at all still holds:
the exact mypy/pytest invocation used to be duplicated verbatim across
.pre-commit-config.yaml, ci.yml and the Makefile, and a future change to one
without the other two silently drifted. This script is that one place now,
written in Python so it runs identically on every OS `uv` runs on — which is
every OS we support, since `uv` is already a hard requirement (scripts/
bootstrap.py refuses to continue without it).

The Makefile is a thin, OPTIONAL convenience wrapper around this script for
people who like typing `make <target>` — nothing automatic depends on it
existing. .pre-commit-config.yaml and ci.yml both call this script directly,
via `uv run`, which — like this script itself — has no python3-vs-python
naming problem: `uv` is one binary with one name on every platform, and it
resolves its own interpreter rather than relying on whatever happens to be on
PATH under whatever name.

Task descriptions live here too, and `tasks.py help` renders them. The Makefile
deliberately does NOT carry its own copy of them: a duplicated description list
is the same drift problem as a duplicated command, and `make help` used to
generate its output with a grep/sed/awk pipeline, none of which is on a stock
Windows PATH either.

Usage:
    uv run --frozen python scripts/tasks.py <task>
    uv run --frozen python scripts/tasks.py help      # list every task
    make <task>                                       # same thing, via make

Add a task by adding one entry to TASKS (or COMPOSITE, for a task that is just
a sequence of other tasks). The description is part of the entry, so it cannot
be forgotten.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

# Markers whose tests hit live services and must never run in CI or in a git
# hook. Declared in [tool.pytest.ini_options] markers; tests/test_tooling.py
# asserts every name here is declared there.
#
# 🔴 Built into the `-m` expression once, deliberately. `-m` takes a string, and
# pytest does NOT error on an unknown marker name inside a `not` clause — `not
# typo` silently matches every test. So renaming a marker and missing one of the
# two copies this used to have would quietly start running the live-credential
# suite in CI and in the pre-push hook, with every check still green. That is
# the exact failure the marker split exists to prevent.
EXCLUDED_MARKERS = ("contract", "e2e")
_EXCLUDE_EXPR = " and ".join(f"not {m}" for m in EXCLUDED_MARKERS)

# name -> (description, [argv, ...]). The argv lists run in order; the first
# non-zero exit code stops the sequence and becomes this script's exit code.
TASKS: dict[str, tuple[str, list[list[str]]]] = {
    "lint": (
        "ruff check + format check, no auto-fix (what CI runs)",
        [
            ["uv", "run", "--frozen", "ruff", "check", "--no-fix", "."],
            ["uv", "run", "--frozen", "ruff", "format", "--check", "."],
        ],
    ),
    "format": (
        "Auto-fix lint violations and format",
        [
            ["uv", "run", "--frozen", "ruff", "check", "--fix", "."],
            ["uv", "run", "--frozen", "ruff", "format", "."],
        ],
    ),
    "type-check": (
        "mypy, strict",
        [["uv", "run", "--frozen", "mypy"]],
    ),
    "test": (
        "Fast suite — excludes tests needing live credentials",
        [["uv", "run", "--frozen", "pytest", "-m", _EXCLUDE_EXPR]],
    ),
    "test-cov": (
        "Fast suite with the coverage gate; terminal/HTML/XML reports",
        [
            [
                "uv",
                "run",
                "--frozen",
                "pytest",
                "--cov",
                "--cov-report=term-missing",
                "--cov-report=html",
                "--cov-report=xml",
                "-m",
                _EXCLUDE_EXPR,
            ],
        ],
    ),
    "test-contract": (
        "Live-service tests. Needs real credentials. Never runs in CI.",
        [["uv", "run", "--frozen", "pytest", "-m", "contract", "-v"]],
    ),
    "test-e2e": (
        "Full-stack end-to-end tests. Needs real credentials. Never runs in CI.",
        [["uv", "run", "--frozen", "pytest", "-m", "e2e", "-v"]],
    ),
    "audit": (
        "Report known CVEs in the locked dependency set (what CI's audit job runs)",
        [
            # Two steps: export the lock to a fully-pinned requirements file, then
            # audit that. Auditing the lock rather than pyproject's loose ranges is
            # the point — it reports what would actually be installed.
            #
            # --no-emit-project omits the `-e .` line; without it pip-audit tries to
            # BUILD this project rather than audit anything.
            #
            # pip-audit is a dev dependency rather than `uvx pip-audit` so its
            # version comes from uv.lock (and Dependabot) instead of being whatever
            # PyPI served that minute — an unpinned third-party tool executing in CI
            # would sit oddly in a repo whose whole thesis is enforced pins.
            # `uv export` is a uv subcommand operating on the lock file, not
            # something that needs to run inside the project venv — unlike every
            # other command here, it must NOT go through `uv run`. `uv run --frozen
            # uv export` would spawn a `uv run` process purely to re-invoke `uv`
            # itself for no benefit; the meaningful `--frozen` (which tells `export`
            # to use the lock file as-is) is the one already passed to `export`
            # below.
            [
                "uv",
                "export",
                "--frozen",
                "--no-emit-project",
                "--format",
                "requirements-txt",
                "-o",
                "requirements.audit.txt",
            ],
            [
                "uv",
                "run",
                "--frozen",
                "pip-audit",
                "--requirement",
                "requirements.audit.txt",
                # The export is already fully pinned and hashed, so there is nothing
                # to resolve; --no-deps skips building a throwaway environment to
                # re-resolve it, which is both faster and avoids a local ensurepip
                # failure on macOS.
                "--no-deps",
            ],
        ],
    ),
    "check-pins": (
        "Verify tool versions agree between pyproject.toml and pre-commit",
        [["uv", "run", "--frozen", "python", "scripts/check_tool_pins.py"]],
    ),
    "check-python-version": (
        "Verify .python-version agrees with ruff/mypy/Dockerfile, and satisfies requires-python",
        [["uv", "run", "--frozen", "python", "scripts/check_python_version.py"]],
    ),
    "secrets-baseline": (
        "Re-scan and update .secrets.baseline, preserving audited entries",
        [
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "scripts/secrets_baseline.py",
                ".secrets.baseline",
            ],
        ],
    ),
    "secrets-audit": (
        "Interactively audit .secrets.baseline — the only thing that sets is_secret",
        [
            [
                "uv",
                "run",
                "--frozen",
                "python",
                "scripts/secrets_baseline.py",
                "--audit",
                ".secrets.baseline",
            ],
        ],
    ),
    "hooks-commit": (
        "Run the fast (pre-commit stage) hooks over the whole tree",
        [
            [
                "uv",
                "run",
                "--frozen",
                "pre-commit",
                "run",
                "--all-files",
                "--hook-stage",
                "pre-commit",
            ],
        ],
    ),
    "hooks-push": (
        "Run the slow (pre-push stage) hooks over the whole tree",
        [
            [
                "uv",
                "run",
                "--frozen",
                "pre-commit",
                "run",
                "--all-files",
                "--hook-stage",
                "pre-push",
            ],
        ],
    ),
    "lock": (
        "Re-resolve uv.lock after editing dependencies in pyproject.toml",
        [["uv", "lock"]],
    ),
    "outdated": (
        "Show dependencies with newer releases available",
        [["uv", "tree", "--outdated"]],
    ),
}

# A composite task is a sequence of other task names, run in order.
COMPOSITE: dict[str, tuple[str, list[str]]] = {
    # The three BLOCKING CI jobs. Deliberately not the `audit` job, which is
    # advisory in CI and needs network access — run `audit` on its own when you
    # want it. There is no "CI's order" to mirror: ci.yml has no `needs:` chains,
    # so its jobs run concurrently.
    "check": (
        "The three blocking CI jobs (fast hooks, mypy, tests + coverage)",
        ["hooks-commit", "type-check", "test-cov"],
    ),
    "hooks": (
        "Every hook, both stages",
        ["hooks-commit", "hooks-push"],
    ),
}


# Never deleted by `clean`, even though git reports them as ignored.
#
# 🔴 This list is why `clean` is not simply "delete everything gitignored".
# .gitignore covers two very different kinds of file: regenerable artefacts
# (caches, coverage output) and IRREPLACEABLE local state (.env, the virtualenv).
# An earlier version spared only the literal ".venv", so `make clean` — a target
# documented as removing "caches, coverage artefacts" — silently deleted a
# developer's .env. Ignored files are by definition not in git, so that is
# unrecoverable.
#
# Matched against the FIRST PATH SEGMENT, so `.env` also protects nothing else by
# accident, while `.venv` protects `.venv/lib/...` if git ever reports it
# per-file rather than collapsed.
_KEEP_EXACT = frozenset(
    {
        ".venv",
        "venv",
        "env",
        "ENV",
        "env.bak",
        "venv.bak",  # virtualenvs
        ".env",
        ".envrc",
        ".direnv",  # local secrets/config
        ".idea",
        ".vscode",  # local editor state
    }
)
# Prefixes, so `.env.local` and `.venv-3.12` are covered too.
_KEEP_PREFIXES = (".env.", ".venv")


def _keep(entry: str) -> bool:
    parts = Path(entry.rstrip("/")).parts
    if not parts:
        return True
    head = parts[0]
    return head in _KEEP_EXACT or head.startswith(_KEEP_PREFIXES)


def _clean() -> int:
    """Remove regenerable gitignored files, sparing local state (see _KEEP_EXACT).

    Reimplemented in pure Python rather than the `git clean -X` / `grep` /
    `xargs` pipeline this replaced: `-e` on `git clean` does not mean "exclude
    from cleaning" (it means the opposite — an extra pattern to *also* clean),
    and grep/xargs are not on a stock Windows PATH any more than `make` is.
    `git ls-files --ignored` plus this script's own filesystem calls need
    nothing beyond git and Python, which every contributor already has.
    """
    try:
        proc = subprocess.run(
            ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )
    except FileNotFoundError:
        print("clean: git is not on PATH", file=sys.stderr)
        return 1
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr, end="")
        return proc.returncode

    removed = 0
    failed: list[str] = []
    for entry in filter(None, proc.stdout.split("\0")):
        if _keep(entry):
            continue
        path = ROOT / entry
        try:
            # is_dir() follows symlinks, so is_symlink() must be checked first —
            # otherwise a symlink to a directory has its TARGET's contents
            # removed instead of the link.
            if path.is_symlink():
                path.unlink(missing_ok=True)
            elif path.is_dir():
                # Not ignore_errors=True: that swallowed every failure and this
                # function still returned 0, so `clean` reported success having
                # removed nothing (reproduced with a chmod 500 directory).
                shutil.rmtree(path)
            else:
                path.unlink(missing_ok=True)
            removed += 1
        except OSError as exc:
            # Routine on Windows, where an editor or language server holds
            # .coverage / .mypy_cache open. Report and keep going rather than
            # aborting part-way through with a traceback.
            failed.append(f"{entry}: {exc.strerror or exc}")

    print(f"clean: removed {removed} path(s)")
    if failed:
        print("clean: could not remove:", file=sys.stderr)
        for f in failed:
            print(f"  {f}", file=sys.stderr)
        return 1
    return 0


def _help() -> int:
    """Print every task and its description, widest name setting the column."""
    rows: list[tuple[str, str]] = [
        *((name, desc) for name, (desc, _) in TASKS.items()),
        *((name, desc) for name, (desc, _) in COMPOSITE.items()),
        *((name, desc) for name, (desc, _) in PY_TASKS.items()),
    ]
    width = max(len(name) for name, _ in rows)
    print("usage: uv run --frozen python scripts/tasks.py <task>\n")
    for name, desc in sorted(rows):
        print(f"  {name.ljust(width)}  {desc}")
    return 0


# Tasks implemented in this script rather than as a subprocess.
PY_TASKS: dict[str, tuple[str, Callable[[], int]]] = {
    "clean": (
        "Remove every gitignored file except .venv (caches, coverage artefacts)",
        _clean,
    ),
    "help": (
        "List every task",
        _help,
    ),
}


def run_task(name: str, _seen: frozenset[str] = frozenset()) -> int:
    """Run one task by name. `_seen` guards against a composite cycle.

    A composite referring to itself (directly or through another composite)
    would otherwise recurse until the interpreter's stack limit and die with a
    RecursionError traceback instead of a usable message.
    """
    if name in _seen:
        chain = " -> ".join([*_seen, name])
        print(f"composite task cycle: {chain}", file=sys.stderr)
        return 2

    if name in PY_TASKS:
        return PY_TASKS[name][1]()

    if name in COMPOSITE:
        for sub in COMPOSITE[name][1]:
            rc = run_task(sub, _seen | {name})
            if rc != 0:
                return rc
        return 0

    for cmd in TASKS[name][1]:
        rc = subprocess.run(cmd, cwd=ROOT, check=False).returncode
        if rc != 0:
            return rc
    return 0


def main(argv: list[str]) -> int:
    known = {*TASKS, *COMPOSITE, *PY_TASKS}
    # A name defined in two registries would run whichever run_task() checks
    # first and silently ignore the other — fail loudly instead. This is a
    # repo-authoring mistake, not a user error, so it is checked on every call
    # rather than only in a test.
    duplicates = sorted(n for n in known if (n in TASKS) + (n in COMPOSITE) + (n in PY_TASKS) > 1)
    if duplicates:
        print(f"task name defined more than once: {', '.join(duplicates)}", file=sys.stderr)
        return 2

    # A composite naming a task that does not exist would otherwise surface as a
    # bare KeyError traceback from run_task, at the moment someone runs it.
    dangling = sorted(
        f"{name} -> {sub}" for name, (_, subs) in COMPOSITE.items() for sub in subs if sub not in known
    )
    if dangling:
        print(f"composite task refers to an unknown task: {', '.join(dangling)}", file=sys.stderr)
        return 2

    if not argv:
        return _help()
    if len(argv) != 1 or argv[0] not in known:
        print(f"unknown task: {' '.join(argv) or '(none)'}\n", file=sys.stderr)
        _help()
        return 2
    return run_task(argv[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
