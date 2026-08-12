"""HTTP client for property-search's wire-level esQuery endpoint (step S4 /
VA-403, consumed here as step N7 / VA-404).

Every update needs a fresh `esQuery` — including a plain rename — because the
field is required on every write to GuestSite and is never returned by any
read. Without this path, this repo would need its own MLS client, its own
`MLS_API_URL`, and a copy of resolver behaviour; with it, this repo contains
zero MLS logic, matching the two-server split the whole architecture is
built on.

**Server-to-server only.** This client calls
`POST {PROPERTY_SEARCH_INTERNAL_URL}/internal/es-query` as a plain HTTP
client — never through MCP, never through an LLM. On the property-search
side that endpoint is registered via `@mcp.custom_route`, the same mechanism
as its own `/health`, alongside `@mcp.tool()`-registered tools rather than as
one: no tool list, no co-selection, no DAG, no LLM decision. The
`saved_search` capability cannot see property-search's tools at all — that
is the entire premise of the two-server split, and this client is the one
place that boundary is deliberately crossed, over plain HTTP.

Contract, read directly from the real implementation
(`vesta-mcp-server`'s `tools.py`, `EsQueryRequest` / `es_query`):

    POST /internal/es-query
    {"searchFilters": {...wire-level codes...}, "searchMode": "forSale"|"forRent"|"Sold"}
      200 -> {"esQuery": "<json string>"}
      400 -> malformed request body (never expected from THIS client, since
             it only ever sends a shape it built itself)
      502 -> the MLS call failed, or MLS returned no Query to extract
      -- never a 500, by that endpoint's own documented contract.
"""

from __future__ import annotations

from typing import Any, Final, Literal

import httpx

from vesta_saved_search.errors import (
    SavedSearchUnexpectedResponseError,
    SavedSearchUpstreamError,
)
from vesta_saved_search.http_support import request_or_upstream_error

_ES_QUERY_PATH: Final[str] = "internal/es-query"

#: The exact enum property-search's S4 endpoint accepts. ⚠️ Note the casing:
#: "Sold" is capitalised; "forSale" and "forRent" are not. This is NOT
#: guaranteed to match a stored record's `searchType` byte-for-byte — see
#: :func:`search_mode_for_es_query`, which exists specifically because that
#: assumption is a real trap, not a hypothetical one.
EsQuerySearchMode = Literal["forSale", "forRent", "Sold"]

#: Case-insensitive map from a stored `searchType` to S4's exact enum.
_SEARCH_MODE_BY_LOWER: Final[dict[str, EsQuerySearchMode]] = {
    "forsale": "forSale",
    "forrent": "forRent",
    "sold": "Sold",
}


def search_mode_for_es_query(search_type: str) -> EsQuerySearchMode:
    """Map a stored record's `searchType` to the literal S4 requires.

    Raises :class:`SavedSearchUnexpectedResponseError` for anything
    unmappable — deliberately, and BEFORE any HTTP call. `searchType`
    round-trips through GuestSite, and this server has already observed one
    real casing trap on this exact API (`SortBy` -> `sortBy`); there is no
    reason to assume `searchType` is exempt. Refusing here, before ever
    reaching S4, is cheaper than debugging a 400 from a different server.
    """
    normalised = search_type.strip().casefold()
    try:
        return _SEARCH_MODE_BY_LOWER[normalised]
    except KeyError:
        raise SavedSearchUnexpectedResponseError(
            f"stored searchType {search_type!r} does not map to a known S4 searchMode "
            f"(one of {sorted(_SEARCH_MODE_BY_LOWER.values())!r})"
        ) from None


class EsQueryClient:
    """Thin async wrapper around property-search's `/internal/es-query` route.

    One instance per base URL, same pattern as :class:`~vesta_saved_search.client.SavedSearchClient`
    — the token parameter there has no equivalent here since this is a
    server-to-server call carrying no end-user identity; property-search's
    MLS access is unauthenticated on this path, matching S4's own contract.
    """

    def __init__(self, base_url: str, *, http_client: httpx.AsyncClient | None = None) -> None:
        self._base_url = base_url.rstrip("/")
        # Accepts an injected client so tests can pass one built on
        # `httpx.MockTransport`, matching `SavedSearchClient`'s own pattern.
        self._http = http_client or httpx.AsyncClient(timeout=30.0)

    async def aclose(self) -> None:
        await self._http.aclose()

    async def fetch_es_query(
        self, search_filters: dict[str, Any], search_mode: EsQuerySearchMode
    ) -> str:
        """Wire-level `searchFilters` + `searchMode` -> a fresh `esQuery` JSON string.

        Required on every write and never returned by any read — see the
        update recipe in :mod:`vesta_saved_search.updates` for why this can
        never be carried over from a GET.
        """
        url = f"{self._base_url}/{_ES_QUERY_PATH}"
        body = {"searchFilters": search_filters, "searchMode": search_mode}
        response = await request_or_upstream_error(
            self._http.post(url, json=body), description="es-query request"
        )

        if response.status_code == 400:
            # Only reachable if this client ever sends a malformed body,
            # which would be a bug in THIS module, not the caller's fault —
            # still surfaced as an upstream error rather than crashing the
            # tool call, since the correct user-facing behaviour ("couldn't
            # do that right now") is identical either way.
            raise SavedSearchUpstreamError(
                f"es-query endpoint rejected the request as malformed: {response.text[:200]}"
            )
        if response.status_code == 502:
            raise SavedSearchUpstreamError(
                f"es-query endpoint's MLS dependency failed: {response.text[:200]}"
            )
        if response.status_code >= 400:
            raise SavedSearchUpstreamError(
                f"es-query endpoint returned an unexpected {response.status_code}: {response.text[:200]}"
            )

        try:
            data = response.json()
        except ValueError as exc:
            raise SavedSearchUnexpectedResponseError(
                f"es-query response was not JSON: {response.text[:200]}"
            ) from exc

        if not isinstance(data, dict) or "esQuery" not in data:
            raise SavedSearchUnexpectedResponseError(f"es-query response missing 'esQuery': {data!r}")

        es_query = data["esQuery"]
        if not isinstance(es_query, str):
            raise SavedSearchUnexpectedResponseError(
                f"expected 'esQuery' to be a string, got {type(es_query).__name__}"
            )
        return es_query


__all__ = ["EsQueryClient", "EsQuerySearchMode", "search_mode_for_es_query"]
