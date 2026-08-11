#!/usr/bin/env python3
"""Cross-platform task runner — the one place that defines every command this
repo runs, whether triggered by a git hook, by CI, or by a human typing `make`.

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

Usage:
    uv run --frozen python scripts/tasks.py <task>
    make <task>                                     # same thing, via make

Add a task by adding one entry to TASKS (or COMPOSITE, for a task that is
just a sequence of other tasks).
"""

from __future__ import annotations

from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

# Each value is a list of argv lists, run in order; the first non-zero exit
# code stops the sequence and becomes this script's exit code.
TASKS: dict[str, list[list[str]]] = {
    "lint": [
        ["uv", "run", "--frozen", "ruff", "check", "--no-fix", "."],
        ["uv", "run", "--frozen", "ruff", "format", "--check", "."],
    ],
    "format": [
        ["uv", "run", "--frozen", "ruff", "check", "--fix", "."],
        ["uv", "run", "--frozen", "ruff", "format", "."],
    ],
    "type-check": [
        ["uv", "run", "--frozen", "mypy"],
    ],
    "test": [
        ["uv", "run", "--frozen", "pytest", "-m", "not contract and not e2e"],
    ],
    "test-cov": [
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
            "not contract and not e2e",
        ],
    ],
    "test-contract": [
        ["uv", "run", "--frozen", "pytest", "-m", "contract", "-v"],
    ],
    "check-pins": [
        ["uv", "run", "--frozen", "python", "scripts/check_tool_pins.py"],
    ],
    "check-python-version": [
        ["uv", "run", "--frozen", "python", "scripts/check_python_version.py"],
    ],
    "secrets-baseline": [
        ["uv", "run", "--frozen", "python", "scripts/secrets_baseline.py", ".secrets.baseline"],
    ],
    "lock": [
        ["uv", "lock"],
    ],
    "outdated": [
        ["uv", "tree", "--outdated"],
    ],
    "hooks": [
        ["uv", "run", "--frozen", "pre-commit", "run", "--all-files", "--hook-stage", "pre-commit"],
        ["uv", "run", "--frozen", "pre-commit", "run", "--all-files", "--hook-stage", "pre-push"],
    ],
}

# A composite task is just a sequence of other task names, run in order.
COMPOSITE: dict[str, list[str]] = {
    "check": ["lint", "type-check", "test-cov"],
}


def _run_argv(cmd: list[str]) -> int:
    return subprocess.run(cmd, cwd=ROOT, check=False).returncode


def _clean() -> int:
    """Remove every gitignored file except .venv.

    Reimplemented in pure Python rather than the `git clean -X` / `grep` /
    `xargs` pipeline this replaced: `-e` on `git clean` does not mean "exclude
    from cleaning" (it means the opposite — an extra pattern to *also* clean),
    and grep/xargs are not on a stock Windows PATH any more than `make` is.
    `git ls-files --ignored` plus this script's own filesystem calls need
    nothing beyond git and Python, which every contributor already has.
    """
    proc = subprocess.run(
        ["git", "ls-files", "--others", "--ignored", "--exclude-standard", "--directory", "-z"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )
    if proc.returncode != 0:
        print(proc.stderr, file=sys.stderr, end="")
        return proc.returncode

    for entry in filter(None, proc.stdout.split("\0")):
        if entry.rstrip("/") == ".venv":
            continue
        path = ROOT / entry
        if path.is_dir():
            shutil.rmtree(path, ignore_errors=True)
        elif path.exists():
            path.unlink()
    return 0


def run_task(name: str) -> int:
    if name == "clean":
        return _clean()
    if name in COMPOSITE:
        for sub in COMPOSITE[name]:
            rc = run_task(sub)
            if rc != 0:
                return rc
        return 0
    for cmd in TASKS[name]:
        rc = _run_argv(cmd)
        if rc != 0:
            return rc
    return 0


def main(argv: list[str]) -> int:
    known = sorted({*TASKS, *COMPOSITE, "clean"})
    if len(argv) != 1 or argv[0] not in known:
        print(f"usage: tasks.py <task>\n  known tasks: {', '.join(known)}", file=sys.stderr)
        return 2
    return run_task(argv[0])


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
