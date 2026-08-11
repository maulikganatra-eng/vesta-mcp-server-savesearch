# Multi-stage build. Generic for any uv project using the src/ layout — the only
# project-specific lines are EXPOSE and CMD, both at the very bottom.

# ---- Stage 1: resolve dependencies into a venv -----------------------------
FROM python:3.14-slim AS builder

# Pinned, not :latest. A mutable tag means two builds of the same commit can use
# different uv versions, and it is the one uv in this repo that scripts/
# check_tool_pins.py cannot see. Keep equal to the uv pinned in
# .pre-commit-config.yaml and .github/workflows/ci.yml.
COPY --from=ghcr.io/astral-sh/uv:0.11.8 /uv /usr/local/bin/uv

WORKDIR /app

# Copy only the dependency manifests first, so this layer is cached and does not
# rebuild every time application code changes.
COPY pyproject.toml uv.lock ./

# --frozen fails if uv.lock disagrees with pyproject.toml, so a stale lock is a
# build error rather than a container running unexpected versions.
# --no-install-project installs dependencies only; the app is added below.
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy
RUN uv sync --frozen --no-dev --no-install-project

COPY src ./src
# README.md is required: pyproject.toml declares readme = "README.md", so the
# build fails without it. (It is deliberately NOT in .dockerignore.)
COPY README.md ./

# --no-editable matters. Without it uv installs the project in editable mode, so
# the venv holds a path reference to /app/src and the runtime image only works
# because the stage below happened to copy src to the identical absolute path.
# Changing WORKDIR would then produce an ImportError at container start rather
# than at build time. Installed non-editably, the package lives in site-packages
# and the runtime stage needs no src/ at all.
RUN uv sync --frozen --no-dev --no-editable

# ---- Stage 2: runtime ------------------------------------------------------
FROM python:3.14-slim

# Run as a non-root user. If the process is ever compromised it should not own
# the filesystem it is running on.
RUN useradd --create-home --uid 10001 app

# Create and own the workdir explicitly. WORKDIR alone creates it as root, so a
# process running as `app` could not write to its own cwd.
WORKDIR /app
RUN chown app:app /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

# ---- The two project-specific lines ----------------------------------------

# 6001, not 6000: the property-search MCP server already owns 6000, and these run
# side by side. See the capability plan's two-server split.
EXPOSE 6001

# Points at the FastMCP app that step N1 creates. Until N1 lands, `docker build`
# succeeds and `docker run` exits immediately with ModuleNotFoundError — this is
# scaffolding, and CI's docker job deliberately builds the image without running
# it. There is no HEALTHCHECK yet for the same reason: N1 adds the /health route
# it would call, and a HEALTHCHECK against a route that does not exist would
# report every container as unhealthy rather than catching anything.
CMD ["python", "-m", "vesta_saved_search.server"]
