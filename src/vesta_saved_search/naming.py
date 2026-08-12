"""Name/criteria collision matching and the URL/filters consistency check
(step N5 / VA-401).

Kept separate from `tools.py` because `propose_saved_search` and `save_search`
both need EXACTLY these rules — `save_search` re-runs them against live data
at write time per the plan ("both duplicate checks re-run against live data")
— and a second, slightly different reimplementation at the write site is
exactly the kind of drift that would make one of the two checks decorative.
"""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import parse_qs, urlparse

from vesta_saved_search.fingerprint import criteria_fingerprint
from vesta_saved_search.models import SavedSearchRecord

#: Collapses runs of whitespace to one space before comparison, so
#: `"Del  Mar Homes "` and `"Del Mar Homes"` are recognised as the same name.
_WHITESPACE_RUN = re.compile(r"\s+")

#: Short, common words ignored when checking whether a generated name
#: "mentions" an unsupported filter. Without this, an unsupported filter
#: phrase like "with a pool" would flag almost any name containing "with" —
#: a word that carries no information about what the filter actually was.
_STOPWORDS = frozenset({"a", "an", "and", "for", "in", "of", "on", "the", "with"})

#: A word must be at least this long to count as "mentioning" an unsupported
#: filter. Guards the same false-positive direction as the stopword list: a
#: 2-3 letter overlap ("bed" inside "bedroom") is far more likely to be
#: coincidence than a name actually referencing the unsupported concept.
_MIN_SIGNIFICANT_WORD_LENGTH = 4

_WORD = re.compile(r"[a-z0-9]+")


def normalise_name(name: str) -> str:
    """Trim, casefold, and collapse internal whitespace for name comparison.

    Per the plan: `"Del Mar Homes"`, `"del mar homes"` and `"Del  Mar Homes "`
    must all collide. Comparison only — the ORIGINAL name (not this
    normalised form) is what gets sent upstream and shown to the user.
    """
    return _WHITESPACE_RUN.sub(" ", name.strip()).casefold()


def find_by_name(records: list[SavedSearchRecord], name: str) -> SavedSearchRecord | None:
    """Exact match (after normalisation) over an already fully-paged list.

    🔴 Deliberately never the GuestSite API's own `SearchName` filter —
    verified to be a `contains` match (`SearchName=TestSaveSearch` returned
    four records sharing that prefix), which would fire a false "that name
    is taken" whenever a new name happens to be a substring of an existing
    one. This does the exact comparison client-side instead.
    """
    target = normalise_name(name)
    for record in records:
        if normalise_name(record.name) == target:
            return record
    return None


def find_by_fingerprint(records: list[SavedSearchRecord], fingerprint: str) -> SavedSearchRecord | None:
    """The first existing record whose criteria fingerprint matches.

    Recomputes the fingerprint of each stored record's `(search_filters,
    search_mode)` rather than trusting a value cached anywhere — this is the
    entire mechanism behind requirement 4, so it always derives from the
    freshest read.
    """
    for record in records:
        if criteria_fingerprint(record.search_filters, record.search_mode) == fingerprint:
            return record
    return None


def url_matches_filters(search_url: str, search_filters: dict[str, Any]) -> bool:
    """The create-side `searchUrl` / `searchFilters` consistency check.

    ⚠️ Cannot be strict equality — a genuinely accepted real payload sent
    `homeType: ["0", "1"]` with only `homeType=1` in the URL (see
    `tests/fixtures/guestsite_dev/create_daily_request.json`). So the rule
    is asymmetric by value shape:

    * A **scalar** filter value must match its URL query parameter exactly
      (case-insensitively on both the key and the value).
    * A **list-valued** filter only requires the URL to carry AT LEAST ONE
      of its members — matching the real accepted payload above.

    🔴 A list-valued filter's URL member can itself arrive two ways,
    observed directly running a real end-to-end scenario against the live
    orchestrator: `homeType=1` (one value) in the fixture above, but
    `homeType=0,1,2` (one query PARAM holding a comma-joined value) from
    `build_saved_search_input` for a three-way selection. `parse_qs` treats
    a comma as an ordinary character, so the second shape parses to the
    single string `"0,1,2"`, not three separate values — every URL value is
    therefore also split on `,` before the overlap check, so both shapes of
    a real payload are recognised as consistent.

    The negative case this exists to catch was produced by a human during
    manual testing: `area=11` in the URL with `area: 17` in `searchFilters`.
    That mismatch — a contradictory record the upstream API itself does not
    reject — is exactly what this refuses before ever reaching GuestSite.

    Known limitation, accepted rather than engineered around: every key in
    `search_filters` is required to appear somewhere in the URL. Every
    verified real payload for this API satisfies that, but it is not a
    guarantee this API makes — see the module docstring's callers for how to
    revisit this if a legitimate filter is ever added that the URL omits.
    """
    query = parse_qs(urlparse(search_url).query, keep_blank_values=True)
    query_by_lower_key = {key.lower(): values for key, values in query.items()}

    for key, value in search_filters.items():
        url_values = query_by_lower_key.get(key.lower())
        if url_values is None:
            return False
        if isinstance(value, list):
            list_items = {str(item).strip().lower() for item in value}
            url_items = {
                token.strip().lower() for url_value in url_values for token in url_value.split(",")
            }
            if not (list_items & url_items):
                return False
        else:
            url_value = url_values[0] if url_values else ""
            if str(value).strip().lower() != url_value.strip().lower():
                return False
    return True


def mentions_unsupported_filter(name: str, unsupported_filters: list[str]) -> bool:
    """True if `name` appears to reference something in `unsupported_filters`.

    🔴 The highest-value naming rule in this module: a generated name like
    "Del Mar Theater Homes" asserts a constraint ("home theater") we never
    actually applied, and it outlives the template's "not included" line by
    the length of however long the user keeps the search. Word-overlap, not
    substring containment, because a naive `"home theater" in name.lower()`
    check misses reordered phrasing — the exact test case that matters here
    has "home theater" as the unsupported filter and "Del Mar Theater Homes"
    (word order reversed, and pluralised) as the name.

    Short/common words are excluded (see `_STOPWORDS` and
    `_MIN_SIGNIFICANT_WORD_LENGTH`) so a filter phrase like "with a view"
    does not flag every name containing "with".
    """
    name_words = set(_WORD.findall(name.lower()))
    for phrase in unsupported_filters:
        significant_words = [
            word
            for word in _WORD.findall(phrase.lower())
            if word not in _STOPWORDS and len(word) >= _MIN_SIGNIFICANT_WORD_LENGTH
        ]
        if any(word in name_words for word in significant_words):
            return True
    return False


def fallback_name(criteria_summary: str, *, max_length: int) -> str:
    """The deterministic name used after naming has failed twice.

    Built from `criteriaSummary` — the human-readable summary the model
    supplies, not the wire-level `searchFilters` — so the result reads as
    "Del Mar Search", never as raw filter codes like `area` or `11`. Plain
    and deterministic on purpose: this path exists specifically so a test
    can assert its output exactly, and so a name generation loop the model
    keeps getting wrong has a guaranteed exit that a user can still read.
    """
    base = criteria_summary.strip()
    candidate = f"{base} Search" if base else "Saved Search"
    return candidate[:max_length]


__all__ = [
    "fallback_name",
    "find_by_fingerprint",
    "find_by_name",
    "mentions_unsupported_filter",
    "normalise_name",
    "url_matches_filters",
]
