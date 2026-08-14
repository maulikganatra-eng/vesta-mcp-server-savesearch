# vesta-mcp-server-savesearch

MCP tool server for MLS saved searches — list, propose, save, update and delete a
user's saved property searches.

## Response envelope

Every tool wraps its response under a single top-level key **named after the
tool itself** — `{"list_saved_searches": {...}}`, `{"save_search": {...}}`, and
so on (the exported constants in `src/vesta_saved_search/tools.py`, e.g.
`LIST_SAVED_SEARCHES_KEY`, are the source of truth; import them in new tests
rather than hardcoding the literal). This matches `vesta-mcp-server`'s own
convention — "each tool returns a single-key envelope named after the tool"
(that repo's README) — and it is not merely a convention on this side: the
orchestrator's `capability_executor.py` now derives its internal grouping key
(`mode_key`) from the calling MCP tool's own name, unconditionally, rather
than trusting whatever key a server happens to wrap under. An earlier version
of this server wrapped every tool under one shared constant instead
(`"saved_search"`), which the orchestrator was still trusting for naming at
the time — two different tool calls in the same turn collided on that one key.
The orchestrator-side fix means a server that still did this today would no
longer corrupt anything, but it would still produce an uninformative key, so
keep this convention regardless.

The build order, the tests for each step and the reasoning behind them live in
`notes/saved-search-incremental-build-and-test-plan.md` in the backend repo
(`smart-search-for-guestSide`). **This repo implements steps N1–N9 of that plan**,
which are phases 5, 7 and 9 of its build sequence. Read those three phases before
writing code here. (That `notes/` directory is untracked in the backend repo, so a
fresh clone will not have the file — ask for a copy.)

---

## Getting started

```bash
git clone https://github.com/<owner>/vesta-mcp-server-savesearch.git
cd vesta-mcp-server-savesearch
make setup
```

`make setup` needs only Python 3.11+ and [uv](https://docs.astral.sh/uv/). It
installs dependencies, installs both git hooks, creates the secrets baseline, and
verifies that ruff, mypy and pytest actually run. It is safe to re-run.

If uv is missing:

```bash
# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh
# Windows (there is no `sh` in cmd or PowerShell)
powershell -ExecutionPolicy ByPass -c "irm https://astral.sh/uv/install.ps1 | iex"
```

**`make` itself is optional.** Nothing in this repo's git hooks or CI depends on
it — GNU Make is not on a stock Windows machine's PATH, so every `make <target>`
below has an exact equivalent with no `make` involved:

```bash
python scripts/bootstrap.py                         # same as `make setup`
uv run --frozen python scripts/tasks.py <task>      # same as `make <task>`
uv run --frozen python scripts/tasks.py help        # every task, with descriptions
```

`setup` is the one thing that is not a `tasks.py` task: it has to run before a
virtualenv exists, so it cannot go through `uv run`.

## Daily commands

```bash
make check        # the three blocking CI jobs: fast hooks, mypy, tests + coverage
make test         # fast suite only
make format       # auto-fix lint and format
make audit        # report known CVEs in the locked dependency set
make help         # every task, with descriptions
```

`check` deliberately excludes the dependency audit, which is advisory in CI, and
needs network access. Run `make audit` when you want it.

You rarely need to run these by hand — the git hooks cover it.

## What runs when

| When | What runs | Roughly |
|---|---|---|
| `git commit` | ruff lint, ruff format, secret scan, file hygiene, lock freshness, version-pin checks | ~1s |
| `git push` | mypy (strict, whole tree) + full test suite with the coverage gate | seconds |
| pull request | five parallel jobs: the fast hooks, mypy, tests, a Docker build, and an advisory CVE audit | ~1 min |

The split is deliberate. Commits stay fast so nobody reaches for `--no-verify`;
the slow checks still run before code leaves your machine.

Tests marked `contract` or `e2e` hit live services, need real credentials, and
never run in CI or in the hooks. Run them deliberately:

```bash
make test-contract   # -m contract
make test-e2e        # -m e2e
```

## Layout

```
src/vesta_saved_search/   the package — all product code goes here
tests/                    will mirror src/ as it grows; also tests the tooling
scripts/                  developer tooling (bootstrap, task runner, consistency checks)
```

The `src/` layout is load-bearing, not a style choice: it means every tool config
targets `src` and `tests` and never a hand-maintained list of module names. See
`docs/reusing-this-setup.md`.

## Reusing this setup on another project

Every config file here is project-agnostic except `pyproject.toml` (five values)
and the `Dockerfile` (its `CMD` module path and `EXPOSE` port). Copy the rest
unchanged. See `docs/reusing-this-setup.md` for the exact list.
