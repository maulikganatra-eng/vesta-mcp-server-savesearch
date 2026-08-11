"""Tests for this repo's own anti-drift tooling.

Why these exist. Every mechanism that stops a value drifting — TOOLS,
EXCLUDE_FILES, STEPS, TASKS, the version checkers — lives in scripts/, and
scripts/ had no tests at all. The apparatus meant to make the repo
regression-proof was the least verified code in it, and several of its guarantees
were asserted only in a comment. Two of those comments named a test that did not
exist.

These are behavioural, not coverage-driven: scripts/ stays out of
[tool.coverage.run] source because it is developer tooling rather than shipped
code, so the 95% gate does not apply to it.
"""

from __future__ import annotations

import ast
from pathlib import Path
import re
import sys
import tomllib
from typing import Any

import check_python_version
import check_tool_pins
import pytest
import secrets_baseline
import tasks

ROOT = Path(__file__).resolve().parents[1]


def _pyproject() -> dict[str, Any]:
    return tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))


# --------------------------------------------------------------------------
# The Makefile ↔ tasks.py registry, which the Makefile's own comment promises
# is tested.
# --------------------------------------------------------------------------


def _makefile_tasks() -> set[str]:
    text = (ROOT / "Makefile").read_text(encoding="utf-8")
    m = re.search(r"^TASKS :=((?:[^\n]*\\\n)*[^\n]*)$", text, flags=re.MULTILINE)
    assert m, "Makefile has no TASKS := assignment"
    return set(m.group(1).replace("\\", " ").split())


def test_makefile_task_list_matches_tasks_py() -> None:
    """Every task is reachable via `make`, and `make` offers nothing fictional.

    Drift here is not silent-but-harmless: a task in tasks.py but not the
    Makefile gives "No rule to make target", and a target in the Makefile but not
    tasks.py gives "unknown task" — both after the fact, to whoever typed it.
    """
    registry = {*tasks.TASKS, *tasks.COMPOSITE, *tasks.PY_TASKS}
    assert _makefile_tasks() == registry


# --------------------------------------------------------------------------
# Marker names: the dangerous one, because pytest does not validate them.
# --------------------------------------------------------------------------


def test_excluded_markers_are_declared_in_pyproject() -> None:
    """A `-m "not <typo>"` expression silently matches EVERY test.

    So an undeclared name in EXCLUDED_MARKERS would not error — it would quietly
    stop excluding the live-credential suite, and CI would start running it while
    staying green. Nothing in pytest catches this; this test is the only thing
    that does.
    """
    declared = {
        line.split(":", 1)[0].strip()
        for line in _pyproject()["tool"]["pytest"]["ini_options"]["markers"]
    }
    assert set(tasks.EXCLUDED_MARKERS) <= declared


def test_marker_excluding_tasks_use_the_shared_expression() -> None:
    """The `-m` string must come from EXCLUDED_MARKERS, not be retyped."""
    for name in ("test", "test-cov"):
        argv = tasks.TASKS[name][1][0]
        assert tasks._EXCLUDE_EXPR in argv


# --------------------------------------------------------------------------
# `clean` must never delete irreplaceable local state. This is a regression
# test for a real data-loss bug: it deleted .env, which is gitignored and so
# unrecoverable.
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "entry",
    [
        ".env",
        ".env.local",
        ".envrc",
        ".venv",
        ".venv/lib/python3.11/site-packages",
        ".venv-3.12",
        "venv",
        ".direnv",
        ".idea",
        ".vscode",
    ],
)
def test_clean_never_deletes_local_state(entry: str) -> None:
    assert tasks._keep(entry), f"clean would delete {entry}"


@pytest.mark.parametrize(
    "entry",
    [
        "htmlcov",
        ".coverage",
        "coverage.xml",
        ".mypy_cache",
        ".ruff_cache",
        ".pytest_cache",
        "audit.log",
        "requirements.audit.txt",
        "src/vesta_saved_search/__pycache__",
    ],
)
def test_clean_does_remove_regenerable_artefacts(entry: str) -> None:
    assert not tasks._keep(entry), f"clean would spare the artefact {entry}"


# --------------------------------------------------------------------------
# tasks.py registry integrity — the checks main() performs at startup.
# --------------------------------------------------------------------------


def test_task_names_are_unique_across_registries() -> None:
    names = [*tasks.TASKS, *tasks.COMPOSITE, *tasks.PY_TASKS]
    assert len(names) == len(set(names))


def test_every_composite_member_resolves() -> None:
    known = {*tasks.TASKS, *tasks.COMPOSITE, *tasks.PY_TASKS}
    for name, (_, members) in tasks.COMPOSITE.items():
        for member in members:
            assert member in known, f"{name} refers to unknown task {member}"


def test_unknown_task_exits_two_and_lists_tasks(capsys: pytest.CaptureFixture[str]) -> None:
    assert tasks.main(["definitely-not-a-task"]) == 2
    out = capsys.readouterr()
    assert "unknown task" in out.err
    assert "lint" in out.out  # the help listing follows


def test_composite_cycle_is_reported_not_a_recursion_error() -> None:
    original = dict(tasks.COMPOSITE)
    try:
        tasks.COMPOSITE["loop-a"] = ("", ["loop-b"])
        tasks.COMPOSITE["loop-b"] = ("", ["loop-a"])
        assert tasks.run_task("loop-a") == 2
    finally:
        tasks.COMPOSITE.clear()
        tasks.COMPOSITE.update(original)


# --------------------------------------------------------------------------
# The two consistency checkers must pass on the real tree, and must actually
# fail when a value drifts. A checker that only ever passes is decorative.
# --------------------------------------------------------------------------


def test_tool_pins_agree_on_the_real_tree() -> None:
    assert check_tool_pins.main() == 0


def test_python_version_agrees_on_the_real_tree() -> None:
    assert check_python_version.main() == 0


def test_python_version_check_catches_a_drifted_dockerfile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    drifted = tmp_path / "Dockerfile"
    drifted.write_text(
        "FROM --platform=$BUILDPLATFORM python:3.13-slim AS builder\nFROM python:3.13-slim\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(check_python_version, "DOCKERFILE", drifted)
    assert check_python_version.main() == 1
    # And the --platform form is seen at all: an earlier anchored regex skipped
    # it, so this drift produced only one message instead of two.
    assert len(check_python_version.dockerfile_versions()) == 2


def test_python_version_check_accepts_a_patch_level_pin(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """`uv python pin 3.11.9` writes a patch version; that must not break every check."""
    pinned = tmp_path / ".python-version"
    pinned.write_text("3.11.9\n", encoding="utf-8")
    monkeypatch.setattr(check_python_version, "PYTHON_VERSION_FILE", pinned)
    assert check_python_version.main() == 0


def test_python_version_check_flags_an_unreadable_dockerfile_tag(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An ARG-substituted tag must be reported, not silently pass."""
    drifted = tmp_path / "Dockerfile"
    drifted.write_text("ARG PY=3.13\nFROM python:${PY}-slim\n", encoding="utf-8")
    monkeypatch.setattr(check_python_version, "DOCKERFILE", drifted)
    assert check_python_version.main() == 1


# --------------------------------------------------------------------------
# secrets_baseline: the committed baseline and the scan must agree, and every
# uv invocation in the repo must be --frozen.
# --------------------------------------------------------------------------


def test_scan_command_is_frozen() -> None:
    cmd = secrets_baseline.build_scan_command(baseline=".secrets.baseline")
    assert cmd[:3] == ["uv", "run", "--frozen"]


def test_baseline_has_no_unaudited_findings() -> None:
    """An entry without `is_secret` was never reviewed by a human.

    Left unaudited, `detect-secrets audit` shows a pending item from day one, and
    the next person to re-baseline could bury a real credential in the noise.
    """
    import json

    baseline = json.loads((ROOT / ".secrets.baseline").read_text(encoding="utf-8"))
    unaudited = [
        f"{path}:{item.get('line_number')}"
        for path, items in baseline.get("results", {}).items()
        for item in items
        if "is_secret" not in item
    ]
    assert not unaudited, f"unaudited baseline entries: {unaudited}"


def test_normalize_paths_uses_forward_slashes_regardless_of_os() -> None:
    """A baseline (re)generated on Windows must match one generated elsewhere.

    detect-secrets records each finding's filename using the OS's native path
    separator, so a Windows re-scan produced backslash-separated filenames
    while Linux/macOS/CI produce forward-slash ones. A previously-audited
    finding would then stop matching its old entry and reappear as new the
    moment anyone re-baselined on Windows.
    """
    import json

    windows_style = json.dumps(
        {
            "results": {
                "scripts\\check_tool_pins.py": [
                    {"filename": "scripts\\check_tool_pins.py", "line_number": 43}
                ]
            }
        }
    )
    normalized = json.loads(secrets_baseline.normalize_paths(windows_style))
    assert list(normalized["results"]) == ["scripts/check_tool_pins.py"]
    assert (
        normalized["results"]["scripts/check_tool_pins.py"][0]["filename"]
        == "scripts/check_tool_pins.py"
    )


def test_every_task_command_is_frozen() -> None:
    """`uv run` without --frozen can silently re-lock.

    Checked against the actual argv lists rather than by scanning text for the
    string, so prose in a docstring cannot produce a false positive and a real
    command cannot hide behind unusual formatting. `uv lock`/`uv tree` are
    excluded: --frozen is meaningless for the command whose job is to re-lock.
    """
    offenders: list[str] = []
    for name, (_, commands) in tasks.TASKS.items():
        for argv in commands:
            if argv[:2] == ["uv", "run"] and "--frozen" not in argv[:3]:
                offenders.append(f"{name}: {' '.join(argv[:4])}")
    assert not offenders, "task commands missing --frozen:\n" + "\n".join(offenders)


def test_automatic_paths_never_invoke_make() -> None:
    """Hooks and CI must not depend on GNU Make — it is absent on stock Windows.

    The Makefile is an optional wrapper; a hook or CI job that shells out to
    `make` broke every commit and push on that platform.
    """
    offenders: list[str] = []
    for rel in (".pre-commit-config.yaml", ".github/workflows/ci.yml"):
        for lineno, line in enumerate((ROOT / rel).read_text(encoding="utf-8").splitlines(), start=1):
            code = line.split("#", 1)[0]
            if re.search(r"(?:^|[\s:'\"])make\s+[a-z]", code):
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert not offenders, "make on an automatic path:\n" + "\n".join(offenders)


def test_precommit_hygiene_hooks_pin_their_own_stage() -> None:
    """Every hook from pre-commit/pre-commit-hooks must set stages: [pre-commit].

    That repo's manifest hardcodes `stages: [commit, push, manual]` for each of
    its hooks, which OVERRIDES the file's `default_stages: [pre-commit]` --
    that setting only fills in for a hook that specifies none at all, at any
    level. Confirmed empirically: with only default_stages set,
    `--hook-stage pre-push` still ran trailing-whitespace and friends. Losing
    a hook's explicit override silently makes it run twice per push again.
    """
    import yaml

    doc = yaml.safe_load((ROOT / ".pre-commit-config.yaml").read_text(encoding="utf-8"))
    hygiene_repos = [
        entry
        for entry in doc["repos"]
        if entry.get("repo") == "https://github.com/pre-commit/pre-commit-hooks"
    ]
    assert hygiene_repos, "pre-commit-hooks repo block not found"
    offenders = [
        hook["id"]
        for entry in hygiene_repos
        for hook in entry["hooks"]
        if hook.get("stages") != ["pre-commit"]
    ]
    assert not offenders, f"hooks missing an explicit stages: [pre-commit] override: {offenders}"


# --------------------------------------------------------------------------
# bootstrap.py must be runnable by a bare interpreter with no project venv.
# --------------------------------------------------------------------------


def test_bootstrap_is_stdlib_only_and_parses_under_a_bare_interpreter() -> None:
    """It runs before `uv sync`, so it cannot import a third-party package.

    Checked by compiling it with the running interpreter and asserting its
    imports are stdlib or its own sibling module.
    """
    source = (ROOT / "scripts" / "bootstrap.py").read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {alias.name.split(".")[0] for alias in node.names}
        elif isinstance(node, ast.ImportFrom) and node.module:
            # Top-level name only: sys.stdlib_module_names holds "collections",
            # not "collections.abc".
            imported.add(node.module.split(".")[0])
    allowed = set(sys.stdlib_module_names) | {"secrets_baseline", "__future__"}
    assert imported <= allowed, f"non-stdlib imports: {imported - allowed}"


def test_bootstrap_printed_output_is_ascii_only() -> None:
    """Status glyphs must survive a cp1252 console.

    Unicode markers used to raise UnicodeEncodeError on Windows whenever stdout
    was a pipe (Git Bash is a pipe, so this was the common case) — including in
    the "uv is not installed" branch, which crashed instead of printing the
    install instructions to the one person who needed them.

    Only string LITERALS are checked, via the AST: comments and the module
    docstring are prose, are never written to stdout, and legitimately contain em
    dashes like the rest of the repo.
    """
    tree = ast.parse((ROOT / "scripts" / "bootstrap.py").read_text(encoding="utf-8"))

    # Identify docstrings by NODE IDENTITY, not by value: ast.get_docstring()
    # returns the cleaned/dedented text, which never equals the raw Constant, so
    # comparing values silently matched nothing.
    docstring_ids: set[int] = set()
    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if isinstance(body, list) and body:
            first = body[0]
            if (
                isinstance(first, ast.Expr)
                and isinstance(first.value, ast.Constant)
                and isinstance(first.value.value, str)
            ):
                docstring_ids.add(id(first.value))

    offenders: dict[int, set[str]] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) in docstring_ids:
                continue
            bad = {ch for ch in node.value if ord(ch) > 127}
            if bad:
                offenders[node.lineno] = bad
    assert not offenders, f"non-ASCII in printed strings: {offenders}"
