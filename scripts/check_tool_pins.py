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

Uses `pyyaml` (real YAML, not line-by-line string matching — a hand-rolled parser
silently mis-handled inline comments, flow-style mappings, and anything else it
didn't anticipate) and `packaging` (real PEP 508 requirement parsing, so an exact
pin with extras or an environment marker, e.g. `ruff[extra]==0.15.15; python_version
>= "3.11"`, is recognised rather than silently dropped). Both are direct dev
dependencies for exactly this reason — this script must run inside the project venv
(`uv run python scripts/check_tool_pins.py`), not a bare system Python.

Add a tool here by adding one row to TOOLS.
"""

from __future__ import annotations

from pathlib import Path
import sys
import tomllib

from packaging.requirements import InvalidRequirement, Requirement
import yaml

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
PRECOMMIT = ROOT / ".pre-commit-config.yaml"

# tool name -> the pre-commit repo URL that provides it
TOOLS = {
    "ruff": "https://github.com/astral-sh/ruff-pre-commit",
    "detect-secrets": "https://github.com/Yelp/detect-secrets",
}


def pyproject_pins() -> dict[str, str]:
    """Exact-pinned versions from the dev dependency group."""
    data = tomllib.loads(PYPROJECT.read_text())
    dev: list[object] = data.get("dependency-groups", {}).get("dev", [])
    pins: dict[str, str] = {}
    for spec in dev:
        # PEP 735 allows dict entries too (e.g. {"include-group": "..."}), not
        # only plain requirement strings. Skip anything that isn't a string
        # rather than crashing — this script runs as a commit-time hook on any
        # pyproject.toml change, so an unrelated dependency-group edit must not
        # be able to block an unrelated commit.
        if not isinstance(spec, str):
            continue
        try:
            req = Requirement(spec)
        except InvalidRequirement:
            continue
        # Only an exact, single-specifier pin counts — "ruff==0.15.15" yes,
        # "ruff>=1.0" or "ruff==0.15.15,<0.16" no. A real parser (rather than a
        # regex) means extras (`ruff[extra]==...`) and environment markers
        # (`; python_version >= "3.11"`) are handled structurally: they simply
        # do not affect which specifier we read, instead of silently defeating
        # a hand-written pattern that never anticipated them.
        specs = list(req.specifier)
        if len(specs) == 1 and specs[0].operator == "==":
            pins[req.name.lower()] = specs[0].version
    return pins


def precommit_revs() -> dict[str, str]:
    """Map repo URL -> rev, from the real parsed YAML structure."""
    doc = yaml.safe_load(PRECOMMIT.read_text()) or {}
    revs: dict[str, str] = {}
    for entry in doc.get("repos", []):
        repo = entry.get("repo")
        rev = entry.get("rev")
        # A "local" repo never publishes a rev of its own — pre-commit doesn't
        # require or use one. Skip it outright rather than recording it, so
        # nothing can ever be misattributed to "local".
        if repo and repo != "local" and rev is not None:
            revs[repo] = str(rev).removeprefix("v")
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
