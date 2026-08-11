#!/usr/bin/env python3
"""Fail when one value is written in two files and the copies disagree.

Why this script exists, concretely. Both sibling repos pin ruff twice — in
`.pre-commit-config.yaml` and in `.github/workflows/ci.yml` — each with a comment
asking the next person to keep the two in step. In both repos the pair currently
agrees, so the comment has held so far; but the two repos are a year apart
(0.4.4 vs 0.15.15) with different default rules, and nothing anywhere would catch
the intra-repo case the moment it slipped. A comment is a request, not a
mechanism. This repo's equivalent duplication is pyproject.toml ↔
.pre-commit-config.yaml, and it is checked here rather than requested.

Four families of duplication are checked:

1. **Dual-pinned dev tools** (ruff, detect-secrets). These legitimately run from
   two places: `tasks.py lint` / `tasks.py secrets-baseline` use the copy in the
   project venv, while the git hooks use pre-commit's own isolated copy. Both are
   wanted; different VERSIONS are not, because then the same code passes locally
   and fails in CI and the error blames the wrong commit.

2. **The inverse of (1)**: any exact `==` pin in the dev group that has no TOOLS
   row. Without this, TOOLS is a hand-maintained allowlist and the mechanism
   depends on the same human memory it was built to replace — add a
   `mirrors-mypy` hook plus `mypy==1.x` and you reproduce the original bug with a
   green check.

3. **uv's own version**, which appears in three places: `.pre-commit-config.yaml`
   (the `uv-lock` hook's uv), every `setup-uv` step in ci.yml, and the
   Dockerfile's `COPY --from=ghcr.io/astral-sh/uv`. Left unpinned in CI or on a
   `:latest` image tag, one side can call `uv.lock` fresh while another rejects
   it. uv is not a dev dependency (it is the bootstrap binary) so it cannot be a
   TOOLS row.

4. **The two values `.secrets.baseline` embeds**: the detect-secrets version, and
   the exclude-pattern list. The baseline is generated but committed, so it is a
   real third copy of both.

Uses `pyyaml` (real YAML, not line-by-line string matching — a hand-rolled parser
silently mis-handled inline comments and flow-style mappings) and `packaging`
(real PEP 508 requirement parsing, so a pin with extras or an environment marker
is recognised; and real version comparison, so `1.5.0rc1` and `v1.5.0-rc1` are
understood as the same release rather than reported as an unfixable mismatch).
Both are direct dev dependencies for exactly this reason — this script must run
inside the project venv (`uv run --frozen python scripts/check_tool_pins.py`),
not a bare system Python.

Add a dual-pinned tool by adding one row to TOOLS. Forgetting to is what check
(2) catches.
"""

from __future__ import annotations

import json
from pathlib import Path
import re
import sys
import tomllib

from packaging.requirements import InvalidRequirement, Requirement
from packaging.version import InvalidVersion, Version
import yaml

ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = ROOT / "pyproject.toml"
PRECOMMIT = ROOT / ".pre-commit-config.yaml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
BASELINE = ROOT / ".secrets.baseline"
DOCKERFILE = ROOT / "Dockerfile"

# encoding="utf-8" everywhere: the default is the LOCALE codec, cp1252 on a stock
# Windows install. These files are full of em dashes, and one pasted smart quote
# would make this hook die with UnicodeDecodeError on Windows only — blocking
# every commit there while CI stayed green.
_UTF8 = {"encoding": "utf-8"}

# tool name (as it appears in pyproject's dev group) -> pre-commit repo URL
TOOLS = {
    "ruff": "https://github.com/astral-sh/ruff-pre-commit",
    # pragma comment: this is a repo URL, not a credential. Marked at source so
    # .secrets.baseline stays empty rather than carrying a pending entry.
    "detect-secrets": "https://github.com/Yelp/detect-secrets",  # pragma: allowlist secret
}

# uv is not a dev dependency (it is the bootstrap binary), so it cannot be
# expressed as a TOOLS row — it gets its own check.
UV_PRECOMMIT_REPO = "https://github.com/astral-sh/uv-pre-commit"
SETUP_UV_ACTION = "astral-sh/setup-uv"

_EXCLUDE_FILTER = "detect_secrets.filters.regex.should_exclude_file"


def pyproject_pins() -> dict[str, str]:
    """Exact-pinned versions from the dev dependency group."""
    data = tomllib.loads(PYPROJECT.read_text(**_UTF8))
    dev: list[object] = data.get("dependency-groups", {}).get("dev", [])
    pins: dict[str, str] = {}
    for spec in dev:
        # PEP 735 allows dict entries too (e.g. {"include-group": "..."}), not
        # only requirement strings. Skip anything that isn't a string rather than
        # crashing — this runs as a commit-time hook on any pyproject.toml change,
        # so an unrelated dependency-group edit must not block an unrelated commit.
        if not isinstance(spec, str):
            continue
        try:
            req = Requirement(spec)
        except InvalidRequirement:
            continue
        # Only an exact, single-specifier pin counts. Extras and environment
        # markers are handled structurally by the real parser, so they do not
        # affect which specifier is read.
        specs = list(req.specifier)
        if len(specs) == 1 and specs[0].operator == "==":
            pins[req.name.lower()] = specs[0].version
    return pins


def precommit_revs() -> tuple[dict[str, str], list[str]]:
    """(repo URL -> rev, problems). Reports a URL listed twice rather than
    silently keeping the last rev — splitting ruff and ruff-format across two
    entries is a real pattern, and a dict assignment would pick one arbitrarily
    and then blame pyproject.toml for the mismatch."""
    doc = yaml.safe_load(PRECOMMIT.read_text(**_UTF8)) or {}
    if not isinstance(doc, dict):
        return {}, [f"{PRECOMMIT.name} does not parse as a YAML mapping."]

    seen: dict[str, set[str]] = {}
    for entry in doc.get("repos", []):
        # A malformed repos: list (a bare string, say) must not crash the hook —
        # same defensiveness as the PEP 735 guard above.
        if not isinstance(entry, dict):
            continue
        repo, rev = entry.get("repo"), entry.get("rev")
        # A "local" repo never publishes a rev of its own; skip it outright so
        # nothing can ever be misattributed to "local".
        if repo and repo != "local" and rev is not None:
            seen.setdefault(str(repo), set()).add(str(rev).removeprefix("v"))

    problems = [
        f"{repo} appears more than once in {PRECOMMIT.name} with different revs: "
        f"{', '.join(sorted(revs))}. Use one rev for one repo."
        for repo, revs in seen.items()
        if len(revs) > 1
    ]
    return {repo: next(iter(revs)) for repo, revs in seen.items() if len(revs) == 1}, problems


def setup_uv_versions() -> list[str]:
    """Every `version:` given to a setup-uv step in ci.yml."""
    if not CI.exists():
        return []
    doc = yaml.safe_load(CI.read_text(**_UTF8)) or {}
    if not isinstance(doc, dict):
        return []
    found: list[str] = []
    for job in (doc.get("jobs") or {}).values():
        if not isinstance(job, dict):
            continue
        for step in job.get("steps") or []:
            if not isinstance(step, dict):
                continue
            uses = str(step.get("uses") or "")
            if uses.split("@")[0] == SETUP_UV_ACTION:
                version = (step.get("with") or {}).get("version")
                # Record the absence too — an unpinned step is the problem.
                found.append(str(version) if version is not None else "")
    return found


def dockerfile_uv_versions() -> list[str]:
    """uv versions from `COPY --from=ghcr.io/astral-sh/uv:<version>` lines."""
    if not DOCKERFILE.exists():
        return []
    return re.findall(r"ghcr\.io/astral-sh/uv:([^\s]+)", DOCKERFILE.read_text(**_UTF8))


def _same_version(a: str, b: str) -> bool | None:
    """True/False if both parse as versions, None if either does not.

    None means "cannot compare" — a SHA rev is legal in pre-commit, and calling
    that a mismatch produced an error whose suggested fix ("pick one version and
    set it in both") was impossible to satisfy.
    """
    try:
        return Version(a) == Version(b)
    except InvalidVersion:
        return None


def _compare(label: str, left_name: str, left: str, right_name: str, right: str) -> list[str]:
    same = _same_version(left, right)
    if same is True:
        return []
    if same is None:
        return [
            f"{label}: {left_name} has {left!r} and {right_name} has {right!r}, and at least one "
            f"is not a version number, so they cannot be compared. Use release versions in both."
        ]
    return [
        f"{label}: {left_name} pins {left}, {right_name} pins {right}.\n"
        f"    These must match. Pick one version, set it in both, then run `uv lock`."
    ]


def main() -> int:
    pins = pyproject_pins()
    revs, problems = precommit_revs()

    # --- 1. dual-pinned dev tools -------------------------------------------
    for tool, repo in TOOLS.items():
        pinned, rev = pins.get(tool), revs.get(repo)
        if pinned is None:
            problems.append(
                f"{tool}: no exact pin in pyproject.toml [dependency-groups] dev.\n"
                f'    Add  "{tool}=={rev or "<version>"}"  so the project venv and the git hook'
                f" use the same version."
            )
            continue
        if rev is None:
            problems.append(f"{tool}: no `rev:` found for {repo} in {PRECOMMIT.name}")
            continue
        problems += _compare(tool, "pyproject.toml", pinned, PRECOMMIT.name, rev)

    # --- 2. the inverse: an exact pin with no TOOLS row ---------------------
    for tool in sorted(set(pins) - set(TOOLS)):
        problems.append(
            f"{tool} is exact-pinned ({tool}=={pins[tool]}) in pyproject.toml but has no TOOLS row "
            f"in {Path(__file__).name}.\n"
            f"    If it is also pinned in {PRECOMMIT.name}, add a row so the two are checked. If it"
            f" is not, loosen the pin to >= so it is clear no second copy exists."
        )

    # --- 3. uv, pinned in pre-commit and in every setup-uv step ------------
    uv_rev = revs.get(UV_PRECOMMIT_REPO)
    ci_versions = setup_uv_versions()
    if uv_rev is None:
        problems.append(f"uv: no `rev:` found for {UV_PRECOMMIT_REPO} in {PRECOMMIT.name}")
    for i, version in enumerate(ci_versions, start=1):
        if not version:
            problems.append(
                f"uv: {SETUP_UV_ACTION} step #{i} in ci.yml has no `version:`, so CI installs the "
                f'latest uv while the uv-lock hook pins {uv_rev}. Add version: "{uv_rev}".'
            )
        elif uv_rev is not None:
            problems += _compare(f"uv (ci.yml step #{i})", PRECOMMIT.name, uv_rev, "ci.yml", version)
    for i, version in enumerate(dockerfile_uv_versions(), start=1):
        if version == "latest":
            problems.append(
                f"uv: Dockerfile COPY #{i} uses ghcr.io/astral-sh/uv:latest. A mutable tag means "
                f"two builds of the same commit can use different uv versions. Pin it to {uv_rev}."
            )
        elif uv_rev is not None:
            problems += _compare(
                f"uv (Dockerfile COPY #{i})", PRECOMMIT.name, uv_rev, "Dockerfile", version
            )

    # --- 4. the two values .secrets.baseline embeds -------------------------
    if BASELINE.exists():
        try:
            baseline = json.loads(BASELINE.read_text(**_UTF8))
        except json.JSONDecodeError as exc:
            problems.append(f"{BASELINE.name} is not valid JSON: {exc}")
        else:
            ds_pin = pins.get("detect-secrets")
            ds_baseline = baseline.get("version")
            if ds_pin and isinstance(ds_baseline, str):
                problems += _compare(
                    "detect-secrets (baseline)",
                    "pyproject.toml",
                    ds_pin,
                    f"{BASELINE.name} version",
                    ds_baseline,
                )

            # Imported rather than restated, so this compares against the same
            # list the scan actually uses.
            from secrets_baseline import EXCLUDE_FILES

            patterns = [
                f.get("pattern")
                for f in baseline.get("filters_used", [])
                if isinstance(f, dict) and f.get("path") == _EXCLUDE_FILTER
            ]
            baked = patterns[0] if patterns and isinstance(patterns[0], list) else None
            if baked is None:
                problems.append(
                    f"{BASELINE.name} has no {_EXCLUDE_FILTER} filter, but "
                    f"secrets_baseline.EXCLUDE_FILES is {EXCLUDE_FILES}. Re-run "
                    f"`tasks.py secrets-baseline`."
                )
            elif list(baked) != list(EXCLUDE_FILES):
                problems.append(
                    f"{BASELINE.name} was generated with exclude patterns {baked}, but "
                    f"secrets_baseline.EXCLUDE_FILES is now {list(EXCLUDE_FILES)}. The committed "
                    f"baseline and the scan disagree about which files count — re-run "
                    f"`tasks.py secrets-baseline` and commit the result."
                )

    if problems:
        print("Config values disagree across files:\n", file=sys.stderr)
        for p in problems:
            print(f"  - {p}\n", file=sys.stderr)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
