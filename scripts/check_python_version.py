#!/usr/bin/env python3
"""Fail if the Python version disagrees across the places that encode it.

`.python-version` is canonical — it is what CI and `uv run` actually build and
test against. But four other places encode the same version and cannot read
it dynamically at parse time:

  * `[tool.ruff] target-version`   (e.g. "py311")
  * `[tool.mypy] python_version`   (e.g. "3.11")
  * the Dockerfile's two `FROM python:X.Y-slim` lines

Bumping `.python-version` to 3.12 and forgetting any one of those silently
ships the wrong Python in the container, or type-checks against the wrong
stdlib — invisible until it matters, the same class of problem
scripts/check_tool_pins.py exists to catch for tool versions.

`requires-python` in `[project]` is checked differently: it is a deliberate
*floor* for consumers ("works on 3.11+"), not the build version, so it only
has to be satisfied by the canonical version, not equal to it.

Uses only the standard library plus `packaging` (already a direct dependency
of scripts/check_tool_pins.py) so it can run as a pre-commit hook with no
extra setup beyond the project venv.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import tomllib
from typing import Any

from packaging.specifiers import SpecifierSet

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION_FILE = ROOT / ".python-version"
PYPROJECT = ROOT / "pyproject.toml"
DOCKERFILE = ROOT / "Dockerfile"


def canonical_version() -> str:
    return PYTHON_VERSION_FILE.read_text().strip()


def _nested_get(data: dict[str, Any], *keys: str) -> object:
    """Walk nested dicts by key, returning None the moment a key is missing
    or an intermediate value isn't a dict — never raising on a malformed or
    partially-written TOML section.
    """
    current: object = data
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = current.get(key)
    return current


def ruff_target_version(data: dict[str, Any]) -> str | None:
    raw = _nested_get(data, "tool", "ruff", "target-version")
    if not isinstance(raw, str):
        return None
    # "py311" -> "3.11"
    m = re.fullmatch(r"py(\d)(\d+)", raw)
    return f"{m.group(1)}.{m.group(2)}" if m else raw


def mypy_python_version(data: dict[str, Any]) -> str | None:
    raw = _nested_get(data, "tool", "mypy", "python_version")
    return raw if isinstance(raw, str) else None


def requires_python_floor(data: dict[str, Any]) -> str | None:
    raw = _nested_get(data, "project", "requires-python")
    return raw if isinstance(raw, str) else None


def dockerfile_versions() -> list[str]:
    if not DOCKERFILE.exists():
        return []
    return re.findall(r"^FROM\s+python:(\d+\.\d+)", DOCKERFILE.read_text(), flags=re.MULTILINE)


def main() -> int:
    canonical = canonical_version()
    data = tomllib.loads(PYPROJECT.read_text())
    problems: list[str] = []

    ruff_v = ruff_target_version(data)
    if ruff_v is None:
        problems.append("[tool.ruff] target-version is missing or not a plain pyXY string.")
    elif ruff_v != canonical:
        problems.append(
            f"[tool.ruff] target-version resolves to {ruff_v}, .python-version says "
            f'{canonical}. Set target-version = "py{canonical.replace(".", "")}".'
        )

    mypy_v = mypy_python_version(data)
    if mypy_v is None:
        problems.append("[tool.mypy] python_version is missing.")
    elif mypy_v != canonical:
        problems.append(f"[tool.mypy] python_version is {mypy_v}, .python-version says {canonical}.")

    for i, docker_v in enumerate(dockerfile_versions(), start=1):
        if docker_v != canonical:
            problems.append(
                f"Dockerfile FROM line #{i} pins python:{docker_v}, .python-version says {canonical}."
            )

    floor = requires_python_floor(data)
    if floor is None:
        problems.append("[project] requires-python is missing.")
    else:
        try:
            satisfied = SpecifierSet(floor).contains(canonical)
        except Exception as exc:  # any parse failure is itself the problem to report
            problems.append(f"[project] requires-python ({floor!r}) failed to parse: {exc}")
        else:
            if not satisfied:
                problems.append(
                    f"[project] requires-python is {floor!r}, which .python-version's "
                    f"{canonical} does not satisfy — the floor is higher than what we "
                    f"actually build with."
                )

    if problems:
        print(f"Python version disagreement (.python-version says {canonical}):\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}\n", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
