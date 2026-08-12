"""Unit tests for EsQueryClient, against `httpx.MockTransport` (step N7 / VA-404).

Level 1. The real-endpoint proof -- that this client's request/response
shapes actually match property-search's S4 implementation -- lives in
tests/test_contract_es_query.py, run against a locally running
`vesta-mcp-server`.
"""

from __future__ import annotations

from collections.abc import Callable

import httpx
import pytest

from vesta_saved_search.errors import (
    SavedSearchApiError,
    SavedSearchUnexpectedResponseError,
    SavedSearchUpstreamError,
)
from vesta_saved_search.es_query_client import EsQueryClient, search_mode_for_es_query

pytestmark = pytest.mark.unit

BASE_URL = "http://localhost:6000"


def _client_with(handler: Callable[[httpx.Request], httpx.Response]) -> EsQueryClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url=BASE_URL)
    return EsQueryClient(BASE_URL, http_client=http_client)


@pytest.mark.parametrize(
    "stored,expected",
    [("forSale", "forSale"), ("FORSALE", "forSale"), ("forRent", "forRent"), ("sold", "Sold")],
)
def test_search_mode_for_es_query_normalises_casing(stored: str, expected: str) -> None:
    assert search_mode_for_es_query(stored) == expected


def test_search_mode_for_es_query_rejects_unmappable_values() -> None:
    with pytest.raises(SavedSearchUnexpectedResponseError):
        search_mode_for_es_query("commercial")


async def test_fetch_es_query_sends_filters_and_mode_and_returns_the_string() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        import json

        seen["body"] = json.loads(request.content)
        return httpx.Response(200, json={"esQuery": '{"bool": {}}'})

    client = _client_with(handler)
    result = await client.fetch_es_query({"city": "Del Mar"}, "forSale")

    assert result == '{"bool": {}}'
    assert seen["body"] == {"searchFilters": {"city": "Del Mar"}, "searchMode": "forSale"}


async def test_400_becomes_upstream_error() -> None:
    client = _client_with(lambda r: httpx.Response(400, json={"error": "bad request"}))
    with pytest.raises(SavedSearchUpstreamError):
        await client.fetch_es_query({}, "forSale")


async def test_502_becomes_upstream_error() -> None:
    """The MLS-dependency-failed bucket -- distinct status, same error type."""
    client = _client_with(lambda r: httpx.Response(502, json={"error": "MLS request failed"}))
    with pytest.raises(SavedSearchUpstreamError):
        await client.fetch_es_query({}, "forSale")


async def test_other_4xx_becomes_upstream_error() -> None:
    client = _client_with(lambda r: httpx.Response(404))
    with pytest.raises(SavedSearchUpstreamError):
        await client.fetch_es_query({}, "forSale")


async def test_timeout_becomes_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    client = _client_with(handler)
    with pytest.raises(SavedSearchUpstreamError):
        await client.fetch_es_query({}, "forSale")


async def test_connection_error_becomes_upstream_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client_with(handler)
    with pytest.raises(SavedSearchUpstreamError):
        await client.fetch_es_query({}, "forSale")


async def test_non_json_response_raises() -> None:
    client = _client_with(lambda r: httpx.Response(200, content=b"not json"))
    with pytest.raises(SavedSearchUnexpectedResponseError):
        await client.fetch_es_query({}, "forSale")


async def test_response_missing_es_query_key_raises() -> None:
    client = _client_with(lambda r: httpx.Response(200, json={"status": "ok"}))
    with pytest.raises(SavedSearchUnexpectedResponseError):
        await client.fetch_es_query({}, "forSale")


async def test_response_with_non_string_es_query_raises() -> None:
    client = _client_with(lambda r: httpx.Response(200, json={"esQuery": 123}))
    with pytest.raises(SavedSearchUnexpectedResponseError):
        await client.fetch_es_query({}, "forSale")


async def test_every_error_is_a_saved_search_api_error() -> None:
    """So tools.py's uniform `except SavedSearchApiError` also covers this client."""
    client = _client_with(lambda r: httpx.Response(502))
    with pytest.raises(SavedSearchApiError):
        await client.fetch_es_query({}, "forSale")


async def test_aclose_delegates_to_the_underlying_http_client() -> None:
    closed = False

    class _FakeHttp(httpx.AsyncClient):
        async def aclose(self) -> None:
            nonlocal closed
            closed = True

    client = EsQueryClient(BASE_URL, http_client=_FakeHttp())
    await client.aclose()
    assert closed is True
