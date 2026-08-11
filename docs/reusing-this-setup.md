# Reusing this setup on a new project

This CI and pre-commit setup is designed to be copied. Everything in it is
project-agnostic except five values in `pyproject.toml` and two lines in the
`Dockerfile`.

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
.gitignore
.python-version
.dockerignore
Makefile
scripts/bootstrap.py
scripts/tasks.py
scripts/check_tool_pins.py
scripts/check_python_version.py
scripts/secrets_baseline.py
tests/conftest.py
tests/test_tooling.py
tests/test_toolchain.py
```

None of them name this project, list source directories, pin a threshold, or
reference a module. There is nothing to edit.

⚠️ **`tests/test_toolchain.py` is on that list for a reason.** An empty `tests/`
directory means pytest collects nothing, exits 5, and reports 0% coverage against
`fail_under = 95` — so the `tests` CI job and the pre-push hook both go red on the
first commit of a fresh copy. Copy it and change the import to your package name.

`tests/test_tooling.py` tests the tooling itself (the drift checks, the task
registry, the `clean` safety list). It is worth copying: those mechanisms are what
make the rest of this setup trustworthy, and they were once the least-verified code
in the repo.

**The `Dockerfile` is NOT in the list** — it is the one other file with
project-specific content. Copy it, then change the two lines at the bottom: `CMD`
(the module to run) and `EXPOSE` (the port).

**2. Copy `pyproject.toml` and change five values:**

| What | Where |
|---|---|
| project name | `[project] name` |
| description | `[project] description` |
| package path | `[tool.hatch.build.targets.wheel] packages` |
| version source | `[tool.hatch.version] path` |
| first-party name | `[tool.ruff.lint.isort] known-first-party` |

Then create `src/<your_package>/__init__.py` with a `__version__` (the build reads
the version from it, so `[project]` declares `dynamic = ["version"]` and there is
no second copy to drift) and a `py.typed` beside it.

**Do not touch `[tool.coverage.run] source`** — it is `["src"]`, which is generic.
An earlier version of this table wrongly listed it as naming the package; editing
it breaks coverage while leaving hatch's path stale produces an empty wheel.

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

In both older repos the set of source paths is restated in several files, and the
shape differs per repo — verified rather than assumed:

- **`vesta-mcp-server`**: `SOURCES` and `COV_MODULES` in the `Makefile`, `SOURCES`
  again in `ci.yml`, plus a chain of `--cov=` flags in both the Makefile and the
  pytest hook.
- **`smart-search-for-guestSide`**: no `SOURCES` variable at all — `ci.yml`
  hardcodes `orchestrator backend core tests` inline, and
  `.pre-commit-config.yaml` repeats `files: ^(orchestrator|backend|core|tests)/`
  in **four** separate hook definitions.

So it is not one tidy list of four; it is a different list in almost every file.
Adding a directory means finding all of them, and forgetting one means the
directory silently stops being linted, type-checked or counted in coverage — with
nothing failing, so nobody finds out.

With everything under `src/`, every tool targets `src` and `tests`. The lists do
not exist, so they cannot drift or be forgotten.

### One source of truth for every version

- **Tool versions** live in `.pre-commit-config.yaml`. CI runs
  `pre-commit run --all-files` rather than installing ruff itself, so the workflow
  contains no version at all.
- **Two tools must appear twice** — ruff and detect-secrets — because
  `scripts/tasks.py lint` and `scripts/tasks.py secrets-baseline` (what `make
  lint` / `make secrets-baseline` call) use the project venv while the git hooks
  use pre-commit's isolated copies. **uv appears three times**: the `uv-lock`
  hook's rev, every `setup-uv` step in `ci.yml`, and the Dockerfile's
  `COPY --from=ghcr.io/astral-sh/uv`. Every one of those pairs is enforced by
  `scripts/check_tool_pins.py`, which also fails if a newly exact-pinned dev
  dependency has no row in its table — so the table cannot quietly fall behind.
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

The reason to care, stated accurately — the earlier version of this paragraph got
the details wrong, which is its own argument for checking claims:

Both older repos pin ruff **twice**, in `.pre-commit-config.yaml` and in `ci.yml`
(`pip install ruff==...`), each with a "keep in sync" comment. Neither pins it in
`pyproject.toml`, so the pair this repo checks is not the pair they duplicate.
And **within** each repo the two currently agree — the comment has held so far.
What is actually true is narrower and still worth fixing: the two repos are a year
apart (0.4.4 vs 0.15.15) with different default rules, the same value lives in two
files in each, and **nothing anywhere would notice the moment one slipped**. A
comment is a request; it is not a mechanism, and it is not a test.

### Nothing automatic depends on `make`

`scripts/tasks.py` is the actual single source of truth for every command this
repo runs — `uv run --frozen python scripts/tasks.py <task>` — with one deliberate
exception: `setup`, which has to run before a virtualenv exists and so cannot go
through `uv run`. It calls `scripts/bootstrap.py` with a bare interpreter instead.
The Makefile is a thin, optional wrapper, for people who like typing
`make <target>`; `tests/test_tooling.py` asserts its target list matches the task
registry, so the two cannot drift apart unnoticed.

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
data pipeline would want a different set. Note there are only **two project-wide
ignores**, plus a handful of narrowly scoped per-file ones (`scripts/**` may print;
`tests/**` may assert and hold fake credentials) — all commented. Resist pasting in
the 40-line ignore list from
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

The five CI jobs are written so each is self-contained — no cross-job artefacts, no
`needs:` chains at all. Lifting them into a
`vestaplus/.github` reusable workflow is mostly mechanical: wrap each job body in
`workflow_call`, and replace the jobs here with one `uses:` line each.

That is the better long-term answer once several repos share this setup, because a
CI fix then propagates without touching each repo. It requires every repo to be in
the same GitHub organisation — private reusable workflows cannot be called from a
repo on a personal account.
