"""Unit tests for criteria_fingerprint (step N4 / VA-400).

Level 1 per the build plan. The golden test replays REAL recorded bytes on
both sides -- the request that created a record and the response that stored
it -- rather than a hand-written approximation of either.
"""

from __future__ import annotations

import pytest

from _guestsite_fixtures import fixture_body
from vesta_saved_search.fingerprint import criteria_fingerprint

pytestmark = pytest.mark.unit


def _stored_search_filters() -> dict[str, object]:
    """The real stored `searchFilters`, decoded, from create_daily.json."""
    import json

    body = fixture_body("create_daily")
    decoded = json.loads(body["savedSearch"])[0]
    raw_filters = decoded["searchFilters"]
    parsed: dict[str, object] = json.loads(raw_filters)
    return parsed


def _sent_search_filters() -> dict[str, object]:
    """The real request `searchFilters` that produced that stored record."""
    body = fixture_body("create_daily_request")
    filters: dict[str, object] = body["searchFilters"]
    return filters


def test_real_stored_record_matches_freshly_built_payload() -> None:
    """🔴 The single highest-value test in this module.

    Fingerprint the REAL stored record (create_daily.json) and the REAL
    request body that created it (create_daily_request.json) and assert they
    are equal. This is the difference between duplicate detection working
    and being decorative: if the drop-list or its casing is wrong, this is
    the test that catches it -- everything else here is a hand-written
    unit test of the rules, not proof the rules survive contact with the
    real API's round-trip.
    """
    stored_fingerprint = criteria_fingerprint(_stored_search_filters(), "forSale")
    sent_fingerprint = criteria_fingerprint(_sent_search_filters(), "forSale")
    assert stored_fingerprint == sent_fingerprint


def test_sortby_case_variants_both_dropped() -> None:
    lower = criteria_fingerprint({"city": "Del Mar", "sortby": "new"}, "forSale")
    upper = criteria_fingerprint({"city": "Del Mar", "SortBy": "new"}, "forSale")
    neither = criteria_fingerprint({"city": "Del Mar"}, "forSale")
    assert lower == upper == neither


def test_consumerid_case_variants_both_dropped() -> None:
    lower = criteria_fingerprint({"city": "Del Mar", "consumerid": 5}, "forSale")
    mixed = criteria_fingerprint({"city": "Del Mar", "consumerId": 5}, "forSale")
    neither = criteria_fingerprint({"city": "Del Mar"}, "forSale")
    assert lower == mixed == neither


def test_mode_key_inside_filters_is_dropped() -> None:
    with_mode = criteria_fingerprint({"city": "Del Mar", "mode": "forSale"}, "forSale")
    without_mode = criteria_fingerprint({"city": "Del Mar"}, "forSale")
    assert with_mode == without_mode


def test_key_order_does_not_change_the_result() -> None:
    a = criteria_fingerprint({"city": "Del Mar", "bedMin": 2}, "forSale")
    b = criteria_fingerprint({"bedMin": 2, "city": "Del Mar"}, "forSale")
    assert a == b


def test_list_order_does_not_change_the_result() -> None:
    a = criteria_fingerprint({"homeType": ["0", "1"]}, "forSale")
    b = criteria_fingerprint({"homeType": ["1", "0"]}, "forSale")
    assert a == b


def test_different_search_mode_changes_the_fingerprint() -> None:
    for_sale = criteria_fingerprint({"city": "Del Mar"}, "forSale")
    for_rent = criteria_fingerprint({"city": "Del Mar"}, "forRent")
    assert for_sale != for_rent


@pytest.mark.parametrize("empty_value", [None, "", [], {}, False])
def test_empty_ish_values_are_dropped(empty_value: object) -> None:
    with_empty = criteria_fingerprint({"city": "Del Mar", "extra": empty_value}, "forSale")
    without = criteria_fingerprint({"city": "Del Mar"}, "forSale")
    assert with_empty == without


def test_zero_is_not_treated_as_empty() -> None:
    """`False == 0` in Python -- this guards against `== False` instead of `is False`."""
    with_zero = criteria_fingerprint({"bedMin": 0}, "forSale")
    without = criteria_fingerprint({}, "forSale")
    assert with_zero != without


def test_whitespace_and_casing_do_not_change_the_result() -> None:
    a = criteria_fingerprint({"city": "Del Mar"}, "forSale")
    b = criteria_fingerprint({"city": "  del mar  "}, "forSale")
    assert a == b


def test_unsupported_filters_have_no_effect() -> None:
    """`unsupportedFilters` is not a parameter at all -- excluded by construction.

    Deliberate consequence per the module docstring: two searches differing
    only in a filter this MLS cannot apply fingerprint identically.
    """
    a = criteria_fingerprint({"city": "Del Mar"}, "forSale")
    # No unsupportedFilters parameter exists to pass -- this test documents
    # that the function signature itself makes them inexpressible, not that
    # some value was tried and ignored.
    assert a == criteria_fingerprint({"city": "Del Mar"}, "forSale")


def test_nested_dict_values_are_recursed_and_normalised() -> None:
    a = criteria_fingerprint({"priceRange": {"min": 100, "MAX": 200}}, "forSale")
    b = criteria_fingerprint({"priceRange": {"MIN": 100, "max": 200}}, "forSale")
    assert a == b


def test_returns_a_sha256_hex_digest() -> None:
    result = criteria_fingerprint({"city": "Del Mar"}, "forSale")
    assert len(result) == 64
    int(result, 16)  # raises ValueError if not valid hex
