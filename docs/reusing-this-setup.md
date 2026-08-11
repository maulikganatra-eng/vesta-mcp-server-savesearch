# Reusing this setup on a new project

This CI and pre-commit setup is designed to be copied. Everything in it is
project-agnostic except three values in `pyproject.toml`.

---

## The three-step copy

**1. Copy these files into the new repo, unchanged:**

```
.pre-commit-config.yaml
.github/workflows/ci.yml
.github/dependabot.yml
.github/pull_request_template.md
.editorconfig
.gitattributes
.python-version
.dockerignore
Dockerfile
Makefile
scripts/bootstrap.py
scripts/tasks.py
scripts/check_tool_pins.py
scripts/check_python_version.py
scripts/secrets_baseline.py
```

None of them name this project, list source directories, pin a threshold, or
reference a module. There is nothing to edit.

**2. Copy `pyproject.toml` and change three things:**

| What | Where |
|---|---|
| `name` | `[project]` |
| `description` | `[project]` |
| the package directory name | `[tool.hatch.build.targets.wheel] packages` and `[tool.ruff.lint.isort] known-first-party` |

Then create `src/<your_package>/__init__.py` and a `tests/` directory.

**3. Run it:**

```bash
make setup
```

Done. Commit the generated `.secrets.baseline` and `uv.lock`.

---

## Why it is copyable at all

Four decisions do the work. Each one exists because of a specific problem in our
older repos.

### The `src/` layout means no path lists anywhere

In `vesta-mcp-server` and `smart-search-for-guestSide`, the list of source paths is
written out four times:

- `Makefile` → `SOURCES` and `COV_MODULES`
- `.github/workflows/ci.yml` → `SOURCES` and a chain of `--cov=` flags
- `.pre-commit-config.yaml` → `files: ^(orchestrator|backend|core|tests)/`

Adding a directory means editing four places. Forgetting one means the directory
silently stops being linted, type-checked or counted in coverage — and nothing
fails, so nobody finds out.

With everything under `src/`, every tool targets `src` and `tests`. The lists do
not exist, so they cannot drift or be forgotten.

### One source of truth for every version

- **Tool versions** live in `.pre-commit-config.yaml`. CI runs
  `pre-commit run --all-files` rather than installing ruff itself, so the workflow
  contains no version at all.
- **The one tool that must appear twice** is ruff, because `scripts/tasks.py lint`
  (what `make lint` calls) uses the project venv while the git hook uses
  pre-commit's isolated copy. That invariant is enforced by
  `scripts/check_tool_pins.py`, which runs on every commit that touches either
  file.
- **The coverage threshold** lives only in `[tool.coverage.report] fail_under`.
  Any `pytest --cov` picks it up. It is not repeated in CI args, the Makefile or a
  hook entry.
- **The Python version is canonical in `.python-version`**, which is what CI
  actually uses — there is no `setup-python` step; `setup-uv` provisions the
  interpreter and reads `.python-version` itself. But three other places
  *encode* the same version and cannot read it dynamically: ruff's
  `target-version`, mypy's `python_version`, and the Dockerfile's two `FROM`
  lines. (`requires-python` in `pyproject.toml` is deliberately a *floor* for
  consumers, not the build version, and is checked separately.)
  `scripts/check_python_version.py` is the mechanism, not "nowhere else" —
  it asserts all four agree with `.python-version` on every commit that
  touches any of them, the same way `check_tool_pins.py` does for tool
  versions rather than eliminating the duplication outright.

The reason to care: both older repos carry the comment *"keep in sync with the
pinned ruff in ci.yml — bump both together"*. They are now a year apart, on 0.15.15
and 0.4.4, with different default rules. A comment is not a mechanism.

### Nothing automatic depends on `make`

`scripts/tasks.py` is the actual single source of truth for every command this
repo runs — `uv run --frozen python scripts/tasks.py <task>`. The Makefile is a
thin, optional wrapper around it, for people who like typing `make <target>`.

This split exists because of a mistake made and then caught in this same repo.
An earlier draft made the Makefile itself the source of truth, and had
`.pre-commit-config.yaml` and `ci.yml` call `make <target>` directly — genuinely
fixing the version-drift problem above, but introducing a new one: **GNU Make is
not on a stock Windows machine's PATH** (Python + Git for Windows only, no
WSL/MinGW/choco make), confirmed empirically. Every commit and every push broke
on a Windows machine that had done nothing wrong.

Moving the one-source-of-truth job to a plain Python script fixes both at once:
one place still defines each command, and that place runs anywhere `uv` runs —
which is everywhere, since `uv` is already required for the rest of this setup.
`uv run` also has no `python3`-vs-`python` naming ambiguity to work around,
unlike a bare interpreter invocation would.

### Fast commits, slower pushes

`pre-commit` stage: lint, format, secrets, hygiene. About a second.
`pre-push` stage: mypy over the whole tree, full tests, coverage gate.

Both installed by one `pre-commit install`, via `default_install_hook_types`.

Our older repos run the full suite plus a 95% coverage gate on every commit. Two
problems with that. A coverage number measured over a partially-staged tree does
not mean anything. And a commit hook that takes 30 seconds trains people to pass
`--no-verify`, at which point it protects nothing at all.

---

## What to change per project, honestly

Some things genuinely are project-specific. Expect to revisit:

**The ruff rule set.** The selection here suits a small async service that handles
credentials: `ASYNC` for blocking calls in async code, `S` for hardcoded secrets,
`T20` to keep `print()` out of a server, `DTZ` for naive datetimes. A CLI tool or a
data pipeline would want a different set. Note there are **only two ignores**, both
justified in a comment — resist pasting in the 40-line ignore list from
`vesta-mcp-server`, which exists purely because rules were switched on after the
code was written.

**`strict = true` for mypy.** Correct for a new repo, unaffordable for an existing
one. That is the point of setting it on day one: it is free now and expensive later.

**The `contract` and `e2e` markers.** Named after the test levels in the saved-search
build plan. Rename them to match whatever levels the project actually has, but keep
the principle: tests needing live credentials are excluded from CI and from the
hooks by marker, not by being commented out.

**The PR template's project-specific checklist.** The one here lists the invisible
failure modes of a pooled-session MCP server. Replace it with whatever the new
project's invisible failures are. A generic template that only says "add tests" is
not worth having.

---

## If you later move to shared reusable workflows

The four CI jobs are written so each is self-contained — no cross-job artefacts, no
`needs:` chains except where genuinely required. Lifting them into a
`vestaplus/.github` reusable workflow is mostly mechanical: wrap each job body in
`workflow_call`, and replace the four jobs here with one `uses:` line.

That is the better long-term answer once several repos share this setup, because a
CI fix then propagates without touching each repo. It requires every repo to be in
the same GitHub organisation — private reusable workflows cannot be called from a
repo on a personal account.
