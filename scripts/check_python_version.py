#!/usr/bin/env python3
"""Fail if the Python version disagrees across the places that encode it.

`.python-version` is canonical — it is what CI and `uv run` actually build and
test against. Three other places encode the same version and cannot read it
dynamically at parse time:

  * `[tool.ruff] target-version`   (e.g. "py311")
  * `[tool.mypy] python_version`   (e.g. "3.11")
  * every `FROM python:X.Y` line in the Dockerfile

Bumping `.python-version` to 3.12 and forgetting any one of those silently
ships the wrong Python in the container, or type-checks against the wrong
stdlib — invisible until it matters, the same class of problem
scripts/check_tool_pins.py exists to catch for tool versions.

`requires-python` in `[project]` is checked differently: it is a deliberate
*floor* for consumers ("works on 3.11+"), not the build version, so it only has
to be satisfied by the canonical version, not equal to it.

Nothing here reads .github/workflows/ci.yml, and nothing needs to: no job in it
names a Python version. The audit job reads `.python-version` at run time
instead of hardcoding one.

Only the (major, minor) pair is compared. `.python-version` legitimately holds a
patch version (`uv python pin 3.11.9` writes `3.11.9`) or a prefixed form
(`cpython-3.11`); an earlier version compared the file's raw contents, so a
patch pin produced four bogus failures at once and told you to set
`target-version = "py3119"`.

Uses only the standard library plus `packaging` (already a direct dependency of
scripts/check_tool_pins.py) so it can run as a pre-commit hook with no extra
setup beyond the project venv.
"""

from __future__ import annotations

from pathlib import Path
import re
import sys
import tomllib
from typing import Any

from packaging.specifiers import InvalidSpecifier, SpecifierSet

ROOT = Path(__file__).resolve().parents[1]
PYTHON_VERSION_FILE = ROOT / ".python-version"
PYPROJECT = ROOT / "pyproject.toml"
DOCKERFILE = ROOT / "Dockerfile"

# encoding="utf-8" on every read: the default is the LOCALE codec, which is
# cp1252 on a stock Windows install. These files are full of em dashes, and the
# first character whose UTF-8 bytes hit one of cp1252's five undefined positions
# (a pasted smart quote is enough) would make this hook die with a
# UnicodeDecodeError on Windows only, blocking every commit while CI stayed green.
_UTF8 = {"encoding": "utf-8"}

_VERSION_RE = re.compile(r"(\d+)\.(\d+)")


def _major_minor(raw: str) -> tuple[int, int] | None:
    """First X.Y in the string, so `3.11`, `3.11.9` and `cpython-3.11` all work."""
    m = _VERSION_RE.search(raw)
    return (int(m.group(1)), int(m.group(2))) if m else None


def _fmt(v: tuple[int, int]) -> str:
    return f"{v[0]}.{v[1]}"


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


def ruff_target_version(data: dict[str, Any]) -> tuple[str, tuple[int, int] | None] | None:
    """Returns (raw, parsed) so an unparseable value can be quoted in the error."""
    raw = _nested_get(data, "tool", "ruff", "target-version")
    if not isinstance(raw, str):
        return None
    m = re.fullmatch(r"py(\d)(\d+)", raw)
    return (raw, (int(m.group(1)), int(m.group(2))) if m else None)


def mypy_python_version(data: dict[str, Any]) -> tuple[str, tuple[int, int] | None] | None:
    raw = _nested_get(data, "tool", "mypy", "python_version")
    if not isinstance(raw, str):
        return None
    return (raw, _major_minor(raw))


def requires_python_floor(data: dict[str, Any]) -> str | None:
    raw = _nested_get(data, "project", "requires-python")
    return raw if isinstance(raw, str) else None


def dockerfile_versions() -> list[tuple[int, str]]:
    """(line number, version) for every `FROM ... python:X.Y` line.

    Matches `python:X.Y` anywhere after FROM rather than immediately after it,
    so the standard BuildKit multi-arch form
    `FROM --platform=$BUILDPLATFORM python:3.11-slim AS builder` is seen. The
    previous anchored pattern skipped it silently, and reported match ordinals
    ("FROM line #1") rather than real line numbers, so the one message you got
    pointed at the wrong line.
    """
    if not DOCKERFILE.exists():
        return []
    found: list[tuple[int, str]] = []
    for lineno, line in enumerate(DOCKERFILE.read_text(**_UTF8).splitlines(), start=1):
        if not re.match(r"^\s*FROM\b", line, flags=re.IGNORECASE):
            continue
        m = re.search(r"\bpython:(\d+\.\d+)", line)
        if m:
            found.append((lineno, m.group(1)))
    return found


def main() -> int:
    if not PYTHON_VERSION_FILE.exists():
        print(f"{PYTHON_VERSION_FILE.name} is missing — it is the canonical version.", file=sys.stderr)
        return 1

    raw_canonical = PYTHON_VERSION_FILE.read_text(**_UTF8).strip()
    parsed = _major_minor(raw_canonical)
    if parsed is None:
        print(
            f"{PYTHON_VERSION_FILE.name} holds {raw_canonical!r}, which has no X.Y version in it.",
            file=sys.stderr,
        )
        return 1
    canonical = _fmt(parsed)

    data = tomllib.loads(PYPROJECT.read_text(**_UTF8))
    problems: list[str] = []
    want_ruff = f"py{parsed[0]}{parsed[1]}"

    ruff = ruff_target_version(data)
    if ruff is None:
        problems.append("[tool.ruff] target-version is missing or not a string.")
    elif ruff[1] is None:
        problems.append(
            f"[tool.ruff] target-version is {ruff[0]!r}, which is not a pyXY string. "
            f'Set target-version = "{want_ruff}".'
        )
    elif ruff[1] != parsed:
        problems.append(
            f"[tool.ruff] target-version is {ruff[0]!r} ({_fmt(ruff[1])}), .python-version says "
            f'{canonical}. Set target-version = "{want_ruff}".'
        )

    mypy = mypy_python_version(data)
    if mypy is None:
        problems.append("[tool.mypy] python_version is missing or not a string.")
    elif mypy[1] is None or mypy[1] != parsed:
        problems.append(
            f"[tool.mypy] python_version is {mypy[0]!r}, .python-version says {canonical}. "
            f'Set python_version = "{canonical}".'
        )

    docker = dockerfile_versions()
    if DOCKERFILE.exists() and not docker:
        # A silent pass here would be the worst outcome: it is exactly what an
        # unrecognised FROM form (an ARG-substituted tag, say) produces, and that
        # refactor is the moment this check is most needed.
        problems.append(
            "Dockerfile exists but no `FROM ... python:X.Y` line was found. If the tag is "
            "built from an ARG, this check cannot see it — pin it literally, or teach "
            "dockerfile_versions() the new form."
        )
    for lineno, docker_v in docker:
        if docker_v != canonical:
            problems.append(
                f"Dockerfile:{lineno} pins python:{docker_v}, .python-version says {canonical}."
            )

    floor = requires_python_floor(data)
    if floor is None:
        problems.append("[project] requires-python is missing.")
    else:
        try:
            satisfied = SpecifierSet(floor).contains(canonical)
        except InvalidSpecifier as exc:
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
