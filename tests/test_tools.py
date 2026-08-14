"""Unit tests for the list_saved_searches tool logic, against a mocked client.

The protocol-level proof -- that this tool actually works over a real MCP client,
with `_meta` carrying the token end to end -- lives in
tests/test_component_list_saved_searches.py. These tests are about the tool's own
branching: what it returns for each client outcome, not whether MCP delivers it.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from mcp.server.fastmcp import FastMCP
import pytest

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.errors import SavedSearchUpstreamError
from vesta_saved_search.models import SavedSearchRecord
from vesta_saved_search.tools import LIST_SAVED_SEARCHES_KEY, register_tools

pytestmark = pytest.mark.unit


def _record(**overrides: Any) -> SavedSearchRecord:
    defaults: dict[str, Any] = {
        "saved_search_id": 1,
        "name": "Del Mar Starter",
        "search_mode": "forSale",
        "search_filters": {"area": "11"},
        "search_url": "explore/listings/saved-search/1/for-sale?area=11",
        "notification_frequency": "daily",
        "new_listings_count": 3,
        "created_at": "2026-01-01T00:00:00",
        "last_update": "2026-01-02T00:00:00",
    }
    defaults.update(overrides)
    return SavedSearchRecord(**defaults)


def _app_with_mocked_client(client: SavedSearchClient) -> FastMCP:
    app = FastMCP("test")
    register_tools(app, client)
    return app


def _get_tool_fn(app: FastMCP, name: str) -> Any:
    """Reach the raw registered function, bypassing FastMCP's own dispatch.

    `FastMCP.call_tool` is the public entry point, but it builds its own
    `Context` internally from a live session -- there is no way to hand it the
    fake `ctx` these tests need in order to simulate different tokens without a
    real protocol session. `_tool_manager.get_tool(name).fn` is the underlying
    function FastMCP itself calls, verified directly against the installed SDK.
    """
    tool = app._tool_manager.get_tool(name)
    assert tool is not None, f"tool {name!r} was not registered"
    return tool.fn


async def _call_list_saved_searches(app: FastMCP, ctx: Any) -> dict[str, Any]:
    """Invoke the registered tool function directly, bypassing the MCP protocol.

    Deliberately not a protocol-level call -- these tests are about the tool's
    return-value branching given a fake `ctx`, which is exactly what a direct call
    can check fastest. The `_meta` channel itself is proven separately, over a real
    client, in the component test.
    """
    fn = _get_tool_fn(app, "list_saved_searches")
    result = await fn(ctx)
    payload: dict[str, Any] = result
    return payload


class _FakeMeta:
    def __init__(self, extra: dict[str, Any] | None) -> None:
        self.model_extra = extra


class _FakeRequestContext:
    def __init__(self, meta: object) -> None:
        self.meta = meta


class _FakeCtx:
    def __init__(self, token: str | None) -> None:
        extra = {"guestsite_bearer_token": token} if token else None
        self.request_context = _FakeRequestContext(_FakeMeta(extra))


async def test_anonymous_caller_gets_sign_in_required_without_calling_the_client() -> None:
    """🔴 The real enforcement boundary, independent of the orchestrator's O4 guard."""
    client = AsyncMock(spec=SavedSearchClient)
    app = _app_with_mocked_client(client)

    result = await _call_list_saved_searches(app, _FakeCtx(token=None))

    assert result == {LIST_SAVED_SEARCHES_KEY: {"status": "sign_in_required"}}
    client.list_saved_searches.assert_not_called()


async def test_empty_account_returns_zero_count_not_an_error() -> None:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.return_value = []
    app = _app_with_mocked_client(client)

    result = await _call_list_saved_searches(app, _FakeCtx(token="tok"))

    assert result == {LIST_SAVED_SEARCHES_KEY: {"status": "ok", "count": 0, "savedSearches": []}}


async def test_records_are_shaped_with_every_documented_field() -> None:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.return_value = [_record()]
    app = _app_with_mocked_client(client)

    result = await _call_list_saved_searches(app, _FakeCtx(token="tok"))

    body = result[LIST_SAVED_SEARCHES_KEY]
    assert body["status"] == "ok"
    assert body["count"] == 1
    record = body["savedSearches"][0]
    assert record == {
        "savedSearchId": 1,
        "name": "Del Mar Starter",
        "notificationFrequency": "daily",
        "searchMode": "forSale",
        "searchFilters": {"area": "11"},
        "searchUrl": "explore/listings/saved-search/1/for-sale?area=11",
        "newListingsCount": 3,
        "createdAt": "2026-01-01T00:00:00",
        "lastUpdate": "2026-01-02T00:00:00",
    }


async def test_search_url_present_on_every_record_even_if_empty_string() -> None:
    """`searchUrl` IS the payload of "load" -- it must never be a missing key."""
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.return_value = [_record(search_url="")]
    app = _app_with_mocked_client(client)

    result = await _call_list_saved_searches(app, _FakeCtx(token="tok"))

    record = result[LIST_SAVED_SEARCHES_KEY]["savedSearches"][0]
    assert "searchUrl" in record
    assert record["searchUrl"] == ""


async def test_frequency_renders_as_the_friendly_word_not_the_raw_pair() -> None:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.return_value = [_record(notification_frequency="instantly")]
    app = _app_with_mocked_client(client)

    result = await _call_list_saved_searches(app, _FakeCtx(token="tok"))

    record = result[LIST_SAVED_SEARCHES_KEY]["savedSearches"][0]
    assert record["notificationFrequency"] == "instantly"


async def test_client_error_becomes_an_error_envelope_not_an_exception() -> None:
    """A tool that raises produces an ugly MCP-level failure for the whole turn.
    An API outage should read as "couldn't list your searches", not crash the call.
    """
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.side_effect = SavedSearchUpstreamError("upstream is down")
    app = _app_with_mocked_client(client)

    result = await _call_list_saved_searches(app, _FakeCtx(token="tok"))

    assert result[LIST_SAVED_SEARCHES_KEY]["status"] == "error"
    assert "upstream is down" in result[LIST_SAVED_SEARCHES_KEY]["message"]


async def test_envelope_has_exactly_one_top_level_key() -> None:
    """🔴 The single-key-unwrap contract with the orchestrator.

    `capability_executor.py` unwraps a response with exactly one top-level
    dict-valued key and uses that key as `mode_key`. A second top-level key here
    would break that silently for every caller of this tool.
    """
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.return_value = []
    app = _app_with_mocked_client(client)

    result = await _call_list_saved_searches(app, _FakeCtx(token="tok"))

    assert list(result.keys()) == [LIST_SAVED_SEARCHES_KEY]


async def test_sign_in_required_and_error_envelopes_also_have_one_top_level_key() -> None:
    client = AsyncMock(spec=SavedSearchClient)
    app = _app_with_mocked_client(client)
    anonymous = await _call_list_saved_searches(app, _FakeCtx(token=None))
    assert list(anonymous.keys()) == [LIST_SAVED_SEARCHES_KEY]

    client.list_saved_searches.side_effect = SavedSearchUpstreamError("down")
    errored = await _call_list_saved_searches(app, _FakeCtx(token="tok"))
    assert list(errored.keys()) == [LIST_SAVED_SEARCHES_KEY]


def test_tool_schema_has_no_user_id_parameter() -> None:
    """🔴 Never a user-id parameter. An LLM-supplied identity is not an identity --
    the token on `_meta` is the only identity this tool will ever use.
    """
    client = AsyncMock(spec=SavedSearchClient)
    app = _app_with_mocked_client(client)
    tool = app._tool_manager.get_tool("list_saved_searches")
    assert tool is not None
    schema = tool.parameters
    properties = schema.get("properties", {})
    assert not any("user" in key.lower() or key.lower() == "id" for key in properties)
