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

from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / ".secrets.baseline"

OK, WARN, FAIL, DIM = "\033[32m✓\033[0m", "\033[33m!\033[0m", "\033[31m✗\033[0m", "\033[2m"
RESET = "\033[0m"


def run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, check=False, capture_output=capture)


def step(n: int, total: int, title: str) -> None:
    print(f"\n{DIM}[{n}/{total}]{RESET} {title}")


def main() -> int:
    total = 5
    failures: list[str] = []

    print("Bootstrapping", ROOT.name)

    # -- 1. uv present? Everything else depends on it. ----------------------
    step(1, total, "Checking for uv")
    if shutil.which("uv") is None:
        print(f"  {FAIL} uv is not on PATH.")
        print("     Install it:  curl -LsSf https://astral.sh/uv/install.sh | sh")
        print("     Then re-run this script.")
        return 1
    version = run(["uv", "--version"], capture=True).stdout.strip()
    print(f"  {OK} {version}")

    # -- 2. Dependencies ---------------------------------------------------
    step(2, total, "Installing dependencies (uv sync)")
    if run(["uv", "sync"]).returncode != 0:
        print(f"  {FAIL} uv sync failed — fix the error above, then re-run.")
        return 1
    print(f"  {OK} .venv is in sync with uv.lock")

    # -- 3. Secrets baseline. Do this BEFORE installing hooks, so the first
    #       commit after bootstrap cannot fail on a missing baseline. --------
    step(3, total, "Secrets baseline")
    if BASELINE.exists():
        print(f"  {OK} .secrets.baseline already present")
    else:
        proc = run(["uv", "run", "detect-secrets", "scan"], capture=True)
        if proc.returncode == 0 and proc.stdout.strip():
            BASELINE.write_text(proc.stdout)
            print(f"  {OK} created .secrets.baseline")
            print(
                f"     {DIM}Commit it. Re-run `make secrets-baseline` after an audited"
                f" false positive.{RESET}"
            )
        else:
            print(f"  {WARN} could not generate a baseline (detect-secrets not installed yet?)")
            print("     Run `make secrets-baseline` once dependencies are installed.")
            failures.append("secrets baseline")

    # -- 4. Git hooks — both types. ---------------------------------------
    step(4, total, "Installing git hooks (pre-commit and pre-push)")
    # default_install_hook_types in .pre-commit-config.yaml means one command
    # installs both. --install-hooks pre-builds the environments so the first
    # real commit is not a two-minute wait.
    if run(["uv", "run", "pre-commit", "install", "--install-hooks"]).returncode != 0:
        print(f"  {FAIL} pre-commit install failed")
        failures.append("git hooks")
    else:
        hooks = ROOT / ".git" / "hooks"
        installed = [n for n in ("pre-commit", "pre-push") if (hooks / n).exists()]
        print(f"  {OK} installed: {', '.join(installed) or 'none found'}")
        if "pre-push" not in installed:
            print(f"  {WARN} pre-push hook missing — tests will not run before push.")
            print("     Check default_install_hook_types in .pre-commit-config.yaml")
            failures.append("pre-push hook")

    # -- 5. Prove it works, rather than assuming. -------------------------
    step(5, total, "Verifying the toolchain runs")
    for label, cmd in (
        ("ruff", ["uv", "run", "ruff", "--version"]),
        ("mypy", ["uv", "run", "mypy", "--version"]),
        ("pytest", ["uv", "run", "pytest", "--version"]),
    ):
        proc = run(cmd, capture=True)
        if proc.returncode == 0:
            print(f"  {OK} {label}: {proc.stdout.strip().splitlines()[0]}")
        else:
            print(f"  {FAIL} {label} would not run")
            failures.append(label)

    # -- Report -----------------------------------------------------------
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
