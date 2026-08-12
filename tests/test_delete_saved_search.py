"""Unit tests for delete_saved_search (step N9 / VA-406), against a mocked client."""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from mcp.server.fastmcp import FastMCP
import pydantic
import pytest

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.errors import SavedSearchUpstreamError
from vesta_saved_search.models import SavedSearchRecord
from vesta_saved_search.proposals import ProposalStore, user_key_from_token
from vesta_saved_search.tools import DeleteSavedSearchParams, register_tools

pytestmark = pytest.mark.unit

TOKEN_A = "tok-a"
TOKEN_B = "tok-b"


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


async def _delete(app: FastMCP, params: DeleteSavedSearchParams, ctx: Any) -> dict[str, Any]:
    tool = app._tool_manager.get_tool("delete_saved_search")
    assert tool is not None
    result: dict[str, Any] = await tool.fn(params, ctx)
    return result


def _client(existing: list[SavedSearchRecord]) -> AsyncMock:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.return_value = existing
    return client


def test_params_reject_both_shapes_at_once() -> None:
    with pytest.raises(pydantic.ValidationError):
        DeleteSavedSearchParams(savedSearchId=1, proposalId="x", confirmed=True)


def test_params_reject_neither_shape() -> None:
    with pytest.raises(pydantic.ValidationError):
        DeleteSavedSearchParams()


async def test_anonymous_caller_gets_sign_in_required() -> None:
    client = _client([])
    app = _app_with(client, ProposalStore())

    result = await _delete(app, DeleteSavedSearchParams(savedSearchId=42), _FakeCtx(None))

    assert result["saved_search"]["status"] == "sign_in_required"
    client.delete.assert_not_called()


async def test_propose_list_error_becomes_an_error_envelope() -> None:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.side_effect = SavedSearchUpstreamError("down")
    app = _app_with(client, ProposalStore())

    result = await _delete(app, DeleteSavedSearchParams(savedSearchId=42), _FakeCtx(TOKEN_A))

    assert result["saved_search"]["status"] == "error"


async def test_propose_writes_nothing_and_returns_a_proposal_id() -> None:
    stored = _record()
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _delete(app, DeleteSavedSearchParams(savedSearchId=42), _FakeCtx(TOKEN_A))

    assert result["saved_search"]["status"] == "ready"
    assert result["saved_search"]["proposalId"]
    assert result["saved_search"]["name"] == "Del Mar Homes"
    client.delete.assert_not_called()


async def test_propose_unknown_id_is_invalid() -> None:
    client = _client([])
    app = _app_with(client, ProposalStore())

    result = await _delete(app, DeleteSavedSearchParams(savedSearchId=999), _FakeCtx(TOKEN_A))

    assert result["saved_search"]["status"] == "invalid"


async def test_delete_is_never_a_one_shot_on_first_mention() -> None:
    """Proposing alone -- with no subsequent confirm call -- must never delete."""
    stored = _record()
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    await _delete(app, DeleteSavedSearchParams(savedSearchId=42), _FakeCtx(TOKEN_A))

    client.delete.assert_not_called()


async def test_unconfirmed_confirm_call_writes_nothing() -> None:
    stored = _record()
    client = _client([stored])
    store = ProposalStore()
    app = _app_with(client, store)

    proposed = await _delete(app, DeleteSavedSearchParams(savedSearchId=42), _FakeCtx(TOKEN_A))
    proposal_id = proposed["saved_search"]["proposalId"]

    result = await _delete(
        app, DeleteSavedSearchParams(proposalId=proposal_id, confirmed=False), _FakeCtx(TOKEN_A)
    )

    assert result["saved_search"]["status"] == "not_confirmed"
    client.delete.assert_not_called()


async def test_confirmed_delete_calls_client_delete_exactly_once() -> None:
    stored = _record()
    client = _client([stored])
    store = ProposalStore()
    app = _app_with(client, store)

    proposed = await _delete(app, DeleteSavedSearchParams(savedSearchId=42), _FakeCtx(TOKEN_A))
    proposal_id = proposed["saved_search"]["proposalId"]

    result = await _delete(
        app, DeleteSavedSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )

    assert result["saved_search"]["status"] == "ok"
    assert result["saved_search"]["savedSearchId"] == 42
    client.delete.assert_awaited_once_with(TOKEN_A, 42)


async def test_confirming_twice_deletes_once_and_replay_returns_already_deleted() -> None:
    stored = _record()
    client = _client([stored])
    store = ProposalStore()
    app = _app_with(client, store)

    proposed = await _delete(app, DeleteSavedSearchParams(savedSearchId=42), _FakeCtx(TOKEN_A))
    proposal_id = proposed["saved_search"]["proposalId"]

    first = await _delete(
        app, DeleteSavedSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )
    second = await _delete(
        app, DeleteSavedSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )

    assert first["saved_search"]["status"] == "ok"
    assert second["saved_search"]["status"] == "already_deleted"
    client.delete.assert_awaited_once()


async def test_a_save_proposal_passed_here_returns_action_mismatch() -> None:
    """🔴 A save proposal confirmed via delete_saved_search must be refused --
    picking wrong means deleting a search the user was trying to save."""
    stored = _record()
    client = _client([stored])
    store = ProposalStore()
    user_key = user_key_from_token(TOKEN_A)
    save_proposal = store.put(
        user_key,
        action="save",
        payload={
            "search_filters": {"city": "Del Mar"},
            "search_url": "explore/listings/for-sale?city=Del%20Mar",
            "es_query": "{}",
            "notification_frequency": "never",
        },
        name="Del Mar Homes",
        fingerprint="fp",
    )
    app = _app_with(client, store)

    result = await _delete(
        app,
        DeleteSavedSearchParams(proposalId=save_proposal.proposal_id, confirmed=True),
        _FakeCtx(TOKEN_A),
    )

    assert result["saved_search"]["status"] == "proposal_action_mismatch"
    client.delete.assert_not_called()


async def test_a_delete_proposal_id_is_refused_with_another_users_token() -> None:
    stored = _record()
    client = _client([stored])
    store = ProposalStore()
    app = _app_with(client, store)

    proposed = await _delete(app, DeleteSavedSearchParams(savedSearchId=42), _FakeCtx(TOKEN_A))
    proposal_id = proposed["saved_search"]["proposalId"]

    result = await _delete(
        app, DeleteSavedSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_B)
    )

    assert result["saved_search"]["status"] == "proposal_expired"
    client.delete.assert_not_called()


async def test_deleting_an_already_gone_record_is_a_clean_error_not_a_crash() -> None:
    stored = _record()
    client = _client([stored])
    client.delete.side_effect = SavedSearchUpstreamError("404-ish: already gone")
    store = ProposalStore()
    app = _app_with(client, store)

    proposed = await _delete(app, DeleteSavedSearchParams(savedSearchId=42), _FakeCtx(TOKEN_A))
    proposal_id = proposed["saved_search"]["proposalId"]

    result = await _delete(
        app, DeleteSavedSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )

    assert result["saved_search"]["status"] == "error"


async def test_ordering_propose_save_then_propose_delete_leaves_save_unexecutable() -> None:
    """🔴 The dedicated ordering test: propose a save, then propose a delete,
    then confirm -- the save proposal must be GONE rather than executable
    (N5's one-pending-proposal-per-user rule), exactly one delete happens,
    and zero saves happen."""
    stored = _record()
    client = _client([stored])
    store = ProposalStore()
    user_key = user_key_from_token(TOKEN_A)
    save_proposal = store.put(
        user_key,
        action="save",
        payload={
            "search_filters": {"city": "Malibu"},
            "search_url": "explore/listings/for-sale?city=Malibu",
            "es_query": "{}",
            "notification_frequency": "never",
        },
        name="Malibu Homes",
        fingerprint="fp-malibu",
    )
    app = _app_with(client, store)

    proposed_delete = await _delete(app, DeleteSavedSearchParams(savedSearchId=42), _FakeCtx(TOKEN_A))
    delete_proposal_id = proposed_delete["saved_search"]["proposalId"]

    # The save proposal is gone -- superseded, not just "not current".
    assert store.get(save_proposal.proposal_id, user_key) is None

    result = await _delete(
        app,
        DeleteSavedSearchParams(proposalId=delete_proposal_id, confirmed=True),
        _FakeCtx(TOKEN_A),
    )

    assert result["saved_search"]["status"] == "ok"
    client.delete.assert_awaited_once_with(TOKEN_A, 42)
    client.create.assert_not_called()
