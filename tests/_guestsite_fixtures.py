"""Loader for the real dev-recorded API responses in tests/fixtures/guestsite_dev/.

Every test that needs "a real recorded response" per the acceptance criteria
(rather than a hand-written approximation) loads through :func:`load_fixture`.
Flat module, not a package, to match this repo's existing test layout — nothing
else under ``tests/`` uses a nested package either.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_FIXTURES_DIR = Path(__file__).resolve().parent / "fixtures" / "guestsite_dev"


def load_fixture(name: str) -> dict[str, Any]:
    """Load one recorded response by name (without the ``.json`` suffix).

    Returns the full recording envelope: ``_status_code``, ``_request_method``,
    ``_request_url``, and ``body`` (the real, unmodified response body).
    """
    path = _FIXTURES_DIR / f"{name}.json"
    parsed: dict[str, Any] = json.loads(path.read_text())
    return parsed


def fixture_body(name: str) -> Any:
    """Load one recorded response and return just its ``body``."""
    return load_fixture(name)["body"]


def fixture_status(name: str) -> int:
    """Load one recorded response and return just its recorded HTTP status."""
    status = load_fixture(name)["_status_code"]
    if not isinstance(status, int):
        raise TypeError(f"fixture {name!r} has a non-int _status_code: {status!r}")
    return status
