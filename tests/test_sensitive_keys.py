"""Unit tests for the sensitive-key check, and for this tool's own field names."""

from __future__ import annotations

import pytest

from vesta_saved_search.sensitive_keys import (
    SENSITIVE_KEY_SUBSTRINGS,
    contains_sensitive_substring,
)
from vesta_saved_search.tools import PROPOSAL_FIELD_NAMES, RECORD_FIELD_NAMES

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("substring", SENSITIVE_KEY_SUBSTRINGS)
def test_a_field_named_exactly_the_substring_is_flagged(substring: str) -> None:
    assert contains_sensitive_substring(substring) is True


@pytest.mark.parametrize("substring", SENSITIVE_KEY_SUBSTRINGS)
def test_match_is_case_insensitive(substring: str) -> None:
    assert contains_sensitive_substring(substring.upper()) is True


def test_ordinary_field_name_is_not_flagged() -> None:
    assert contains_sensitive_substring("searchUrl") is False


@pytest.mark.parametrize("field_name", RECORD_FIELD_NAMES)
def test_no_record_field_name_collides_with_a_sensitive_substring(field_name: str) -> None:
    """🔴 The check this whole module exists for.

    A field here named e.g. `authorization` or `bearerToken` would be SILENTLY
    deleted from the SSE payload by the orchestrator's `_strip_sensitive` — the
    tool would look like it worked and the field would just be gone, with no error
    anywhere. Looping over the real field-name tuple, rather than hand-picking a
    few to check, means a field added to `RECORD_FIELD_NAMES` later is covered by
    this test automatically.
    """
    assert not contains_sensitive_substring(field_name), (
        f"{field_name!r} contains a substring the orchestrator strips as sensitive "
        "-- it would be silently deleted from the SSE payload"
    )


@pytest.mark.parametrize("field_name", PROPOSAL_FIELD_NAMES)
def test_no_proposal_field_name_collides_with_a_sensitive_substring(field_name: str) -> None:
    """Same check as above, for the fields propose_saved_search / save_search emit."""
    assert not contains_sensitive_substring(field_name), (
        f"{field_name!r} contains a substring the orchestrator strips as sensitive "
        "-- it would be silently deleted from the SSE payload"
    )
