"""Unit tests for the naming/matching helpers (step N5 / VA-401)."""

from __future__ import annotations

import pytest

from vesta_saved_search.models import SavedSearchRecord
from vesta_saved_search.naming import (
    fallback_name,
    find_by_fingerprint,
    find_by_name,
    mentions_unsupported_filter,
    normalise_name,
    url_matches_filters,
)

pytestmark = pytest.mark.unit


def _record(
    *,
    saved_search_id: int = 1,
    name: str = "Del Mar Homes",
    search_filters: dict[str, object] | None = None,
) -> SavedSearchRecord:
    return SavedSearchRecord(
        saved_search_id=saved_search_id,
        name=name,
        search_mode="forSale",
        search_filters=search_filters or {"city": "Del Mar"},
        search_url="explore/listings/saved-search/1/for-sale?city=Del%20Mar",
        notification_frequency="never",
        new_listings_count=0,
        created_at=None,
        last_update=None,
    )


@pytest.mark.parametrize(
    "a,b",
    [
        ("Del Mar Homes", "del mar homes"),
        ("Del Mar Homes", "Del  Mar Homes "),
        ("  Del Mar Homes  ", "Del Mar Homes"),
    ],
)
def test_normalise_name_treats_case_and_whitespace_variants_as_equal(a: str, b: str) -> None:
    assert normalise_name(a) == normalise_name(b)


def test_find_by_name_matches_normalised_names() -> None:
    records = [_record(name="Del Mar Homes")]
    assert find_by_name(records, "del mar homes") is records[0]
    assert find_by_name(records, "  Del  Mar Homes  ") is records[0]


def test_find_by_name_is_exact_not_substring() -> None:
    """The API's own SearchName filter is a substring match -- this must not be."""
    records = [_record(name="TestSaveSearch123")]
    assert find_by_name(records, "TestSaveSearch") is None


def test_find_by_name_returns_none_when_nothing_matches() -> None:
    assert find_by_name([_record(name="A")], "B") is None


def test_find_by_fingerprint_matches_on_recomputed_fingerprint() -> None:
    from vesta_saved_search.fingerprint import criteria_fingerprint

    filters = {"city": "Del Mar", "bedMin": 2}
    records = [_record(search_filters=filters)]
    fp = criteria_fingerprint(filters, "forSale")
    assert find_by_fingerprint(records, fp) is records[0]


def test_find_by_fingerprint_returns_none_for_no_match() -> None:
    records = [_record(search_filters={"city": "Del Mar"})]
    assert find_by_fingerprint(records, "nonexistent-fingerprint") is None


def test_url_matches_filters_accepts_the_real_recorded_payload() -> None:
    """The exact accepted create_daily_request.json shape: a scalar SortBy/mode/
    city/status match exactly, and homeType (a list) needs only one member present."""
    search_filters = {
        "mode": "forSale",
        "city": "Beverly Hills",
        "homeType": ["0", "1"],
        "status": "active,comingSoon",
        "SortBy": "new",
    }
    search_url = (
        "explore/listings/for-sale?city=Beverly%20Hills&status=active,comingSoon"
        "&mode=forSale&homeType=1&sortBy=new"
    )
    assert url_matches_filters(search_url, search_filters) is True


def test_url_matches_filters_rejects_a_contradictory_scalar() -> None:
    """The exact contradiction a human produced by hand: area=11 in the URL,
    area: 17 in searchFilters."""
    search_filters = {"area": "17"}
    search_url = "explore/listings/for-sale?area=11"
    assert url_matches_filters(search_url, search_filters) is False


def test_url_matches_filters_rejects_a_missing_key() -> None:
    assert url_matches_filters("explore/listings/for-sale?city=Del%20Mar", {"bedMin": 2}) is False


def test_url_matches_filters_list_value_needs_at_least_one_member() -> None:
    assert url_matches_filters("explore/listings/for-sale?homeType=2", {"homeType": ["0", "1"]}) is False


def test_url_matches_filters_accepts_a_comma_joined_list_value() -> None:
    """🔴 Discovered running a real end-to-end scenario: build_saved_search_input
    produced homeType=0,1,2 as ONE query param holding a comma-joined value,
    not three separate homeType params -- parse_qs treats the comma as an
    ordinary character, so this must be split explicitly before the overlap
    check or a real, valid three-way selection is wrongly refused."""
    assert (
        url_matches_filters("explore/listings/for-sale?homeType=0,1,2", {"homeType": ["0", "1", "2"]})
        is True
    )


def test_url_matches_filters_comma_joined_value_still_requires_overlap() -> None:
    assert (
        url_matches_filters("explore/listings/for-sale?homeType=3,4,5", {"homeType": ["0", "1"]})
        is False
    )


def test_mentions_unsupported_filter_catches_reordered_wording() -> None:
    """🔴 The highest-value naming test: 'home theater' (unsupported) vs
    'Del Mar Theater Homes' (generated name) -- reversed word order and
    pluralised, so a naive substring check would miss it."""
    assert mentions_unsupported_filter("Del Mar Theater Homes", ["home theater"]) is True


def test_mentions_unsupported_filter_does_not_false_positive_on_short_words() -> None:
    assert mentions_unsupported_filter("Del Mar Homes", ["home theater"]) is False


def test_mentions_unsupported_filter_ignores_stopwords() -> None:
    assert mentions_unsupported_filter("Homes with a View", ["with a pool"]) is False


def test_mentions_unsupported_filter_with_no_unsupported_filters() -> None:
    assert mentions_unsupported_filter("Del Mar Homes", []) is False


def test_fallback_name_is_derived_from_criteria_summary() -> None:
    result = fallback_name("Del Mar, 2+ bed, for sale", max_length=60)
    assert result == "Del Mar, 2+ bed, for sale Search"
    assert "area" not in result
    assert "11" not in result


def test_fallback_name_truncates_to_max_length() -> None:
    result = fallback_name("A" * 100, max_length=20)
    assert len(result) == 20


def test_fallback_name_handles_blank_summary() -> None:
    assert fallback_name("   ", max_length=60) == "Saved Search"
