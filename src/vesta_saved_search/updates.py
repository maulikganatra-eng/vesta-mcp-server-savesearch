"""The full-replace update recipe every write to an existing saved search
goes through (step N7 / VA-404).

🔴 **Updates are full-replace, not merge.** Verified directly against dev: a
record set to Daily (`scheduleId: 3`), updated once with `scheduleInterval`
omitted, came back `scheduleId: null` — HTTP 200, no error, no warning. A
rename that forgets to resend the frequency silently switches off a user's
daily notifications, and they find out by not receiving something, which is
the hardest kind of bug to report.

One function, six steps, in order, so omission is impossible **by
construction** rather than by everyone remembering:

1. **GET the full record**, immediately before writing — last-write-wins
   (there is no version or etag), so a record fetched earlier in the turn
   must never be reused here.
2. `searchFilters` arrives already parsed (a dict, `consumerId` intact) via
   :class:`~vesta_saved_search.models.SavedSearchRecord`.
3. Apply the caller's change — name, frequency, or criteria.
4. 🔴 **Obtain a fresh `esQuery`.** The one step with no shortcut: required on
   every write, never returned by any read, so it can never be carried over
   from step 1's GET. Criteria unchanged (rename, frequency) → the wire-level
   S4 endpoint via :class:`~vesta_saved_search.es_query_client.EsQueryClient`.
   Criteria changed → supplied by the caller (from `build_saved_search_input`,
   already run by the co-selected property-search capability) — this
   function never asks twice.
5. **Keep `searchUrl` in its resolved form.** If criteria changed, only the
   query string is replaced; the path (which carries the real id) is kept.
6. **Send the complete payload**, including `savedSearchId`, via
   :meth:`~vesta_saved_search.client.SavedSearchClient.update`.

🔴 **The `searchUrl` format precondition.** Verified: the stored resolved URL
(`.../saved-search/22/...`) is stored byte-identical on update; the plain
create-shaped URL is stored with a **literal `{SavedSearchId}` placeholder,
id never substituted** — and both return HTTP 200. Create substitutes the
id; update stores what it is given. So this module asserts the outgoing URL
contains `saved-search/<the real id>/` before ever sending it, and refuses
otherwise — the wrong one looks identical to a success.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.errors import SavedSearchUnexpectedResponseError
from vesta_saved_search.es_query_client import EsQueryClient, search_mode_for_es_query
from vesta_saved_search.models import SavedSearchRecord


@dataclass(frozen=True, slots=True)
class UpdateChange:
    """What the caller wants to change about one saved search.

    Exactly one concern is expected to be set per call — a rename (`name`),
    a frequency change (`notification_frequency`), or a criteria change
    (`search_filters` + `fresh_es_query` + `new_search_url_query` together).
    Nothing here enforces that exclusivity; it is a convention every current
    caller (`save_search`'s update branch, `update_saved_search_notifications`)
    follows because the tools built on top of this module each expose only
    one kind of change to the user in a single confirmation.
    """

    name: str | None = None
    notification_frequency: str | None = None
    search_filters: dict[str, Any] | None = None
    #: Required if `search_filters` is set. Must already be the resolved
    #: `esQuery` JSON string from `build_saved_search_input` — this recipe
    #: never derives one itself when criteria changed, per step 4 above.
    fresh_es_query: str | None = None
    #: Required if `search_filters` is set. The REPLACEMENT query string only
    #: (no leading `?`) — the stored URL's path is always kept, per step 5.
    new_search_url_query: str | None = None


def _replace_query_string(stored_url: str, new_query: str) -> str:
    """Keep `stored_url`'s scheme/host/PATH (the part carrying the real id)
    and swap only its query string."""
    parts = urlsplit(stored_url)
    return urlunsplit((parts.scheme, parts.netloc, parts.path, new_query.lstrip("?"), parts.fragment))


def _assert_resolved_update_url(url: str, saved_search_id: int) -> None:
    """🔴 The URL-format precondition documented on the module. A create-shaped
    URL is accepted with HTTP 200 and silently stored with a literal
    `{SavedSearchId}` placeholder — this is the one thing that precondition
    exists to make impossible to send in the first place.
    """
    expected_fragment = f"saved-search/{saved_search_id}/"
    if expected_fragment not in url:
        raise SavedSearchUnexpectedResponseError(
            f"outgoing update URL does not contain {expected_fragment!r} -- refusing to send "
            "a create-shaped URL on an update, which GuestSite would silently store with a "
            f"literal '{{SavedSearchId}}' placeholder rather than reject: {url!r}"
        )


async def apply_update(
    client: SavedSearchClient,
    es_query_client: EsQueryClient,
    token: str,
    *,
    saved_search_id: int,
    change: UpdateChange,
    records: list[SavedSearchRecord] | None = None,
) -> SavedSearchRecord:
    """Run the six-step update recipe and return the record GuestSite stored.

    `records`, when given, is used instead of this function re-fetching the
    caller's full list itself — for a caller (`save_search`'s update branch)
    that already fetched the same list microseconds earlier for its own
    live duplicate re-checks, with nothing async in between that could have
    gone stale meaningfully. Left `None` in every other case (including
    every test in this module), which re-fetches internally so step 1's
    "immediately before writing" guarantee still holds for a caller that has
    no reason to have a list already in hand.

    Raises :class:`~vesta_saved_search.errors.SavedSearchUnexpectedResponseError`
    if `saved_search_id` is not found in the caller's own list, if a criteria
    change is missing its required `fresh_es_query` / `new_search_url_query`,
    or if the URL precondition fails. Raises whatever
    :class:`~vesta_saved_search.errors.SavedSearchApiError` subclass the
    underlying `SavedSearchClient` / `EsQueryClient` calls raise otherwise —
    this function adds no error handling of its own beyond the checks above,
    so a tool built on it can catch one base class for everything.
    """
    # Step 1: GET the full record, immediately before writing.
    if records is None:
        records = await client.list_saved_searches(token)
    current = next((r for r in records if r.saved_search_id == saved_search_id), None)
    if current is None:
        raise SavedSearchUnexpectedResponseError(
            f"savedSearchId={saved_search_id} was not found in the caller's own saved searches"
        )

    # Step 3: apply the caller's change. Anything not explicitly changed is
    # carried over UNCHANGED from the fresh record above -- never from any
    # value the caller might have cached earlier in the turn.
    new_name = current.name if change.name is None else change.name
    new_frequency = (
        current.notification_frequency
        if change.notification_frequency is None
        else change.notification_frequency
    )
    criteria_changed = change.search_filters is not None
    new_filters = change.search_filters if criteria_changed else current.search_filters

    # Step 4: a fresh esQuery, always -- never carried over from step 1's GET.
    if criteria_changed:
        if change.fresh_es_query is None:
            raise SavedSearchUnexpectedResponseError(
                "search_filters changed but no fresh_es_query was supplied -- it must come "
                "from build_saved_search_input, never be derived inside this recipe"
            )
        es_query = change.fresh_es_query
    else:
        search_mode = search_mode_for_es_query(current.search_mode)
        es_query = await es_query_client.fetch_es_query(current.search_filters, search_mode)

    # Step 5: searchUrl stays resolved; only its query string moves if
    # criteria changed.
    if criteria_changed:
        if change.new_search_url_query is None:
            raise SavedSearchUnexpectedResponseError(
                "search_filters changed but no new_search_url_query was supplied"
            )
        new_url = _replace_query_string(current.search_url, change.new_search_url_query)
    else:
        new_url = current.search_url
    _assert_resolved_update_url(new_url, saved_search_id)

    # Step 6: the complete payload, every field, every time.
    return await client.update(
        token,
        saved_search_id=saved_search_id,
        name=new_name,
        search_filters=new_filters if new_filters is not None else current.search_filters,
        search_url=new_url,
        es_query=es_query,
        notification_frequency=new_frequency,
    )


__all__ = ["UpdateChange", "apply_update"]
