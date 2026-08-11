#!/usr/bin/env python3
"""Single source for detect-secrets' `--exclude-files` pattern.

Used by scripts/bootstrap.py (the first-ever baseline on a fresh clone) and by
`tasks.py secrets-baseline` (re-scan after a new false positive) so the pattern
cannot drift between those two call sites — previously each spelled out the same
two patterns independently, and adding an exclusion to one without the other
would bake baseline entries for files the commit-time hook never re-scans.

⚠️ EXCLUDE_FILES must still be kept in sync by hand with the detect-secrets
hook's `exclude:` in .pre-commit-config.yaml. That is a genuinely different
mechanism — pre-commit's own file filter, evaluated before the hook ever runs,
not a `detect-secrets` CLI flag — and YAML cannot import a Python list.

The committed `.secrets.baseline` also embeds a copy of this list (a scan records
the filters it ran with). That copy is NOT kept in sync by hand:
scripts/check_tool_pins.py imports EXCLUDE_FILES from here and fails if the
committed baseline was generated with a different set.

Note the distinction between *scanning* and *auditing*, which earlier wording in
this repo blurred:
  * `detect-secrets scan --baseline` (this file) re-scans and updates the
    baseline, preserving `is_secret` on entries that were already audited.
  * `detect-secrets audit` (interactive; `tasks.py secrets-audit`) is what
    actually sets `is_secret`, i.e. what records a human decision.
Only the second one audits anything.
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]

# Keep in sync with the detect-secrets hook's `exclude:` in
# .pre-commit-config.yaml. check_tool_pins.py enforces the .secrets.baseline copy.
EXCLUDE_FILES: list[str] = [r"uv\.lock", r".*\.ipynb"]


def build_scan_command(*, baseline: str | None = None) -> list[str]:
    """Build the `detect-secrets scan` invocation, exclusions applied.

    Without `baseline`: the command prints a fresh baseline as JSON to stdout —
    used for the very first `.secrets.baseline` on a clone.
    With `baseline`: `detect-secrets` updates that file in place, preserving any
    already-audited false positives.

    `--frozen` because every other `uv run` in this repo uses it. Without it this
    nested call can implicitly re-sync or re-lock the environment, sneaking a
    uv.lock change into what is supposed to be a read-only secrets re-scan.
    """
    cmd = ["uv", "run", "--frozen", "detect-secrets", "scan", *_exclude_args()]
    if baseline:
        cmd += ["--baseline", baseline]
    return cmd


def build_audit_command(baseline: str) -> list[str]:
    """Interactive audit — the only thing that writes `is_secret`."""
    return ["uv", "run", "--frozen", "detect-secrets", "audit", baseline]


def _exclude_args() -> list[str]:
    args: list[str] = []
    for pattern in EXCLUDE_FILES:
        args += ["--exclude-files", pattern]
    return args


def _portable(path: str) -> str:
    """Forward slashes, and repo-relative if the path is inside the repo.

    Two separate machine-dependencies to remove, both of which end up in a
    COMMITTED file:

    * the separator — detect-secrets records filenames with the OS's native one,
      so a Windows re-scan writes backslashes where Linux/macOS/CI write forward
      slashes, and neither recognises the other's entries as the same finding.
    * the prefix — when scanned with `--baseline`, detect-secrets records an
      `is_baseline_file` filter whose filename is an ABSOLUTE path. Committing
      that puts one developer's home directory in the repo and guarantees a diff
      for everyone else.
    """
    cleaned = path.replace("\\", "/")
    root = str(ROOT).replace("\\", "/").rstrip("/") + "/"
    return cleaned.removeprefix(root)


def normalize_paths(text: str) -> str:
    """Make a baseline byte-identical regardless of which machine produced it.

    Covers both places a path appears: every finding under `results`, AND the
    `filters_used` entries. An earlier version normalized only `results`, so the
    absolute path in `filters_used` survived — the same class of
    machine-dependence, in a different key.
    """
    data = json.loads(text)

    normalized: dict[str, list[dict[str, object]]] = {}
    for filename, findings in data.get("results", {}).items():
        for finding in findings:
            if isinstance(finding.get("filename"), str):
                finding["filename"] = _portable(finding["filename"])
        normalized[_portable(filename)] = findings
    data["results"] = normalized

    for entry in data.get("filters_used", []):
        if isinstance(entry, dict) and isinstance(entry.get("filename"), str):
            entry["filename"] = _portable(entry["filename"])

    return json.dumps(data, indent=2) + "\n"


def main(argv: list[str]) -> int:
    # cwd=ROOT and a ROOT-relative baseline: without them, running this script
    # from a subdirectory scanned that subdirectory and wrote a stray baseline
    # into it.
    baseline = argv[0] if argv else ".secrets.baseline"
    audit = "--audit" in argv
    if audit:
        argv = [a for a in argv if a != "--audit"]
        baseline = argv[0] if argv else ".secrets.baseline"
    target = str((ROOT / baseline).resolve())
    cmd = build_audit_command(target) if audit else build_scan_command(baseline=target)
    returncode = subprocess.run(cmd, cwd=ROOT, check=False).returncode

    # `audit` only flips is_secret on existing entries in place — it never
    # touches filenames — so normalizing there would be a no-op at best.
    target_path = Path(target)
    if not audit and returncode == 0 and target_path.exists():
        target_path.write_text(
            normalize_paths(target_path.read_text(encoding="utf-8")),
            encoding="utf-8",
            newline="\n",
        )
    return returncode


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
