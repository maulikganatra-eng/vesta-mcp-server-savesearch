#!/usr/bin/env python3
"""Single source for detect-secrets' `--exclude-files` pattern.

Used by both scripts/bootstrap.py (the first-ever baseline on a fresh clone)
and `make secrets-baseline` (re-audit after an audited false positive) so the
pattern cannot drift between those two call sites — previously each one
spelled out the same two patterns independently, and adding a new exclusion to
one without the other would bake baseline entries for files the commit-time
hook never re-scans.

⚠️ EXCLUDE_FILES must still be kept in sync by hand with the detect-secrets
hook's `exclude:` in .pre-commit-config.yaml. That is a genuinely different
mechanism — pre-commit's own file filter, evaluated before the hook ever runs,
not a `detect-secrets` CLI flag — and YAML cannot import a Python list. This
file is the one place said in code; the YAML value is the one place that has
to be remembered.
"""

from __future__ import annotations

import subprocess
import sys

# Keep in sync with the detect-secrets hook's `exclude:` in
# .pre-commit-config.yaml.
EXCLUDE_FILES: list[str] = [r"uv\.lock", r".*\.ipynb"]


def build_scan_command(*, baseline: str | None = None) -> list[str]:
    """Build the `detect-secrets scan` invocation, exclusions applied.

    Without `baseline`: the command prints a fresh baseline as JSON to
    stdout — used for the very first `.secrets.baseline` on a clone.
    With `baseline`: `detect-secrets` updates that file in place, preserving
    any already-audited false positives — used for `make secrets-baseline`.
    """
    cmd = ["uv", "run", "detect-secrets", "scan", *_exclude_args()]
    if baseline:
        cmd += ["--baseline", baseline]
    return cmd


def _exclude_args() -> list[str]:
    args: list[str] = []
    for pattern in EXCLUDE_FILES:
        args += ["--exclude-files", pattern]
    return args


def main() -> int:
    baseline = sys.argv[1] if len(sys.argv) > 1 else ".secrets.baseline"
    return subprocess.run(build_scan_command(baseline=baseline), check=False).returncode


if __name__ == "__main__":
    raise SystemExit(main())
