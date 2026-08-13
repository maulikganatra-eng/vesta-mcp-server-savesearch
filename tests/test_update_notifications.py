"""Unit tests for update_saved_search_notifications (step N8 / VA-405).

This tool only PROPOSES -- it never writes. The actual write goes through
`save_search`'s update branch, already covered by test_confirm_update.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from mcp.server.fastmcp import FastMCP
import pytest

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.errors import SavedSearchUpstreamError
from vesta_saved_search.models import SavedSearchRecord
from vesta_saved_search.proposals import ProposalStore, user_key_from_token
from vesta_saved_search.tools import UpdateSavedSearchNotificationsParams, register_tools

pytestmark = pytest.mark.unit

TOKEN = "tok-a"


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


def _record(**overrides: Any) -> SavedSearchRecord:
    defaults: dict[str, Any] = {
        "saved_search_id": 42,
        "name": "Del Mar Homes",
        "search_mode": "forSale",
        "search_filters": {"city": "Del Mar"},
        "search_url": "explore/listings/saved-search/42/for-sale?city=Del%20Mar",
        "notification_frequency": "never",
        "new_listings_count": 0,
        "created_at": None,
        "last_update": None,
    }
    defaults.update(overrides)
    return SavedSearchRecord(**defaults)


def _app_with(client: SavedSearchClient, store: ProposalStore) -> FastMCP:
    app = FastMCP("test")
    register_tools(app, client, proposal_store=store)
    return app


async def _propose_notifications(
    app: FastMCP, params: UpdateSavedSearchNotificationsParams, ctx: Any
) -> dict[str, Any]:
    tool = app._tool_manager.get_tool("update_saved_search_notifications")
    assert tool is not None
    result: dict[str, Any] = await tool.fn(params, ctx)
    return result


def _client(existing: list[SavedSearchRecord]) -> AsyncMock:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.return_value = existing
    return client


async def test_anonymous_caller_gets_sign_in_required() -> None:
    client = _client([])
    app = _app_with(client, ProposalStore())

    result = await _propose_notifications(
        app,
        UpdateSavedSearchNotificationsParams(savedSearchId=42, notificationFrequency="daily"),
        _FakeCtx(None),
    )

    assert result["saved_search"]["status"] == "sign_in_required"
    client.update.assert_not_called()


async def test_out_of_range_frequency_never_reaches_the_http_layer() -> None:
    client = _client([])
    app = _app_with(client, ProposalStore())

    result = await _propose_notifications(
        app,
        UpdateSavedSearchNotificationsParams(savedSearchId=42, notificationFrequency="weekly"),
        _FakeCtx(TOKEN),
    )

    assert result["saved_search"]["status"] == "invalid"
    client.list_saved_searches.assert_not_called()


async def test_client_error_becomes_an_error_envelope() -> None:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.side_effect = SavedSearchUpstreamError("down")
    app = _app_with(client, ProposalStore())

    result = await _propose_notifications(
        app,
        UpdateSavedSearchNotificationsParams(savedSearchId=42, notificationFrequency="daily"),
        _FakeCtx(TOKEN),
    )

    assert result["saved_search"]["status"] == "error"


async def test_unknown_saved_search_id_is_invalid() -> None:
    client = _client([])
    app = _app_with(client, ProposalStore())

    result = await _propose_notifications(
        app,
        UpdateSavedSearchNotificationsParams(savedSearchId=999, notificationFrequency="daily"),
        _FakeCtx(TOKEN),
    )

    assert result["saved_search"]["status"] == "invalid"


async def test_valid_request_stashes_a_ready_update_proposal_and_writes_nothing() -> None:
    stored = _record(notification_frequency="never")
    client = _client([stored])
    store = ProposalStore()
    app = _app_with(client, store)

    result = await _propose_notifications(
        app,
        UpdateSavedSearchNotificationsParams(savedSearchId=42, notificationFrequency="daily"),
        _FakeCtx(TOKEN),
    )

    assert result["saved_search"]["status"] == "ready"
    assert result["saved_search"]["notificationFrequency"] == "daily"
    client.update.assert_not_called()

    proposal = store.get(result["saved_search"]["proposalId"], user_key_from_token(TOKEN))
    assert proposal is not None
    assert proposal.action == "update"
    assert proposal.payload["change"] == {"notification_frequency": "daily"}
    # Only the frequency is in the change -- name/filters untouched, so the
    # confirm-time recipe carries everything else over from the fresh GET.
    assert "name" not in proposal.payload["change"]
    assert "search_filters" not in proposal.payload["change"]


async def test_unmappable_search_mode_is_invalid_at_propose_time() -> None:
    """🔴 A frequency-only change always refreshes esQuery via S4, which
    requires mapping the record's stored searchType -- unlike
    propose_saved_search's rename path, N8 has no fingerprint gate at all,
    so this really is reachable for any record with a corrupted/unexpected
    stored searchType."""
    stored = _record(search_mode="commercial")
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose_notifications(
        app,
        UpdateSavedSearchNotificationsParams(savedSearchId=42, notificationFrequency="daily"),
        _FakeCtx(TOKEN),
    )

    assert result["saved_search"]["status"] == "invalid"
    assert "cannot update" in result["saved_search"]["message"]
    client.update.assert_not_called()


async def test_does_not_bypass_confirmation() -> None:
    """An unconfirmed call -- i.e. this tool alone, with no save_search
    confirm afterward -- writes nothing. Asserted by construction: this tool
    has no `confirmed` field and no write code path at all."""
    stored = _record()
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    await _propose_notifications(
        app,
        UpdateSavedSearchNotificationsParams(savedSearchId=42, notificationFrequency="daily"),
        _FakeCtx(TOKEN),
    )

    client.update.assert_not_called()
    client.create.assert_not_called()
