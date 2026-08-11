# vesta-mcp-server-savesearch

MCP tool server for MLS saved searches — list, propose, save, update and delete a
user's saved property searches.

The build order, the tests for each step and the reasoning behind them live in
`notes/saved-search-incremental-build-and-test-plan.md` in the backend repo
(`smart-search-for-guestSide`). **This repo implements Stream B of that plan,
steps N1 to N9.** Read Stream B before writing code here.

---

## Getting started

```bash
git clone https://github.com/<owner>/vesta-mcp-server-savesearch.git
cd vesta-mcp-server-savesearch
make setup
```

`make setup` needs only Python 3.11+ and [uv](https://docs.astral.sh/uv/). It
installs dependencies, installs both git hooks, creates the secrets baseline, and
verifies every tool actually runs. It is safe to re-run.

If uv is missing:

```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

**`make` itself is optional.** Nothing in this repo's git hooks or CI depends on
it — GNU Make is not on a stock Windows machine's PATH, so every `make <target>`
below has an exact equivalent with no `make` involved:

```bash
python scripts/bootstrap.py                        # same as `make setup`
uv run --frozen python scripts/tasks.py <task>      # same as `make <task>`
```

## Daily commands

```bash
make check        # everything CI runs: lint, mypy, tests + coverage gate
make test         # fast suite only
make format       # auto-fix lint and format
make help         # all targets
```

You rarely need to run these by hand — the git hooks cover it.

## What runs when

| When | What runs | Roughly |
|---|---|---|
| `git commit` | ruff lint, ruff format, secret scan, file hygiene, lock freshness, pin check | ~1s |
| `git push` | mypy (strict, whole tree) + full test suite with the coverage gate | seconds |
| pull request | all of the above, in parallel jobs, plus an advisory CVE audit | ~1 min |

The split is deliberate. Commits stay fast so nobody reaches for `--no-verify`;
the slow checks still run before code leaves your machine.

Tests marked `contract` or `e2e` need real credentials and never run in CI or in
the hooks. Run them deliberately with `make test-contract`.

## Layout

```
src/vesta_saved_search/   the package — all product code goes here
tests/                    mirrors src/
scripts/                  developer tooling (bootstrap, pin check)
```

The `src/` layout is load-bearing, not a style choice: it means every tool config
targets `src` and `tests` and never a hand-maintained list of module names. See
`docs/reusing-this-setup.md`.

## Reusing this setup on another project

Every config file here except `pyproject.toml` is project-agnostic. Copy them into
a new repo and change three values. See `docs/reusing-this-setup.md`.
