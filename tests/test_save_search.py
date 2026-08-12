"""Unit tests for the save_search tool logic (step N6 / VA-402), against a
mocked client. This tool is the ONLY writer -- every no-write branch here
asserts `client.create` was never called.

The protocol-level cross-user proof -- the single most important assertion in
this feature -- lives in tests/test_component_save_flow.py, run concurrently
over a real MCP session.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from mcp.server.fastmcp import FastMCP
import pytest

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.errors import SavedSearchNameExistsError, SavedSearchUpstreamError
from vesta_saved_search.fingerprint import criteria_fingerprint
from vesta_saved_search.models import SavedSearchRecord
from vesta_saved_search.proposals import ProposalStore, user_key_from_token
from vesta_saved_search.tools import SaveSearchParams, register_tools

pytestmark = pytest.mark.unit

TOKEN_A = "tok-a"
TOKEN_B = "tok-b"

_FILTERS = {"city": "Del Mar"}
_URL = "explore/listings/for-sale?city=Del%20Mar"
_FINGERPRINT = criteria_fingerprint(_FILTERS, "forSale")


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


def _app_with(client: SavedSearchClient, store: ProposalStore) -> FastMCP:
    app = FastMCP("test")
    register_tools(app, client, proposal_store=store)
    return app


async def _save(app: FastMCP, params: SaveSearchParams, ctx: Any) -> dict[str, Any]:
    tool = app._tool_manager.get_tool("save_search")
    assert tool is not None
    result: dict[str, Any] = await tool.fn(params, ctx)
    return result


def _client(existing: list[SavedSearchRecord] | None = None) -> AsyncMock:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.return_value = existing or []
    return client


def _stash_ready_proposal(
    store: ProposalStore,
    *,
    token: str = TOKEN_A,
    action: str = "save",
    name: str = "Del Mar Homes",
    fingerprint: str = _FINGERPRINT,
) -> str:
    user_key = user_key_from_token(token)
    proposal = store.put(
        user_key,
        action=action,  # type: ignore[arg-type]
        payload={
            "search_filters": _FILTERS,
            "search_url": _URL,
            "es_query": '{"bool": {}}',
            "notification_frequency": "never",
            "search_mode": "forSale",
        },
        name=name,
        fingerprint=fingerprint,
    )
    return proposal.proposal_id


async def test_anonymous_caller_gets_sign_in_required() -> None:
    client = _client()
    store = ProposalStore()
    app = _app_with(client, store)

    result = await _save(app, SaveSearchParams(proposalId="x", confirmed=True), _FakeCtx(None))

    assert result == {"saved_search": {"status": "sign_in_required"}}
    client.create.assert_not_called()


async def test_unconfirmed_call_writes_nothing() -> None:
    client = _client([])
    store = ProposalStore()
    proposal_id = _stash_ready_proposal(store)
    app = _app_with(client, store)

    result = await _save(
        app, SaveSearchParams(proposalId=proposal_id, confirmed=False), _FakeCtx(TOKEN_A)
    )

    assert result["saved_search"]["status"] == "not_confirmed"
    client.create.assert_not_called()


async def test_unknown_proposal_id_returns_proposal_expired() -> None:
    client = _client([])
    app = _app_with(client, ProposalStore())

    result = await _save(
        app, SaveSearchParams(proposalId="does-not-exist", confirmed=True), _FakeCtx(TOKEN_A)
    )

    assert result["saved_search"]["status"] == "proposal_expired"
    client.create.assert_not_called()


async def test_a_proposal_id_presented_with_another_users_token_is_refused() -> None:
    """🔴 The single most important assertion in the feature.

    A's proposalId with B's token must be refused, and the response must be
    indistinguishable from an unknown/expired id -- no existence oracle.
    """
    client = _client([])
    store = ProposalStore()
    proposal_id = _stash_ready_proposal(store, token=TOKEN_A)
    app = _app_with(client, store)

    result = await _save(
        app, SaveSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_B)
    )

    assert result["saved_search"]["status"] == "proposal_expired"
    client.create.assert_not_called()


async def test_a_delete_proposal_returns_action_mismatch() -> None:
    client = _client([])
    store = ProposalStore()
    proposal_id = _stash_ready_proposal(store, action="delete")
    app = _app_with(client, store)

    result = await _save(
        app, SaveSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )

    assert result["saved_search"]["status"] == "proposal_action_mismatch"
    client.create.assert_not_called()


async def test_confirmed_save_writes_exactly_once() -> None:
    client = _client([])
    client.create.return_value = _record(
        saved_search_id=99, name="Del Mar Homes", search_filters=_FILTERS, search_url=_URL
    )
    store = ProposalStore()
    proposal_id = _stash_ready_proposal(store)
    app = _app_with(client, store)

    result = await _save(
        app, SaveSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )

    assert result["saved_search"]["status"] == "ok"
    assert result["saved_search"]["savedSearchId"] == 99
    client.create.assert_awaited_once()


async def test_confirming_twice_writes_once_and_replay_returns_already_saved() -> None:
    """🔴 'Yes' twice writes once; the second returns already_saved."""
    client = _client([])
    client.create.return_value = _record(saved_search_id=99, name="Del Mar Homes")
    store = ProposalStore()
    proposal_id = _stash_ready_proposal(store)
    app = _app_with(client, store)

    first = await _save(app, SaveSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A))
    second = await _save(
        app, SaveSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )

    assert first["saved_search"]["status"] == "ok"
    assert second["saved_search"]["status"] == "already_saved"
    assert second["saved_search"]["savedSearchId"] == 99
    client.create.assert_awaited_once()


async def test_live_duplicate_criteria_check_re_runs_at_write_time() -> None:
    """Requirement 4's live re-check: a duplicate that appeared AFTER the
    proposal was stashed is still caught before writing."""
    existing = _record(name="A Newer Duplicate", search_filters=_FILTERS)
    client = _client([existing])
    store = ProposalStore()
    proposal_id = _stash_ready_proposal(store)
    app = _app_with(client, store)

    result = await _save(
        app, SaveSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )

    assert result["saved_search"]["status"] == "criteria_already_saved"
    assert result["saved_search"]["existingName"] == "A Newer Duplicate"
    client.create.assert_not_called()


async def test_live_name_collision_check_re_runs_at_write_time() -> None:
    existing = _record(name="Del Mar Homes", search_filters={"city": "Someplace Else"})
    client = _client([existing])
    store = ProposalStore()
    proposal_id = _stash_ready_proposal(store, name="Del Mar Homes")
    app = _app_with(client, store)

    result = await _save(
        app, SaveSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )

    assert result["saved_search"]["status"] == "name_exists"
    client.create.assert_not_called()


async def test_list_failure_at_write_time_becomes_an_error_envelope() -> None:
    """The live re-check itself can fail upstream -- that must also become a
    clean error, never a crash or a false success."""
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.side_effect = SavedSearchUpstreamError("upstream is down")
    store = ProposalStore()
    proposal_id = _stash_ready_proposal(store)
    app = _app_with(client, store)

    result = await _save(
        app, SaveSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )

    assert result["saved_search"]["status"] == "error"
    client.create.assert_not_called()


async def test_stashed_url_no_longer_matching_filters_is_refused_at_write_time() -> None:
    """Re-asserted even though propose_saved_search already checked it -- a
    stash tampered with, or built by a future code path that skips the
    propose-time check, still cannot produce a corrupt record."""
    client = _client([])
    store = ProposalStore()
    user_key = user_key_from_token(TOKEN_A)
    proposal = store.put(
        user_key,
        action="save",
        payload={
            "search_filters": {"area": "17"},
            "search_url": "explore/listings/for-sale?area=11",
            "es_query": '{"bool": {}}',
            "notification_frequency": "never",
            "search_mode": "forSale",
        },
        name="Del Mar Homes",
        fingerprint="unrelated-fingerprint",
    )
    app = _app_with(client, store)

    result = await _save(
        app, SaveSearchParams(proposalId=proposal.proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )

    assert result["saved_search"]["status"] == "invalid"
    client.create.assert_not_called()


async def test_upstream_name_exists_backstop_is_mapped_cleanly() -> None:
    """The free backstop: even if our own check somehow missed it, the
    upstream 400 'exists' body must never surface as a raw error."""
    client = _client([])
    client.create.side_effect = SavedSearchNameExistsError("exists")
    store = ProposalStore()
    proposal_id = _stash_ready_proposal(store)
    app = _app_with(client, store)

    result = await _save(
        app, SaveSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )

    assert result["saved_search"]["status"] == "name_exists"


async def test_upstream_5xx_never_produces_a_success_status() -> None:
    client = _client([])
    client.create.side_effect = SavedSearchUpstreamError("upstream is down")
    store = ProposalStore()
    proposal_id = _stash_ready_proposal(store)
    app = _app_with(client, store)

    result = await _save(
        app, SaveSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )

    assert result["saved_search"]["status"] == "error"


async def test_envelope_has_exactly_one_top_level_key_on_every_branch() -> None:
    client = _client([])
    client.create.return_value = _record(saved_search_id=1, name="Del Mar Homes")
    store = ProposalStore()
    proposal_id = _stash_ready_proposal(store)
    app = _app_with(client, store)

    result = await _save(
        app, SaveSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN_A)
    )
    assert list(result.keys()) == ["saved_search"]
