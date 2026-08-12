"""Unit tests for the propose_saved_search tool logic (step N5 / VA-401),
against a mocked client. Every test here also asserts `client.create` was
never called -- this tool writes nothing upstream, by construction, and that
must hold across every branch, not just the obviously-safe ones.

The protocol-level cross-user proof lives in
tests/test_component_save_flow.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from mcp.server.fastmcp import FastMCP
import pytest

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.errors import SavedSearchUpstreamError
from vesta_saved_search.models import SavedSearchRecord
from vesta_saved_search.naming import fallback_name
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
        "saved_search_id": 1,
        "name": "Existing Search",
        "search_mode": "forSale",
        "search_filters": {"city": "Malibu"},
        "search_url": "explore/listings/saved-search/1/for-sale?city=Malibu",
        "notification_frequency": "never",
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
        "searchUrl": "explore/listings/for-sale?city=Del%20Mar",
        "searchMode": "forSale",
        "criteriaSummary": "Del Mar, for sale",
        "unsupportedFilters": [],
        "notificationFrequency": "never",
        "intent": "auto",
        "savedSearchId": None,
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


def _client(existing: list[SavedSearchRecord] | None = None) -> AsyncMock:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.return_value = existing or []
    return client


async def test_anonymous_caller_gets_sign_in_required() -> None:
    client = _client()
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(), _FakeCtx(token=None))

    assert result == {"saved_search": {"status": "sign_in_required"}}
    client.list_saved_searches.assert_not_called()
    client.create.assert_not_called()


async def test_update_existing_without_a_saved_search_id_is_invalid() -> None:
    """N7 unlocked `intent=update_existing`, but `savedSearchId` is still
    required to identify which record is being changed -- see
    tests/test_propose_update.py for the full update-proposal behaviour."""
    client = _client()
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app, _params(intent="update_existing", savedSearchId=None), _FakeCtx(token=TOKEN)
    )

    assert result["saved_search"]["status"] == "invalid"
    client.list_saved_searches.assert_not_called()
    client.create.assert_not_called()


async def test_out_of_range_frequency_never_reaches_the_http_layer() -> None:
    client = _client()
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(notificationFrequency="weekly"), _FakeCtx(token=TOKEN))

    assert result["saved_search"]["status"] == "invalid"
    client.list_saved_searches.assert_not_called()
    client.create.assert_not_called()


async def test_url_inconsistent_with_filters_is_refused_before_any_http_call() -> None:
    """The exact contradiction a human produced by hand: area=11 in the URL,
    area: 17 in searchFilters."""
    client = _client()
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        _params(searchFilters={"area": "17"}, searchUrl="explore/listings/for-sale?area=11"),
        _FakeCtx(token=TOKEN),
    )

    assert result["saved_search"]["status"] == "invalid"
    client.list_saved_searches.assert_not_called()
    client.create.assert_not_called()


async def test_criteria_already_saved_takes_precedence_over_name_exists() -> None:
    """A record matching BOTH name and criteria -> criteria_already_saved, not name_exists."""
    existing = _record(name="Del Mar Homes", search_filters={"city": "Del Mar"})
    client = _client([existing])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(), _FakeCtx(token=TOKEN))

    assert result["saved_search"]["status"] == "criteria_already_saved"
    assert result["saved_search"]["existingName"] == "Del Mar Homes"
    client.create.assert_not_called()


async def test_criteria_already_saved_names_the_existing_search() -> None:
    existing = _record(name="My Malibu Search", search_filters={"city": "Del Mar"})
    client = _client([existing])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(name="Totally Different Name"), _FakeCtx(token=TOKEN))

    assert result["saved_search"]["status"] == "criteria_already_saved"
    assert result["saved_search"]["existingName"] == "My Malibu Search"


async def test_user_stated_name_collision_asks_name_exists() -> None:
    existing = _record(name="Del Mar Homes", search_filters={"city": "Malibu"})
    client = _client([existing])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app, _params(name="Del Mar Homes", nameWasGenerated=False), _FakeCtx(token=TOKEN)
    )

    assert result["saved_search"]["status"] == "name_exists"
    client.create.assert_not_called()


async def test_generated_name_collision_never_asks_the_user() -> None:
    """Paired with the test above: SAME name collision, opposite nameWasGenerated,
    opposite outcome -- asserted together so nobody 'simplifies' them back together."""
    existing = _record(name="Del Mar Homes", search_filters={"city": "Malibu"})
    client = _client([existing])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app, _params(name="Del Mar Homes", nameWasGenerated=True), _FakeCtx(token=TOKEN)
    )

    assert result["saved_search"]["status"] == "name_needs_regeneration"
    assert result["saved_search"]["status"] != "name_exists"
    client.create.assert_not_called()


async def test_intent_create_new_with_existing_user_stated_name_is_refused() -> None:
    existing = _record(name="Del Mar Homes", search_filters={"city": "Malibu"})
    client = _client([existing])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        _params(name="Del Mar Homes", nameWasGenerated=False, intent="create_new"),
        _FakeCtx(token=TOKEN),
    )

    assert result["saved_search"]["status"] == "name_conflict_create_only"
    client.create.assert_not_called()


async def test_two_consecutive_generated_name_collisions_trigger_the_fallback() -> None:
    """🔴 Two consecutive failures -> the deterministic fallback fires, and its
    output is asserted exactly."""
    existing = _record(name="Del Mar Homes", search_filters={"city": "Malibu"})
    client = _client([existing])
    store = ProposalStore()
    app = _app_with(client, store)

    first = await _propose(
        app, _params(name="Del Mar Homes", nameWasGenerated=True), _FakeCtx(token=TOKEN)
    )
    assert first["saved_search"]["status"] == "name_needs_regeneration"

    second = await _propose(
        app,
        # Same colliding candidate again -- simulates the model's one retry
        # still landing on a taken name, which is exactly when the server
        # falls back rather than asking a third time.
        _params(name="Del Mar Homes", nameWasGenerated=True),
        _FakeCtx(token=TOKEN),
    )

    assert second["saved_search"]["status"] == "ready"
    expected = fallback_name("Del Mar, for sale", max_length=60)
    assert second["saved_search"]["name"] == expected
    client.create.assert_not_called()


async def test_generated_name_mentioning_unsupported_filter_needs_regeneration() -> None:
    """🔴 The highest-value naming test: 'Del Mar Theater Homes' generated while
    'home theater' is unsupported."""
    client = _client([])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        _params(
            name="Del Mar Theater Homes",
            nameWasGenerated=True,
            unsupportedFilters=["home theater"],
        ),
        _FakeCtx(token=TOKEN),
    )

    assert result["saved_search"]["status"] == "name_needs_regeneration"
    client.create.assert_not_called()


async def test_two_consecutive_unsupported_mentions_also_fall_back() -> None:
    client = _client([])
    store = ProposalStore()
    app = _app_with(client, store)

    params = _params(
        name="Del Mar Theater Homes",
        nameWasGenerated=True,
        unsupportedFilters=["home theater"],
    )
    first = await _propose(app, params, _FakeCtx(token=TOKEN))
    assert first["saved_search"]["status"] == "name_needs_regeneration"

    second = await _propose(app, params, _FakeCtx(token=TOKEN))
    assert second["saved_search"]["status"] == "ready"
    assert "theater" not in second["saved_search"]["name"].lower()


async def test_user_stated_name_over_length_limit_is_invalid_not_regenerated() -> None:
    client = _client([])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        _params(name="X" * 100, nameWasGenerated=False),
        _FakeCtx(token=TOKEN),
    )

    assert result["saved_search"]["status"] == "invalid"
    client.create.assert_not_called()


async def test_ready_response_carries_a_proposal_id_and_display_fields() -> None:
    client = _client([])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(), _FakeCtx(token=TOKEN))

    body = result["saved_search"]
    assert body["status"] == "ready"
    assert body["proposalId"]
    assert body["name"] == "Del Mar Homes"
    assert body["criteriaSummary"] == "Del Mar, for sale"
    assert body["notificationFrequency"] == "never"
    client.create.assert_not_called()


async def test_re_proposal_with_a_changed_generated_name_carries_previous_name() -> None:
    client = _client([])
    store = ProposalStore()
    app = _app_with(client, store)

    first = await _propose(
        app, _params(name="Del Mar Homes A", nameWasGenerated=True), _FakeCtx(token=TOKEN)
    )
    assert first["saved_search"]["status"] == "ready"

    second = await _propose(
        app, _params(name="Del Mar Homes B", nameWasGenerated=True), _FakeCtx(token=TOKEN)
    )

    assert second["saved_search"]["status"] == "ready"
    assert second["saved_search"]["previousName"] == "Del Mar Homes A"


async def test_re_proposal_with_unchanged_name_has_no_previous_name() -> None:
    client = _client([])
    store = ProposalStore()
    app = _app_with(client, store)

    await _propose(app, _params(name="Del Mar Homes", nameWasGenerated=True), _FakeCtx(token=TOKEN))
    second = await _propose(
        app, _params(name="Del Mar Homes", nameWasGenerated=True), _FakeCtx(token=TOKEN)
    )

    assert "previousName" not in second["saved_search"]


async def test_re_proposal_of_a_user_stated_name_never_carries_previous_name() -> None:
    """We never second-guess a name the user chose, even across re-proposals."""
    client = _client([])
    store = ProposalStore()
    app = _app_with(client, store)

    await _propose(app, _params(name="First Choice", nameWasGenerated=False), _FakeCtx(token=TOKEN))
    second = await _propose(
        app, _params(name="Second Choice", nameWasGenerated=False), _FakeCtx(token=TOKEN)
    )

    assert "previousName" not in second["saved_search"]


async def test_client_error_becomes_an_error_envelope_not_an_exception() -> None:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.side_effect = SavedSearchUpstreamError("upstream is down")
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(), _FakeCtx(token=TOKEN))

    assert result["saved_search"]["status"] == "error"
    client.create.assert_not_called()


async def test_second_proposal_for_the_same_user_supersedes_the_first() -> None:
    client = _client([])
    store = ProposalStore()
    app = _app_with(client, store)

    first = await _propose(app, _params(name="First Search"), _FakeCtx(token=TOKEN))
    second = await _propose(
        app,
        _params(
            name="Second Search",
            searchFilters={"city": "Malibu"},
            searchUrl="explore/listings/for-sale?city=Malibu",
        ),
        _FakeCtx(token=TOKEN),
    )

    user_key = user_key_from_token(TOKEN)
    assert store.get(first["saved_search"]["proposalId"], user_key) is None
    assert store.get(second["saved_search"]["proposalId"], user_key) is not None
