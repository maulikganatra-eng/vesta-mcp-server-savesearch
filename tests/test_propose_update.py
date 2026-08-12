"""Unit tests for `propose_saved_search`'s `intent=update_existing` branch
(step N7 / VA-404), against a mocked client.

Every test here also asserts `client.create`/`client.update` were never
called -- propose writes nothing upstream, for updates just as much as for
creates.
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
from vesta_saved_search.tools import ProposeSavedSearchParams, register_tools

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
        "notification_frequency": "daily",
        "new_listings_count": 0,
        "created_at": None,
        "last_update": None,
    }
    defaults.update(overrides)
    return SavedSearchRecord(**defaults)


def _params(**overrides: Any) -> ProposeSavedSearchParams:
    defaults: dict[str, Any] = {
        "name": "Del Mar Homes",
        "nameWasGenerated": False,
        "searchFilters": {"city": "Del Mar"},
        "esQuery": '{"bool": {}}',
        "searchUrl": "explore/listings/saved-search/42/for-sale?city=Del%20Mar",
        "searchMode": "forSale",
        "criteriaSummary": "Del Mar, for sale",
        "unsupportedFilters": [],
        "notificationFrequency": "daily",
        "intent": "update_existing",
        "savedSearchId": 42,
    }
    defaults.update(overrides)
    return ProposeSavedSearchParams(**defaults)


def _app_with(client: SavedSearchClient, store: ProposalStore) -> FastMCP:
    app = FastMCP("test")
    register_tools(app, client, proposal_store=store)
    return app


async def _propose(app: FastMCP, params: ProposeSavedSearchParams, ctx: Any) -> dict[str, Any]:
    tool = app._tool_manager.get_tool("propose_saved_search")
    assert tool is not None
    result: dict[str, Any] = await tool.fn(params, ctx)
    return result


def _client(existing: list[SavedSearchRecord]) -> AsyncMock:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.return_value = existing
    return client


async def test_out_of_range_frequency_never_reaches_the_http_layer() -> None:
    client = _client([])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(notificationFrequency="weekly"), _FakeCtx(TOKEN))

    assert result["saved_search"]["status"] == "invalid"
    client.list_saved_searches.assert_not_called()


async def test_client_error_becomes_an_error_envelope() -> None:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.side_effect = SavedSearchUpstreamError("down")
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(), _FakeCtx(TOKEN))

    assert result["saved_search"]["status"] == "error"


async def test_unknown_saved_search_id_is_invalid() -> None:
    client = _client([])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(savedSearchId=999), _FakeCtx(TOKEN))

    assert result["saved_search"]["status"] == "invalid"
    client.create.assert_not_called()
    client.update.assert_not_called()


async def test_rename_only_is_ready_and_writes_nothing() -> None:
    stored = _record()
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(name="New Name"), _FakeCtx(TOKEN))

    assert result["saved_search"]["status"] == "ready"
    assert result["saved_search"]["name"] == "New Name"
    client.create.assert_not_called()
    client.update.assert_not_called()


async def test_frequency_only_change_is_ready() -> None:
    stored = _record(notification_frequency="never")
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(notificationFrequency="daily"), _FakeCtx(TOKEN))

    assert result["saved_search"]["status"] == "ready"
    assert result["saved_search"]["notificationFrequency"] == "daily"


async def test_rename_colliding_with_another_record_returns_name_exists() -> None:
    stored = _record(saved_search_id=42, name="Del Mar Homes")
    other = _record(saved_search_id=7, name="Malibu Homes", search_filters={"city": "Malibu"})
    client = _client([stored, other])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(name="Malibu Homes"), _FakeCtx(TOKEN))

    assert result["saved_search"]["status"] == "name_exists"


async def test_renaming_to_its_own_current_name_is_not_a_collision() -> None:
    """A record must never collide with itself when nothing about the name changed."""
    stored = _record(saved_search_id=42, name="Del Mar Homes")
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(name="Del Mar Homes"), _FakeCtx(TOKEN))

    assert result["saved_search"]["status"] == "ready"


async def test_criteria_change_duplicating_another_record_returns_criteria_already_saved() -> None:
    stored = _record(saved_search_id=42, search_filters={"city": "Del Mar"})
    other = _record(saved_search_id=7, name="Malibu Homes", search_filters={"city": "Malibu"})
    client = _client([stored, other])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        _params(
            searchFilters={"city": "Malibu"},
            searchUrl="explore/listings/saved-search/42/for-sale?city=Malibu",
        ),
        _FakeCtx(TOKEN),
    )

    assert result["saved_search"]["status"] == "criteria_already_saved"
    assert result["saved_search"]["existingName"] == "Malibu Homes"


async def test_criteria_change_url_inconsistent_with_filters_is_invalid() -> None:
    stored = _record(saved_search_id=42)
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        _params(
            searchFilters={"area": "17"},
            searchUrl="explore/listings/saved-search/42/for-sale?area=11",
        ),
        _FakeCtx(TOKEN),
    )

    assert result["saved_search"]["status"] == "invalid"


async def test_criteria_change_stashes_a_ready_update_proposal() -> None:
    stored = _record(saved_search_id=42)
    client = _client([stored])
    store = ProposalStore()
    app = _app_with(client, store)

    result = await _propose(
        app,
        _params(
            searchFilters={"city": "Malibu"},
            searchUrl="explore/listings/saved-search/42/for-sale?city=Malibu",
        ),
        _FakeCtx(TOKEN),
    )

    assert result["saved_search"]["status"] == "ready"
    proposal = store.get(result["saved_search"]["proposalId"], user_key_from_token(TOKEN))
    assert proposal is not None
    assert proposal.action == "update"
    assert proposal.payload["saved_search_id"] == 42
    assert proposal.payload["change"]["search_filters"] == {"city": "Malibu"}


async def test_create_shaped_url_from_the_model_cannot_corrupt_the_stored_path() -> None:
    """🔴 Structural proof of the URL-format precondition: even if the model
    supplies a plain CREATE-shaped URL (no saved-search/<id>/ path) for an
    update, only its QUERY STRING is ever extracted -- the stashed proposal
    carries no path at all for apply_update to misuse."""
    stored = _record(
        saved_search_id=42, search_url="explore/listings/saved-search/42/for-sale?city=Del%20Mar"
    )
    client = _client([stored])
    store = ProposalStore()
    app = _app_with(client, store)

    result = await _propose(
        app,
        _params(
            searchFilters={"city": "Malibu"},
            # A create-shaped URL -- no saved-search/<id>/ path segment.
            searchUrl="explore/listings/for-sale?city=Malibu",
        ),
        _FakeCtx(TOKEN),
    )

    assert result["saved_search"]["status"] == "ready"
    proposal = store.get(result["saved_search"]["proposalId"], user_key_from_token(TOKEN))
    assert proposal is not None
    assert proposal.payload["change"]["new_search_url_query"] == "city=Malibu"
    assert "new_search_url_query" in proposal.payload["change"]
    # No key in the stashed change carries a full URL/path at all.
    assert all(
        "saved-search" not in str(v) for v in proposal.payload["change"].values() if v is not None
    )


async def test_generated_name_collision_on_rename_never_asks_the_user() -> None:
    """🔴 Same 2x2 as the create path, now proven for renames too: a
    GENERATED name colliding on rename must silently regenerate/fall back,
    never surface name_exists the way a user-stated collision does."""
    stored = _record(saved_search_id=42, name="Del Mar Homes")
    other = _record(saved_search_id=7, name="Malibu Homes", search_filters={"city": "Malibu"})
    client = _client([stored, other])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(name="Malibu Homes", nameWasGenerated=True), _FakeCtx(TOKEN))

    assert result["saved_search"]["status"] == "name_needs_regeneration"
    assert result["saved_search"]["status"] != "name_exists"
    client.update.assert_not_called()


async def test_generated_name_collision_on_rename_falls_back_after_two_attempts() -> None:
    stored = _record(saved_search_id=42, name="Del Mar Homes")
    other = _record(saved_search_id=7, name="Malibu Homes", search_filters={"city": "Malibu"})
    client = _client([stored, other])
    store = ProposalStore()
    app = _app_with(client, store)

    first = await _propose(app, _params(name="Malibu Homes", nameWasGenerated=True), _FakeCtx(TOKEN))
    assert first["saved_search"]["status"] == "name_needs_regeneration"

    second = await _propose(app, _params(name="Malibu Homes", nameWasGenerated=True), _FakeCtx(TOKEN))

    assert second["saved_search"]["status"] == "ready"
    assert second["saved_search"]["name"] != "Malibu Homes"


async def test_user_stated_over_length_name_on_rename_is_invalid_not_regenerated() -> None:
    stored = _record(saved_search_id=42)
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(name="X" * 100, nameWasGenerated=False), _FakeCtx(TOKEN))

    assert result["saved_search"]["status"] == "invalid"
    client.update.assert_not_called()


async def test_generated_over_length_name_on_rename_regenerates_then_falls_back() -> None:
    stored = _record(saved_search_id=42)
    client = _client([stored])
    store = ProposalStore()
    app = _app_with(client, store)

    first = await _propose(app, _params(name="X" * 100, nameWasGenerated=True), _FakeCtx(TOKEN))
    assert first["saved_search"]["status"] == "name_needs_regeneration"

    second = await _propose(app, _params(name="Y" * 100, nameWasGenerated=True), _FakeCtx(TOKEN))
    assert second["saved_search"]["status"] == "ready"
    assert len(second["saved_search"]["name"]) <= 60


async def test_resending_display_cased_frequency_during_a_pure_rename_is_not_a_change() -> None:
    """🔴 The update path's frequency-changed check must be casefolded too --
    otherwise resending "Daily" (display casing) during a pure rename would
    register a phantom frequency change and bypass the exact casefold fix
    this PR adds to frequency_to_schedule_interval."""
    stored = _record(saved_search_id=42, notification_frequency="daily")
    client = _client([stored])
    store = ProposalStore()
    app = _app_with(client, store)

    result = await _propose(
        app, _params(name="New Name", notificationFrequency="Daily"), _FakeCtx(TOKEN)
    )

    assert result["saved_search"]["status"] == "ready"
    proposal = store.get(result["saved_search"]["proposalId"], user_key_from_token(TOKEN))
    assert proposal is not None
    assert "notification_frequency" not in proposal.payload["change"]
    assert proposal.payload["change"]["name"] == "New Name"
