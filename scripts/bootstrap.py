#!/usr/bin/env python3
"""Fresh clone to ready-to-work, in one command.

Run either of these — they do the same thing:

    make setup
    uv run python scripts/bootstrap.py

Idempotent: safe to re-run any time. Each step reports whether it did work or
found the job already done.

Why this exists: the setup steps for these repos are "uv sync", "pre-commit
install", "generate a secrets baseline" — and the third one is the one everybody
forgets. Without a baseline the detect-secrets hook fails on the very first
commit with an error that reads like a bug in the hook rather than a missing
file. A new team member hits that on day one.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
import shutil
import subprocess
import sys

import secrets_baseline

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".secrets.baseline"

OK, WARN, FAIL, DIM = "\033[32m✓\033[0m", "\033[33m!\033[0m", "\033[31m✗\033[0m", "\033[2m"
RESET = "\033[0m"


def run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, check=False, capture_output=capture)


def _step_uv_present() -> bool:
    if shutil.which("uv") is None:
        print(f"  {FAIL} uv is not on PATH.")
        print("     Install it:  curl -LsSf https://astral.sh/uv/install.sh | sh")
        print("     Then re-run this script.")
        return False
    version = run(["uv", "--version"], capture=True).stdout.strip()
    print(f"  {OK} {version}")
    return True


def _step_dependencies() -> bool:
    if run(["uv", "sync"]).returncode != 0:
        print(f"  {FAIL} uv sync failed — fix the error above, then re-run.")
        return False
    print(f"  {OK} .venv is in sync with uv.lock")
    return True


def _step_secrets_baseline() -> bool:
    if BASELINE.exists():
        print(f"  {OK} .secrets.baseline already present")
        return True

    proc = run(secrets_baseline.build_scan_command(), capture=True)
    if proc.returncode == 0 and proc.stdout.strip():
        BASELINE.write_text(proc.stdout)
        print(f"  {OK} created .secrets.baseline")
        print(
            f"     {DIM}Commit it. Re-run `make secrets-baseline` after an audited"
            f" false positive.{RESET}"
        )
        return True

    print(f"  {WARN} could not generate a baseline (detect-secrets not installed yet?)")
    print("     Run `make secrets-baseline` once dependencies are installed.")
    return False


def _step_git_hooks() -> bool:
    # default_install_hook_types in .pre-commit-config.yaml means one command
    # installs both. --install-hooks pre-builds the environments so the first
    # real commit is not a two-minute wait.
    if run(["uv", "run", "pre-commit", "install", "--install-hooks"]).returncode != 0:
        print(f"  {FAIL} pre-commit install failed")
        return False

    hooks = ROOT / ".git" / "hooks"
    installed = [n for n in ("pre-commit", "pre-push") if (hooks / n).exists()]
    print(f"  {OK} installed: {', '.join(installed) or 'none found'}")
    if "pre-push" not in installed:
        print(f"  {WARN} pre-push hook missing — tests will not run before push.")
        print("     Check default_install_hook_types in .pre-commit-config.yaml")
        return False
    return True


def _step_toolchain_runs() -> bool:
    ok = True
    for label, cmd in (
        ("ruff", ["uv", "run", "ruff", "--version"]),
        ("mypy", ["uv", "run", "mypy", "--version"]),
        ("pytest", ["uv", "run", "pytest", "--version"]),
    ):
        proc = run(cmd, capture=True)
        if proc.returncode != 0:
            print(f"  {FAIL} {label} would not run")
            ok = False
            continue
        # A tool can exit 0 with its version banner on stderr rather than
        # stdout (or with no output at all), so .stdout.splitlines()[0] alone
        # can IndexError here. Fall back rather than crash the whole script
        # over a cosmetic status line.
        text = (proc.stdout or proc.stderr).strip()
        line = text.splitlines()[0] if text else "(no version output)"
        print(f"  {OK} {label}: {line}")
    return ok


# (title, function) — total step count is len(STEPS), never a hand-maintained
# number that a future added/removed step can silently make wrong.
STEPS: list[tuple[str, Callable[[], bool]]] = [
    ("Checking for uv", _step_uv_present),
    ("Installing dependencies (uv sync)", _step_dependencies),
    ("Secrets baseline", _step_secrets_baseline),
    ("Installing git hooks (pre-commit and pre-push)", _step_git_hooks),
    ("Verifying the toolchain runs", _step_toolchain_runs),
]


def main() -> int:
    total = len(STEPS)
    print("Bootstrapping", ROOT.name)

    failures: list[str] = []
    for i, (title, step_fn) in enumerate(STEPS, start=1):
        print(f"\n{DIM}[{i}/{total}]{RESET} {title}")
        if not step_fn():
            failures.append(title)
            if i <= 2:
                # uv missing or `uv sync` failing means nothing downstream can
                # possibly work — stop immediately rather than print four more
                # confusing failures caused by the same root cause.
                return 1

    print()
    if failures:
        print(f"{WARN} Done, with problems: {', '.join(failures)}")
        print("  The repo is usable but not fully guarded. Fix the above before committing.")
        return 1

    print(f"{OK} Ready.\n")
    print("  make check    everything CI runs")
    print("  make test     fast suite only")
    print("  make help     all targets")
    return 0


if __name__ == "__main__":
    sys.exit(main())
