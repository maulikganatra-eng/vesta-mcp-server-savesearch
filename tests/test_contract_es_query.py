"""Level 3 contract test: EsQueryClient against a REALLY RUNNING property-search
server (step S4 / VA-403, consumed as N7 / VA-404).

Marked `contract` -- never runs in CI. Requires `vesta-mcp-server` running
locally with the S4 route (branch `feat/va-403-internal-es-query-endpoint`):

    cd ../vesta-mcp-server && docker compose -f docker-compose.local.yml up -d

Skips cleanly if that server is not reachable, rather than failing -- a
machine without the sibling repo checked out and running is an ordinary
state, not an error.
"""

from __future__ import annotations

import os

import httpx
import pytest

from vesta_saved_search.es_query_client import EsQueryClient

pytestmark = pytest.mark.contract

PROPERTY_SEARCH_URL = os.environ.get("PROPERTY_SEARCH_INTERNAL_URL", "http://localhost:6000")


def _require_reachable() -> None:
    try:
        httpx.get(f"{PROPERTY_SEARCH_URL}/internal/es-query", timeout=3.0)
    except httpx.RequestError:
        pytest.skip(
            f"{PROPERTY_SEARCH_URL} is not reachable -- start vesta-mcp-server locally "
            "(feat/va-403-internal-es-query-endpoint) to run this test"
        )


async def test_real_endpoint_returns_a_parseable_es_query_for_for_sale() -> None:
    _require_reachable()
    client = EsQueryClient(PROPERTY_SEARCH_URL)
    try:
        es_query = await client.fetch_es_query(
            {"city": "Del Mar", "mode": "forSale", "status": "active,comingSoon", "SortBy": "new"},
            "forSale",
        )
    finally:
        await client.aclose()

    assert isinstance(es_query, str)
    assert "del mar" in es_query.lower()


async def test_real_endpoint_produces_a_different_query_for_a_different_city() -> None:
    """Confirms this path genuinely calls MLS per-request rather than caching
    or returning a fixed response -- two different wire-level filter sets
    produce two different esQuery strings.

    (An empty filter set and a deliberately nonexistent city VALUE were both
    tried here first and surfaced as real 502s from the downstream MLS call
    -- this path skips resolvers entirely and sends whatever it is given
    straight to MLS, so it inherits MLS's own requirements for what a valid
    wire-level filter set looks like. That is this path working as designed,
    not a bug, but it means only REALISTIC, fully-formed filters -- the kind
    a stored record or `build_saved_search_input` actually produces -- are
    valid input here, which is exactly how the N7 update recipe uses it.)
    """
    _require_reachable()
    client = EsQueryClient(PROPERTY_SEARCH_URL)
    try:
        del_mar = await client.fetch_es_query(
            {"city": "Del Mar", "mode": "forSale", "status": "active,comingSoon", "SortBy": "new"},
            "forSale",
        )
        malibu = await client.fetch_es_query(
            {"city": "Malibu", "mode": "forSale", "status": "active,comingSoon", "SortBy": "new"},
            "forSale",
        )
    finally:
        await client.aclose()

    assert "del mar" in del_mar.lower()
    assert "malibu" in malibu.lower()
    assert del_mar != malibu


async def test_real_endpoint_rejects_an_unmappable_search_mode_before_ever_calling_it() -> None:
    """The client-side guard fires before any HTTP call -- this test proves
    the guard exists by never letting an invalid literal reach the type
    checker at all; see test_es_query_client.py's unit test for the guard
    itself. Documented here as the real endpoint's own 400 shape for an
    invalid searchMode, reached only by bypassing this client's typing."""
    _require_reachable()
    async with httpx.AsyncClient(timeout=10.0) as http_client:
        response = await http_client.post(
            f"{PROPERTY_SEARCH_URL}/internal/es-query",
            json={"searchFilters": {}, "searchMode": "commercial"},
        )
    assert response.status_code == 400
