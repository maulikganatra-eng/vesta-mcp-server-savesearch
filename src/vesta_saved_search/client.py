"""The one module that owns all HTTP contact with the GuestSite SavedSearches API.

No tool calls the API directly; every one goes through :class:`SavedSearchClient`.

This API has five separate traps, verified directly against dev (see
``scratch/adhoc/record_n2_dev_fixtures.py`` in the orchestrator repo and
``tests/fixtures/guestsite_dev/`` in this one), and **every one of them fails
silently**:

1. **Double-decode.** The create/update response's `savedSearch` field is a JSON
   *string* containing a JSON *array*: `"savedSearch": "[{...}]"`.
2. **`searchFilters` is a string on read**, even though it is sent as an object:
   `"{\\"area\\":\\"11\\",...}"`.
3. **`consumerId` is injected inside stored `searchFilters`.** We never send it;
   the service adds it. Left in place on read — see :class:`SavedSearchRecord`.
4. **Key casing changes.** Sent `"SortBy"`, comes back `"sortBy"` — reproduced in
   `tests/fixtures/guestsite_dev/create_daily.json`.
5. **`PageSize` defaults to 15** and silently truncates. `list()` requests a large
   explicit page size and walks pages until one comes back short, so this is never
   the caller's problem.

Handling all five in one place means a call site cannot forget one of them —
five call sites each remembering five traps is twenty-five chances to be wrong.

✅ **The page-walking parameter name is now verified against a real paginated
account.** `tests/test_contract_saved_search.py` (level 3, run against real dev)
forces `page_size=5`, creates enough throwaway records to cross 15 total, and
confirms the walk returns every record with no duplicates and no hang — see
that test for the mechanics, including why it wraps the call in a timeout
rather than trusting a wrong guess to fail fast on its own. `_PAGE_NUMBER_PARAM`
below records what was confirmed and how.
"""

from __future__ import annotations

import json
from typing import Any, Final

import httpx

from vesta_saved_search.errors import (
    SavedSearchInvalidRequestError,
    SavedSearchNameExistsError,
    SavedSearchUnexpectedResponseError,
    SavedSearchUpstreamError,
)
from vesta_saved_search.frequency import frequency_to_schedule_interval
from vesta_saved_search.http_support import request_or_upstream_error
from vesta_saved_search.models import SavedSearchRecord

#: The endpoint both creates and updates a record. Presence of `savedSearchId` in
#: the outgoing payload means update; its absence means create. Verified: this is
#: one shared endpoint, not two, and both directions return the same envelope
#: shape (confirmed against a real rename in `update_rename_omit_schedule.json`).
_SAVE_ENDPOINT: Final[str] = "SavedSearches/saveAI"

_LIST_ENDPOINT: Final[str] = "SavedSearches"

#: Default page size, requested explicitly on every list call because the
#: service's own default is 15 and silently truncates. Large enough that a real
#: guest account should never need a second page — but `list()` still walks pages
#: if one ever comes back full, rather than trusting that this number is always
#: enough. Overridable per client via the constructor so tests can force
#: multi-page walking without touching this module-level default.
_DEFAULT_PAGE_SIZE: Final[int] = 200

#: ✅ VERIFIED against a real multi-page response by the level-3 paging test in
#: `tests/test_contract_saved_search.py` (real dev, `page_size=5`, >15 total
#: records, no duplicate ids, every created record present in the fully-walked
#: result). Kept as a single named constant regardless — this is exactly the
#: kind of value that is cheap to get wrong and expensive to debug if the
#: API's naming ever changes.
_PAGE_NUMBER_PARAM: Final[str] = "PageNumber"


class SavedSearchClient:
    """Thin async wrapper around the GuestSite SavedSearches HTTP API.

    One instance per base URL. Not per-request, not per-user — the token is a
    parameter to every call, never stored on the instance, because storing
    anything user-specific on an object this server might reuse across requests
    is exactly the cross-tenant leak documented in
    :func:`vesta_saved_search.identity.bearer_token_from_meta`.
    """

    def __init__(
        self,
        base_url: str,
        *,
        http_client: httpx.AsyncClient | None = None,
        page_size: int = _DEFAULT_PAGE_SIZE,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        # Accepts an injected client so tests can pass one built on
        # `httpx.MockTransport` without this class knowing tests exist.
        self._http = http_client or httpx.AsyncClient(timeout=30.0)
        # A constructor parameter rather than a module constant a test would have
        # to reach in and mutate: `_DEFAULT_PAGE_SIZE` is `Final`, so overriding it
        # from outside this module for one test is exactly the kind of workaround
        # that gets flagged by mypy the moment someone else tries to copy it.
        self._page_size = page_size

    async def aclose(self) -> None:
        await self._http.aclose()

    def _headers(self, token: str) -> dict[str, str]:
        return {"Authorization": f"Bearer {token}"}

    async def list_saved_searches(self, token: str) -> list[SavedSearchRecord]:
        """Return every saved search for the caller, fully paged.

        Paging is internal and total: this method never returns a partial list,
        and no caller of this client ever sees a page size or a page number.
        Walks pages until one comes back with fewer records than this client's
        page size, which is the exhaustion signal — there is no total-count field
        in the response to rely on instead.
        """
        records: list[SavedSearchRecord] = []
        page = 1
        while True:
            params: dict[str, Any] = {"PageSize": self._page_size}
            if page > 1:
                params[_PAGE_NUMBER_PARAM] = page
            response = await self._request("GET", _LIST_ENDPOINT, params=params, token=token)
            batch = self._parse_list_body(response)
            records.extend(SavedSearchRecord.from_api(raw) for raw in batch)
            if len(batch) < self._page_size:
                break
            page += 1
        return records

    async def create(
        self,
        token: str,
        *,
        name: str,
        search_filters: dict[str, Any],
        search_url: str,
        es_query: str,
        notification_frequency: str,
    ) -> SavedSearchRecord:
        """Create a new saved search. Raises :class:`SavedSearchNameExistsError` on collision.

        `es_query` must already be the JSON *string* the API expects (see
        `summariser.py` in the property-search repo, which is what produces it) —
        this client does not serialise it, so a caller passing a dict here would
        send the wrong wire shape with no error until the write is rejected far
        downstream, or silently accepted and unreadable later.
        """
        body = {
            "searchName": name,
            "searchFilters": search_filters,
            "searchUrl": search_url,
            "esQuery": es_query,
            "scheduleInterval": frequency_to_schedule_interval(notification_frequency),
        }
        response = await self._request("POST", _SAVE_ENDPOINT, json_body=body, token=token)
        return self._parse_save_body(response)

    async def update(
        self,
        token: str,
        *,
        saved_search_id: int,
        name: str,
        search_filters: dict[str, Any],
        search_url: str,
        es_query: str,
        notification_frequency: str,
    ) -> SavedSearchRecord:
        """Update an existing saved search. Full-replace: every field is required.

        🔴 Verified directly against dev: a record set to Daily (`scheduleId: 3`),
        updated once with `scheduleInterval` omitted, came back `scheduleId: null`
        — HTTP 200, no error, no warning. This method's signature has no optional
        fields for exactly that reason: there is no way to call it with "just the
        name" and accidentally omit the frequency. The full six-step recipe that
        reads the current record and refreshes `esQuery` before calling this
        belongs to step N7, one level up from this client.
        """
        body = {
            "savedSearchId": saved_search_id,
            "searchName": name,
            "searchFilters": search_filters,
            "searchUrl": search_url,
            "esQuery": es_query,
            "scheduleInterval": frequency_to_schedule_interval(notification_frequency),
        }
        response = await self._request("POST", _SAVE_ENDPOINT, json_body=body, token=token)
        return self._parse_save_body(response)

    async def delete(self, token: str, saved_search_id: int) -> None:
        """Delete a saved search by id.

        The id MUST be a path segment: `DELETE /SavedSearches/{id}`. The
        querystring form (`?savedSearchId=`) returns 405 — not documented by this
        API, discovered by trying it. `httpx` builds the URL from the f-string
        below, so there is no query-parameter code path here to accidentally use.
        """
        await self._request("DELETE", f"SavedSearches/{saved_search_id}", token=token)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        token: str,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
    ) -> httpx.Response:
        url = f"{self._base_url}/{path}"
        response = await request_or_upstream_error(
            self._http.request(method, url, params=params, json=json_body, headers=self._headers(token)),
            description=f"{method} {path}",
        )

        if response.status_code == 400:
            self._raise_for_400(response)
        elif response.status_code >= 500:
            raise SavedSearchUpstreamError(
                f"{method} {path} returned {response.status_code}: {response.text[:200]}"
            )
        elif response.status_code >= 400:
            raise SavedSearchUpstreamError(
                f"{method} {path} returned an unexpected {response.status_code}: {response.text[:200]}"
            )
        return response

    def _raise_for_400(self, response: httpx.Response) -> None:
        """Split the two documented shapes of a 400 body.

        Verified against dev, both shapes:

        * `{"status": "exists", "savedSearch": null}` — a name collision.
        * A plain JSON string, e.g. `"EsQuery cannot be null"` — every other
          validation failure. This is NOT the same shape as the exists body, and
          treating it as one (e.g. checking for a `"status"` key that happens to be
          absent) would silently misreport a validation error as something else.
        """
        try:
            body = response.json()
        except ValueError:
            raise SavedSearchInvalidRequestError(
                f"400 with a non-JSON body: {response.text[:200]}"
            ) from None

        if isinstance(body, dict) and body.get("status") == "exists":
            raise SavedSearchNameExistsError("a saved search with this name already exists")

        if isinstance(body, str):
            raise SavedSearchInvalidRequestError(body)

        raise SavedSearchInvalidRequestError(f"400 with an unrecognised body shape: {body!r}")

    def _parse_list_body(self, response: httpx.Response) -> list[dict[str, Any]]:
        try:
            body = response.json()
        except ValueError as exc:
            raise SavedSearchUnexpectedResponseError(
                f"list response was not JSON: {response.text[:200]}"
            ) from exc
        if not isinstance(body, list):
            raise SavedSearchUnexpectedResponseError(
                f"list response was a {type(body).__name__}, expected a JSON array "
                f"(verified shape: a bare top-level list, not {{'savedSearches': [...]}})"
            )
        return body

    def _parse_save_body(self, response: httpx.Response) -> SavedSearchRecord:
        """Undo the double-decode trap: `savedSearch` is a JSON string of a JSON array."""
        try:
            body = response.json()
        except ValueError as exc:
            raise SavedSearchUnexpectedResponseError(
                f"save response was not JSON: {response.text[:200]}"
            ) from exc

        if not isinstance(body, dict) or "savedSearch" not in body:
            raise SavedSearchUnexpectedResponseError(f"save response missing 'savedSearch': {body!r}")

        raw_saved_search = body["savedSearch"]
        if not isinstance(raw_saved_search, str):
            raise SavedSearchUnexpectedResponseError(
                "expected 'savedSearch' to be a JSON string (the documented "
                f"double-decode shape), got a {type(raw_saved_search).__name__}"
            )

        try:
            decoded = json.loads(raw_saved_search)
        except json.JSONDecodeError as exc:
            raise SavedSearchUnexpectedResponseError(
                f"'savedSearch' was not valid JSON: {raw_saved_search!r}"
            ) from exc

        if not isinstance(decoded, list) or not decoded:
            raise SavedSearchUnexpectedResponseError(
                f"'savedSearch' decoded to {decoded!r}, expected a non-empty array"
            )

        return SavedSearchRecord.from_api(decoded[0])
