"""Criteria fingerprinting (step N4 / VA-400).

A pure function, no I/O: wire-level `searchFilters` + `searchMode` -> a SHA-256
hex string. This is the entire mechanism behind requirement 4 ("identical
criteria under a different name reports the existing search"), and it is a
pure function on purpose -- getting it right in isolation, before it is buried
inside `propose_saved_search`'s seven status branches, is much cheaper than
debugging it there.

🔴 Two things break a naive implementation, and both fail *open* -- the save
proceeds, no error, and requirement 4 quietly does nothing while looking
implemented:

1. **The drop-list must include `consumerId`, `sortBy` and `mode`.** The
   service injects `consumerId` *inside* stored `searchFilters`, and we never
   send it -- so the object we are about to save and the object we read back
   always differ by at least that key. Fingerprint naively and duplicate
   detection never fires, ever.
2. ⚠️ **Match drop-list keys case-insensitively.** Sent `"SortBy"`, stored
   `"sortBy"` -- observed directly in `tests/fixtures/guestsite_dev/
   create_daily.json` vs `create_daily_request.json`. A case-sensitive
   drop-list misses the sent-side key and produces the same silent mismatch.

**Explicit non-goal: exact match only.** `priceMax: 2000000` and `2050000`
are different searches and will not be flagged as duplicates. Do NOT add
fuzzy equality here -- a false "you already saved this" blocks a legitimate
save, which is far worse than a near-duplicate slipping through.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Final

#: Keys dropped from the fingerprint regardless of casing. Matched against
#: `key.casefold()`, so `"SortBy"`, `"sortby"` and `"SORTBY"` all match the
#: same entry here.
#:
#: * `consumerid` -- injected by the service inside stored `searchFilters`;
#:   never sent by us. Always present on read, never on the freshly-built
#:   payload, so leaving it in would make every real-vs-fresh comparison
#:   fail forever.
#: * `sortby` -- not part of the search criteria; a display/ordering
#:   preference that does not change which listings match.
#: * `mode` -- redundant with `search_mode`, which is passed as its own
#:   parameter and included in the canonical form separately (see
#:   `criteria_fingerprint`). Some payloads carry it inside `searchFilters`
#:   too (`{"mode": "forSale", ...}`); dropping it here avoids double-counting.
_DROP_KEYS_CASEFOLDED: Final[frozenset[str]] = frozenset({"consumerid", "sortby", "mode"})


def _is_empty_ish(value: Any) -> bool:
    """True for values this fingerprint treats as "not actually set".

    `None`, `""`, `[]` and `{}` are unambiguous. `False` is included too --
    an unchecked checkbox-style filter carries no more information than its
    absence. Uses `value is False` rather than `value == False`: `bool` is a
    subclass of `int` in Python, so `0 == False` is `True`, and an `==`
    comparison here would silently also drop a legitimate `0` (e.g.
    `bedMin: 0`), which is a real, meaningful filter value.
    """
    if value is False:
        return True
    if value is None:
        return True
    return isinstance(value, (str, list, dict)) and len(value) == 0


def _normalise(value: Any) -> Any:
    """Recursively normalise one value for the canonical form.

    Strings are trimmed and casefolded so that differing whitespace or casing
    -- neither of which changes what the search actually matches -- does not
    produce a different fingerprint. Lists are normalised element-wise and
    then sorted, so `["1", "0"]` and `["0", "1"]` fingerprint identically.
    Dicts are recursed into and have their own empty-ish/drop-list rules
    re-applied, in case a nested object ever carries one of the dropped keys.
    """
    if isinstance(value, str):
        return value.strip().casefold()
    if isinstance(value, list):
        normalised_items = [_normalise(item) for item in value]
        # Sort by the JSON representation so heterogeneous element types
        # (unlikely in practice, but not ruled out by the input's typing)
        # never raise a "'<' not supported" comparison error.
        return sorted(normalised_items, key=lambda item: json.dumps(item, sort_keys=True))
    if isinstance(value, dict):
        return _canonicalise(value)
    return value


def _canonicalise(search_filters: dict[str, Any]) -> dict[str, Any]:
    canonical: dict[str, Any] = {}
    for key, value in search_filters.items():
        if key.casefold() in _DROP_KEYS_CASEFOLDED:
            continue
        if _is_empty_ish(value):
            continue
        canonical[key.casefold()] = _normalise(value)
    return canonical


def criteria_fingerprint(search_filters: dict[str, Any], search_mode: str) -> str:
    """Fingerprint one saved search's criteria as a SHA-256 hex string.

    `search_mode` is included explicitly, not read out of `search_filters`
    (where it is dropped by the drop-list above): a for-sale and a for-rent
    search with otherwise identical filters are not the same saved search,
    and would collide here if mode were omitted.

    `unsupportedFilters` is not a parameter to this function at all, so it is
    excluded from the fingerprint by construction. That is a deliberate
    consequence, not an oversight: "Del Mar with a home theater" and "Del Mar
    with solar panels" produce the SAME fingerprint here, because as saved
    searches (which notify on matching listings) they are genuinely
    identical -- neither `homeType` nor `solar` is something this MLS can
    filter on. `propose_saved_search` is responsible for wording the
    resulting duplicate message so this does not read as a bug to the user.
    """
    canonical = _canonicalise(search_filters)
    # 🔴 `_canonicalise` already casefolds every key from `search_filters`, so
    # a literal `searchMode`/`SearchMode` key in the input (distinct from
    # `mode`, which real payloads use and which IS dropped) would already be
    # sitting in `canonical` under the casefolded key `"searchmode"` -- a
    # DIFFERENT dict key from the mixed-case `"searchMode"` set below. Popping
    # it first guarantees exactly one entry ever represents the search mode,
    # so a round-tripped record that happened to carry that field can never
    # fingerprint differently from a freshly built payload that omits it.
    canonical.pop("searchmode", None)
    canonical["searchMode"] = search_mode.strip().casefold()
    encoded = json.dumps(canonical, sort_keys=True)
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = ["criteria_fingerprint"]
