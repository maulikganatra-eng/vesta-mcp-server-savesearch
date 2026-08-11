# Multi-stage build. Generic for any uv project using the src/ layout — the only
# project-specific line is the CMD at the very bottom.

# ---- Stage 1: resolve dependencies into a venv -----------------------------
FROM python:3.11-slim AS builder

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

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
COPY README.md ./
RUN uv sync --frozen --no-dev

# ---- Stage 2: runtime ------------------------------------------------------
FROM python:3.11-slim

# Run as a non-root user. If the process is ever compromised it should not own
# the filesystem it is running on.
RUN useradd --create-home --uid 10001 app
WORKDIR /app

COPY --from=builder --chown=app:app /app/.venv /app/.venv
COPY --from=builder --chown=app:app /app/src /app/src

ENV PATH="/app/.venv/bin:$PATH" \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

USER app

EXPOSE 6001

# The only project-specific line in this file. Points at the FastMCP app once
# step N1 creates it.
CMD ["python", "-m", "vesta_saved_search.server"]
