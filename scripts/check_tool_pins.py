#!/usr/bin/env python3
"""Fail if a tool is pinned to different versions in pyproject.toml and .pre-commit-config.yaml.

Why this script exists, concretely. Two of our repos pin ruff in both files, each
with a comment saying "keep in sync with the other". They are now on 0.15.15 and
0.4.4 — over a year apart, with different default rules. Nobody did anything wrong;
a comment is not a mechanism.

Some tools legitimately run from two places:
  * `make lint` / `make format` use the copy in the project venv (pyproject.toml)
  * the git hook uses pre-commit's own isolated copy (.pre-commit-config.yaml rev)

Both are wanted. What is not wanted is them being different versions, because then
the same code passes locally and fails in CI, and the error blames the wrong commit.

Uses only the standard library so it can run as a pre-commit hook with no
environment of its own. Add a tool here by adding one row to TOOLS.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import tomllib

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
PRECOMMIT = ROOT / ".pre-commit-config.yaml"

# tool name -> the pre-commit repo URL that provides it
TOOLS = {
    "ruff": "https://github.com/astral-sh/ruff-pre-commit",
}


def pyproject_pins() -> dict[str, str]:
    """Exact-pinned versions from the dev dependency group."""
    data = tomllib.loads(PYPROJECT.read_text())
    dev: list[str] = data.get("dependency-groups", {}).get("dev", [])
    pins: dict[str, str] = {}
    for spec in dev:
        m = re.fullmatch(r"([A-Za-z0-9._-]+)==([0-9][^\s;]*)", spec.strip())
        if m:
            pins[m.group(1).lower()] = m.group(2)
    return pins


def precommit_revs() -> dict[str, str]:
    """Map repo URL -> rev, without needing a YAML parser."""
    revs: dict[str, str] = {}
    current: str | None = None
    for raw in PRECOMMIT.read_text().splitlines():
        line = raw.strip()
        if line.startswith("- repo:"):
            current = line.split(":", 1)[1].strip()
        elif line.startswith("rev:") and current:
            # Strip a leading "v" and any inline comment: `rev: v0.15.15  # note`
            rev = line.split(":", 1)[1].strip().split("#")[0].strip().strip("'\"")
            revs[current] = rev.removeprefix("v")
            current = None
    return revs


def main() -> int:
    pins, revs = pyproject_pins(), precommit_revs()
    problems: list[str] = []

    for tool, repo in TOOLS.items():
        pinned, rev = pins.get(tool), revs.get(repo)

        if pinned is None:
            problems.append(
                f"{tool}: no exact pin in pyproject.toml [dependency-groups] dev.\n"
                f'    Add  "{tool}=={rev or "<version>"}"  so `make lint` and the git hook'
                f" use the same version."
            )
            continue
        if rev is None:
            problems.append(f"{tool}: no `rev:` found for {repo} in .pre-commit-config.yaml")
            continue
        if pinned != rev:
            problems.append(
                f"{tool}: pyproject.toml pins {pinned}, .pre-commit-config.yaml pins {rev}.\n"
                f"    These must match. Pick one version and set it in both, then run"
                f" `uv lock`."
            )

    if problems:
        print("Tool version pins disagree:\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}\n", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
