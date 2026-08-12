"""Unit tests for SavedSearchRecord decoding, against REAL recorded bytes.

Every case here is fed a real response from tests/fixtures/guestsite_dev/, per the
acceptance criteria: "one unit test per trap, each fed a real recorded response,
not a hand-written one."
"""

from __future__ import annotations

import json

import pytest

from _guestsite_fixtures import fixture_body
from vesta_saved_search.errors import SavedSearchUnexpectedResponseError
from vesta_saved_search.models import SavedSearchRecord

pytestmark = pytest.mark.unit


def _first_record_from(fixture_name: str) -> dict[str, object]:
    """Get one raw record out of a fixture, whichever of the two real envelopes it is.

    `list_baseline` etc. are a bare JSON array. `create_daily`,
    `create_instantly` and `update_rename_omit_schedule` are the OTHER real
    envelope: `{"status": "ok", "savedSearch": "<json array string>"}` -- the
    double-decode trap this whole client exists to undo. Doing that decode by
    hand here, rather than importing the client's own decoder, keeps this test
    fixture-only: it must not depend on the very code under test to interpret
    its own fixtures.
    """
    body = fixture_body(fixture_name)
    if isinstance(body, list):
        result: dict[str, object] = body[0]
        return result
    if isinstance(body, dict) and isinstance(body.get("savedSearch"), str):
        decoded = json.loads(body["savedSearch"])
        result = decoded[0]
        return result
    raise TypeError(f"fixture {fixture_name!r} is neither a list nor a double-decode envelope")


def test_search_filters_decoded_from_string_to_dict() -> None:
    """🔴 Trap 2: searchFilters is a JSON string on the wire, sent as an object."""
    raw = _first_record_from("list_baseline")
    assert isinstance(raw["searchFilters"], str), "fixture assumption: still a string on disk"

    record = SavedSearchRecord.from_api(raw)

    assert isinstance(record.search_filters, dict)


def test_consumer_id_survives_in_decoded_filters() -> None:
    """🔴 Trap 3: consumerId is injected inside stored searchFilters.

    Left in place here on purpose -- N4's fingerprint step is what has to exclude
    it, and it can only do that if this layer does not hide it first.
    """
    raw = _first_record_from("list_baseline")
    record = SavedSearchRecord.from_api(raw)
    assert "consumerId" in record.search_filters


def test_casing_trap_is_visible_in_the_decoded_dict() -> None:
    """🔴 Trap 4: sent "SortBy", the API stores and returns "sortBy".

    This test does not fix the casing -- it exists so that if some future refactor
    accidentally normalises it away silently, this fails loudly instead of N4's
    fingerprint drop-list quietly starting to work by accident.
    """
    raw = _first_record_from("create_daily")
    record = SavedSearchRecord.from_api(raw)
    assert "sortBy" in record.search_filters
    assert "SortBy" not in record.search_filters


@pytest.mark.parametrize(
    ("fixture_name", "expected_frequency"),
    [
        ("create_daily", "daily"),
        ("create_instantly", "instantly"),
    ],
)
def test_frequency_decoded_from_real_pairs(fixture_name: str, expected_frequency: str) -> None:
    raw = _first_record_from(fixture_name)
    record = SavedSearchRecord.from_api(raw)
    assert record.notification_frequency == expected_frequency


def test_double_replace_trap_is_visible_after_decode() -> None:
    """🔴 The URL-placeholder trap, confirmed live on dev (not just sprint).

    Sending the plain create-shaped URL on an update stored the LITERAL string
    "{SavedSearchId}" rather than substituting the real id. This model layer does
    not correct it -- N7's precondition is what has to refuse it before sending --
    but decoding must not silently paper over it either.
    """
    raw = _first_record_from("update_rename_omit_schedule")
    record = SavedSearchRecord.from_api(raw)
    assert "{SavedSearchId}" in record.search_url


def test_full_replace_trap_is_visible_after_decode() -> None:
    """🔴 The full-replace trap, confirmed live on dev: a Daily record renamed
    without resending scheduleInterval came back with scheduleId=null.
    """
    raw = _first_record_from("update_rename_omit_schedule")
    record = SavedSearchRecord.from_api(raw)
    assert record.notification_frequency == "never", (
        "the recorded fixture shows the frequency was reset by the omission -- "
        "if this ever starts asserting 'daily' the API's full-replace behaviour "
        "changed and N7's whole justification needs re-checking"
    )


def test_missing_search_filters_decodes_to_empty_dict() -> None:
    raw = dict(_first_record_from("list_baseline"))
    raw["searchFilters"] = None
    record = SavedSearchRecord.from_api(raw)
    assert record.search_filters == {}


def test_malformed_search_filters_raises_not_silently_empties() -> None:
    """A record whose searchFilters cannot be parsed is a signal, not a shrug.

    An empty dict here would make every filter look absent -- indistinguishable
    from a search that genuinely has no filters -- which is a worse failure than
    a loud error.
    """
    raw = dict(_first_record_from("list_baseline"))
    raw["searchFilters"] = "{not valid json"
    with pytest.raises(SavedSearchUnexpectedResponseError, match="not valid JSON"):
        SavedSearchRecord.from_api(raw)


def test_search_filters_already_a_dict_is_accepted_defensively() -> None:
    """Not an observed API shape, but a test fixture might hand-construct one."""
    raw = dict(_first_record_from("list_baseline"))
    raw["searchFilters"] = {"already": "a dict"}
    record = SavedSearchRecord.from_api(raw)
    assert record.search_filters == {"already": "a dict"}


def test_search_filters_of_wrong_type_raises() -> None:
    raw = dict(_first_record_from("list_baseline"))
    raw["searchFilters"] = 12345
    with pytest.raises(SavedSearchUnexpectedResponseError, match="unexpected type"):
        SavedSearchRecord.from_api(raw)


def test_search_filters_decoding_to_a_list_raises() -> None:
    raw = dict(_first_record_from("list_baseline"))
    raw["searchFilters"] = json.dumps(["not", "an", "object"])
    with pytest.raises(SavedSearchUnexpectedResponseError, match="expected an object"):
        SavedSearchRecord.from_api(raw)


@pytest.mark.parametrize("missing_key", ["savedSearchId", "searchName", "searchType", "searchUrl"])
def test_missing_required_field_raises_typed_error_not_bare_key_error(missing_key: str) -> None:
    """🔴 The gap flagged in review: these four fields were bare dict indexing.

    A bare KeyError is not a SavedSearchApiError, so it would escape tools.py's
    `except SavedSearchApiError` as a raw crash instead of the clean
    {"status": "error", ...} envelope every other failure path in this client
    produces. Never observed missing on dev -- this is defence for a shape the
    upstream has not shown us yet, matching how `searchFilters` is already
    guarded three ways.
    """
    raw = dict(_first_record_from("list_baseline"))
    del raw[missing_key]
    with pytest.raises(SavedSearchUnexpectedResponseError, match="missing required key"):
        SavedSearchRecord.from_api(raw)


def test_new_listings_count_defaults_to_zero_when_absent() -> None:
    raw = dict(_first_record_from("list_baseline"))
    del raw["newListingsSinceCertainDate"]
    record = SavedSearchRecord.from_api(raw)
    assert record.new_listings_count == 0


def test_every_baseline_record_decodes_without_error() -> None:
    """The whole real list, not just its first element -- some records use `area`
    instead of `city` in their filters, which must decode just as cleanly.
    """
    body = fixture_body("list_baseline")
    records = [SavedSearchRecord.from_api(raw) for raw in body]
    assert len(records) == len(body)
    assert all(isinstance(r.search_filters, dict) for r in records)
