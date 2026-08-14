"""Unit tests for the naming/matching helpers (step N5 / VA-401)."""

from __future__ import annotations

import pytest

from vesta_saved_search.models import SavedSearchRecord
from vesta_saved_search.naming import (
    dedupe_fallback_name,
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


def test_fallback_name_strips_words_that_mention_unsupported_filters() -> None:
    """🔴 The fallback must not reintroduce the exact defect
    mentions_unsupported_filter exists to prevent -- a summary that itself
    names an unsupported filter must have that word dropped."""
    result = fallback_name(
        "Del Mar with a home theater",
        max_length=60,
        unsupported_filters=["home theater"],
    )
    assert "theater" not in result.lower()
    assert "Del Mar" in result


def test_fallback_name_with_no_unsupported_filters_is_unchanged() -> None:
    result = fallback_name("Del Mar, 2+ bed", max_length=60, unsupported_filters=[])
    assert result == "Del Mar, 2+ bed Search"


def test_fallback_name_with_only_stopwords_in_unsupported_filters_is_unchanged() -> None:
    """No significant word survives the stopword/length filter, so there is
    nothing to strip -- the summary passes through untouched."""
    result = fallback_name("Del Mar, 2+ bed", max_length=60, unsupported_filters=["with a"])
    assert result == "Del Mar, 2+ bed Search"


def test_fallback_name_that_becomes_blank_after_stripping_falls_back_to_saved_search() -> None:
    result = fallback_name("home theater", max_length=60, unsupported_filters=["home theater"])
    assert result == "Saved Search"


def test_dedupe_fallback_name_returns_the_name_unchanged_when_no_collision() -> None:
    records = [_record(name="Something Else")]
    assert dedupe_fallback_name("Del Mar Search", records, max_length=60) == "Del Mar Search"


def test_dedupe_fallback_name_appends_the_smallest_clearing_suffix() -> None:
    """🔴 Two structurally different searches can share the same
    criteriaSummary-derived fallback -- this is what stops that collision
    from surfacing only at confirm time as name_exists."""
    records = [_record(name="Del Mar Search"), _record(name="Del Mar Search (2)")]
    result = dedupe_fallback_name("Del Mar Search", records, max_length=60)
    assert result == "Del Mar Search (3)"
    assert find_by_name(records, result) is None


def test_dedupe_fallback_name_tries_suffixes_in_order() -> None:
    records = [_record(name="X Search")]
    assert dedupe_fallback_name("X Search", records, max_length=60) == "X Search (2)"


def test_dedupe_fallback_name_avoids_names_not_present_in_records() -> None:
    """🔴 `avoid_names` covers names that are not a collision with any OTHER
    record -- namely `update_saved_search`'s own current name, which the
    caller deliberately excludes from `records` (it is not a collision with
    a DIFFERENT search). Without this, the deterministic fallback could
    coincidentally reproduce the record's own unchanged name, silently
    collapsing an intended rename into a no-op."""
    result = dedupe_fallback_name("Del Mar Search", [], max_length=60, avoid_names=("Del Mar Search",))
    assert result == "Del Mar Search (2)"


def test_dedupe_fallback_name_avoid_names_combines_with_records() -> None:
    records = [_record(name="Del Mar Search (2)")]
    result = dedupe_fallback_name(
        "Del Mar Search", records, max_length=60, avoid_names=("Del Mar Search",)
    )
    assert result == "Del Mar Search (3)"


def test_dedupe_fallback_name_clamps_rather_than_uses_a_negative_slice() -> None:
    """🔴 name[:max_length - len(suffix)] goes negative (and silently slices
    from the wrong end instead of raising) once a multi-digit suffix like
    " (10)" would overrun a small max_length. Force ten collisions so the
    loop actually reaches a two-digit suffix, and assert the result is a
    real, correctly-suffixed name rather than a mangled one."""
    records = [_record(name="AB")] + [_record(name=f"AB ({n})") for n in range(2, 10)]
    result = dedupe_fallback_name("AB", records, max_length=6)
    # At suffix " (10)" (5 chars), max(0, 6 - 5) == 1, so the base truncates
    # to "A" rather than going negative and slicing from the wrong end.
    assert result == "A (10)"
    assert len(result) <= 6
    assert find_by_name(records, result) is None
