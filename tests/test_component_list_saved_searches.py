"""🔴 list_saved_searches over a REAL MCP client, with the GuestSite API faked.

Level 2b per the build plan: "Real MCP client, faked API with a realistic
account. Assert the full output shape and that nothing extra leaked in." This is
also the cross-user leak test the plan calls out specifically for N1's
prohibition, run here against the real tool rather than a throwaway echo tool.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.identity import META_TOKEN_KEY
from vesta_saved_search.server import create_app

pytestmark = pytest.mark.component

BASE_URL = "https://www.dev.themls.com/GuestSiteApi"

# Two distinguishable fake tokens map to two distinguishable fake accounts below.
TOKEN_A = "eyJhbGciOiJIUzI1NiJ9.fake-user-a.sig"
TOKEN_B = "eyJhbGciOiJIUzI1NiJ9.fake-user-b.sig"

_ACCOUNT_A = [
    {
        "savedSearchId": 101,
        "searchName": "A-Malibu",
        "searchType": "forSale",
        "criteria": None,
        "searchFilters": json.dumps({"area": "17", "consumerId": 555}),
        "searchUrl": "explore/listings/saved-search/101/for-sale?area=17",
        "lastUpdate": "2026-01-01T00:00:00",
        "createdDate": "2025-12-01T00:00:00",
        "newListingsSinceCertainDate": 2,
        "notify": True,
        "scheduleId": 1,
    }
]

_ACCOUNT_B: list[dict[str, Any]] = []  # an empty account, deliberately


def _fake_transport() -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        if auth == f"Bearer {TOKEN_A}":
            return httpx.Response(200, json=_ACCOUNT_A)
        if auth == f"Bearer {TOKEN_B}":
            return httpx.Response(200, json=_ACCOUNT_B)
        return httpx.Response(401, json={"error": "unrecognised token in this fake"})

    return httpx.MockTransport(handler)


def _real_client_over_fake_transport() -> SavedSearchClient:
    http_client = httpx.AsyncClient(transport=_fake_transport(), base_url=BASE_URL)
    return SavedSearchClient(BASE_URL, http_client=http_client)


def _app() -> Any:
    return create_app(saved_search_client=_real_client_over_fake_transport())


def _envelope(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    return dict(json.loads(result.content[0].text))


async def test_full_envelope_over_a_real_mcp_client() -> None:
    async with create_connected_server_and_client_session(_app()) as client:
        result = await client.call_tool("list_saved_searches", {}, meta={META_TOKEN_KEY: TOKEN_A})

    envelope = _envelope(result)
    assert list(envelope.keys()) == ["list_saved_searches"]
    body = envelope["list_saved_searches"]
    assert body["status"] == "ok"
    assert body["count"] == 1
    record = body["savedSearches"][0]
    assert record["savedSearchId"] == 101
    assert record["name"] == "A-Malibu"
    assert record["searchFilters"] == {"area": "17", "consumerId": 555}
    assert record["notificationFrequency"] == "instantly"
    assert "searchUrl" in record


async def test_empty_account_over_a_real_mcp_client() -> None:
    async with create_connected_server_and_client_session(_app()) as client:
        result = await client.call_tool("list_saved_searches", {}, meta={META_TOKEN_KEY: TOKEN_B})

    body = _envelope(result)["list_saved_searches"]
    assert body == {"status": "ok", "count": 0, "savedSearches": []}


async def test_anonymous_over_a_real_mcp_client() -> None:
    async with create_connected_server_and_client_session(_app()) as client:
        result = await client.call_tool("list_saved_searches", {})

    assert _envelope(result) == {"list_saved_searches": {"status": "sign_in_required"}}


async def test_first_cross_system_check_envelope_unwraps_to_saved_search_mode_key() -> None:
    """The cross-repo contract this step establishes: a response with exactly one
    top-level dict-valued key, which the orchestrator's `capability_executor`
    treats as `mode_key`. Asserted here at the protocol level since that is the
    only place the real shape is observable end to end from this repo.
    """
    async with create_connected_server_and_client_session(_app()) as client:
        result = await client.call_tool("list_saved_searches", {}, meta={META_TOKEN_KEY: TOKEN_A})

    envelope = _envelope(result)
    top_level_dict_keys = [k for k, v in envelope.items() if isinstance(v, dict)]
    assert top_level_dict_keys == ["list_saved_searches"], (
        "more than one top-level dict-valued key would make the orchestrator's "
        "single-key unwrap ambiguous, changing mode_key for every caller"
    )


async def test_cross_user_isolation_two_accounts_one_session() -> None:
    """🔴 The leak this N1 prohibition and N3's client wiring both exist to prevent.

    Two calls on ONE session, two different tokens. Neither call's records may
    leak into the other's response.
    """
    async with create_connected_server_and_client_session(_app()) as client:
        result_a = await client.call_tool("list_saved_searches", {}, meta={META_TOKEN_KEY: TOKEN_A})
        result_b = await client.call_tool("list_saved_searches", {}, meta={META_TOKEN_KEY: TOKEN_B})

    body_a = _envelope(result_a)["list_saved_searches"]
    body_b = _envelope(result_b)["list_saved_searches"]

    assert body_a["count"] == 1
    assert body_a["savedSearches"][0]["name"] == "A-Malibu"
    assert body_b == {"status": "ok", "count": 0, "savedSearches": []}, (
        "user B was served user A's saved searches -- a cross-tenant leak"
    )
