#!/usr/bin/env python3
"""Fresh clone to ready-to-work, in one command.

Run either of these — they do the same thing:

    make setup
    python scripts/bootstrap.py        # or python3, on macOS/Linux

Idempotent: safe to re-run any time. Each step reports whether it did work or
found the job already done.

Why this exists: the setup steps for these repos are "uv sync", "pre-commit
install", "generate a secrets baseline" — and the third one is the one everybody
forgets. Without a baseline the detect-secrets hook fails on the very first
commit with an error that reads like a bug in the hook rather than a missing
file. A new team member hits that on day one.

Standard library only, and deliberately ASCII-only output. This is the one script
that runs on a machine with no virtualenv and no project dependencies, under
whatever interpreter is on PATH, with output that may be piped to a file or read
in a terminal that has no UTF-8 console writer. Unicode status glyphs used to
raise UnicodeEncodeError under Windows' cp1252 locale codec whenever stdout was
not a real console handle (Git Bash/mintty is a pipe, so this was the common
case, not the edge case) — and the worst instance was the "uv is not installed"
branch, which crashed with a traceback instead of printing the install
instructions to the one person who most needed them.
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

# Colour only when stdout is a terminal. Piping to a file or into a pager should
# produce clean text, and legacy Windows consoles render raw escape codes rather
# than interpreting them.
_TTY = sys.stdout.isatty()


def _c(code: str, text: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _TTY else text


OK = _c("32", "[ ok ]")
WARN = _c("33", "[warn]")
FAIL = _c("31", "[fail]")


def _dim(text: str) -> str:
    return _c("2", text)


def run(cmd: list[str], *, capture: bool = False) -> subprocess.CompletedProcess[str]:
    return subprocess.run(cmd, cwd=ROOT, text=True, check=False, capture_output=capture)


def _step_uv_present() -> bool:
    if shutil.which("uv") is None:
        print(f"  {FAIL} uv is not on PATH.")
        print("     Install it, then re-run this script:")
        print("       macOS/Linux:  curl -LsSf https://astral.sh/uv/install.sh | sh")
        # The POSIX one-liner is unrunnable on a stock Windows box — there is no
        # `sh` in cmd or PowerShell — so both forms are printed rather than
        # leaving Windows users with an instruction they cannot follow.
        print(
            "       Windows:      powershell -ExecutionPolicy ByPass -c "
            '"irm https://astral.sh/uv/install.ps1 | iex"'
        )
        return False
    # Check the exit code, not just the output. A uv that is on PATH but broken
    # (a partial install, the wrong architecture) exits non-zero, and reading only
    # stdout printed an empty version and reported SUCCESS -- so the install
    # guidance above never appeared and the user hit a confusing `uv sync` failure
    # one step later instead.
    proc = run(["uv", "--version"], capture=True)
    if proc.returncode != 0:
        print(f"  {FAIL} uv is on PATH but would not run.")
        print(f"     {(proc.stderr or proc.stdout).strip() or '(no output)'}")
        print("     Reinstall it, then re-run this script.")
        return False
    text = (proc.stdout or proc.stderr).strip()
    print(f"  {OK} {text.splitlines()[0] if text else 'uv (no version output)'}")
    return True


def _step_dependencies() -> bool:
    if run(["uv", "sync"]).returncode != 0:
        print(f"  {FAIL} uv sync failed. Fix the error above, then re-run.")
        return False
    print(f"  {OK} .venv is in sync with uv.lock")
    return True


def _step_secrets_baseline() -> bool:
    if BASELINE.exists():
        print(f"  {OK} .secrets.baseline already present")
        return True

    proc = run(secrets_baseline.build_scan_command(), capture=True)
    if proc.returncode == 0 and proc.stdout.strip():
        # newline="\n" matters: the default translates \n to \r\n on Windows, and
        # the resulting CRLF file trips the mixed-line-ending hook on the very
        # first commit — rewriting the baseline and failing that commit, which is
        # exactly the day-one surprise .gitattributes exists to prevent.
        # encoding="utf-8" for the same reason it is set on every read below.
        # normalize_paths() so a baseline generated on Windows uses the same
        # forward-slash filenames one generated on Linux/macOS/CI would — see
        # its docstring for what breaks otherwise.
        BASELINE.write_text(
            secrets_baseline.normalize_paths(proc.stdout), encoding="utf-8", newline="\n"
        )
        print(f"  {OK} created .secrets.baseline")
        print(
            f"     {_dim('Commit it. Re-run the secrets-baseline task after a new')}"
            f" {_dim('false positive.')}"
        )
        return True

    print(f"  {WARN} could not generate a baseline (detect-secrets not installed yet?)")
    print("     Run the secrets-baseline task once dependencies are installed.")
    return False


def _step_git_hooks() -> bool:
    # default_install_hook_types in .pre-commit-config.yaml means one command
    # installs both. --install-hooks pre-builds the environments so the first
    # real commit is not a two-minute wait.
    if run(["uv", "run", "pre-commit", "install", "--install-hooks"]).returncode != 0:
        print(f"  {FAIL} pre-commit install failed")
        return False

    # Ask git where hooks live rather than assuming ROOT/.git/hooks. In a
    # `git worktree` checkout .git is a FILE, and the hooks directory belongs to
    # the main repo -- pre-commit resolves that correctly and installs there,
    # while the hardcoded path found nothing and reported a perfectly good clone
    # as broken. Same for submodules and for anyone with core.hooksPath set.
    rev = run(["git", "rev-parse", "--git-path", "hooks"], capture=True)
    hooks = (
        Path(rev.stdout.strip())
        if rev.returncode == 0 and rev.stdout.strip()
        else ROOT / ".git" / "hooks"
    )
    if not hooks.is_absolute():
        hooks = ROOT / hooks
    installed = [n for n in ("pre-commit", "pre-push") if (hooks / n).exists()]
    print(f"  {OK} installed: {', '.join(installed) or 'none found'}")
    if "pre-push" not in installed:
        print(f"  {WARN} pre-push hook missing: tests will not run before push.")
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


# (title, function, critical). `critical` means nothing after this step can
# possibly succeed, so stop immediately rather than printing several more
# failures that all share one root cause. It is a property of the step itself —
# the previous version inferred it from the step's POSITION in this list, which
# silently attached to the wrong steps as soon as one was reordered, defeating
# the point of listing them declaratively.
STEPS: list[tuple[str, Callable[[], bool], bool]] = [
    ("Checking for uv", _step_uv_present, True),
    ("Installing dependencies (uv sync)", _step_dependencies, True),
    ("Secrets baseline", _step_secrets_baseline, False),
    ("Installing git hooks (pre-commit and pre-push)", _step_git_hooks, False),
    ("Verifying the toolchain runs", _step_toolchain_runs, False),
]


def main() -> int:
    total = len(STEPS)
    print("Bootstrapping", ROOT.name)

    failures: list[str] = []
    for i, (title, step_fn, critical) in enumerate(STEPS, start=1):
        print(f"\n{_dim(f'[{i}/{total}]')} {title}")
        if not step_fn():
            failures.append(title)
            if critical:
                return 1

    print()
    if failures:
        print(f"{WARN} Done, with problems: {', '.join(failures)}")
        print("  The repo is usable but not fully guarded. Fix the above before committing.")
        return 1

    print(f"{OK} Ready.\n")
    print("  make check    the three blocking CI jobs")
    print("  make test     fast suite only")
    print("  make help     all targets")
    print(_dim("  (no make? use: uv run --frozen python scripts/tasks.py <task>)"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
