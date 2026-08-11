## What and why

<!-- One or two sentences. What changes, and what breaks or stays broken without it. -->

Step from the build plan (if applicable): <!-- e.g. N2 -->

## Checklist

- [ ] `make check` passes locally (lint, mypy, tests + coverage gate)
- [ ] New behaviour has a test that fails without the change
- [ ] No secret, token or real bearer value in code, tests, fixtures or commit history

## Checks specific to this server

These are the failures that are invisible when you get them wrong, so they are
listed rather than left to memory.

- [ ] **No user-specific data cached on module or session state.** MCP sessions are
      pooled and shared across users. A module-level dict keyed on anything but the
      caller's identity is a cross-user leak, not a performance win.
- [ ] **Identity comes from the token, never from a tool argument.** No tool takes a
      user id.
- [ ] **Writes are confirmed before they happen**, and an unconfirmed call writes
      nothing.
- [ ] **An upstream error surfaces as an error.** Never a success status with no write
      behind it.
