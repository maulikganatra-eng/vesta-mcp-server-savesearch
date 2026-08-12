"""Unit tests for `save_search`'s `update`-action confirm branch (step N7 /
VA-404), against mocked `SavedSearchClient` / `EsQueryClient`.

The real full-replace and URL-precondition proof against actual dev/sprint
lives in tests/test_contract_updates.py.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from mcp.server.fastmcp import FastMCP
import pytest

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.errors import SavedSearchUpstreamError
from vesta_saved_search.es_query_client import EsQueryClient
from vesta_saved_search.fingerprint import criteria_fingerprint
from vesta_saved_search.models import SavedSearchRecord
from vesta_saved_search.proposals import ProposalStore, user_key_from_token
from vesta_saved_search.tools import SaveSearchParams, register_tools

pytestmark = pytest.mark.unit

TOKEN = "tok-a"


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


def _client(existing: list[SavedSearchRecord]) -> AsyncMock:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.return_value = existing

    async def _update(token: str, **kwargs: Any) -> SavedSearchRecord:
        return _record(
            saved_search_id=kwargs["saved_search_id"],
            name=kwargs["name"],
            search_filters=kwargs["search_filters"],
            search_url=kwargs["search_url"],
            notification_frequency=kwargs["notification_frequency"],
        )

    client.update.side_effect = _update
    return client


def _es_query_client(return_value: str = '{"bool": {"fresh": true}}') -> AsyncMock:
    es_client = AsyncMock(spec=EsQueryClient)
    es_client.fetch_es_query.return_value = return_value
    return es_client


def _app_with(client: SavedSearchClient, store: ProposalStore, es_client: EsQueryClient) -> FastMCP:
    app = FastMCP("test")
    register_tools(app, client, proposal_store=store, es_query_client=es_client)
    return app


async def _confirm(app: FastMCP, proposal_id: str) -> dict[str, Any]:
    tool = app._tool_manager.get_tool("save_search")
    assert tool is not None
    result: dict[str, Any] = await tool.fn(
        SaveSearchParams(proposalId=proposal_id, confirmed=True), _FakeCtx(TOKEN)
    )
    return result


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


def _stash_update_proposal(
    store: ProposalStore,
    *,
    saved_search_id: int = 42,
    change: dict[str, Any],
    fingerprint: str = "fp",
    name: str = "Del Mar Homes",
) -> str:
    user_key = user_key_from_token(TOKEN)
    proposal = store.put(
        user_key,
        action="update",
        payload={"saved_search_id": saved_search_id, "change": change},
        name=name,
        fingerprint=fingerprint,
    )
    return proposal.proposal_id


async def test_confirmed_rename_calls_update_with_fresh_es_query() -> None:
    stored = _record()
    client = _client([stored])
    es_client = _es_query_client('{"bool": {"marker": "fresh"}}')
    store = ProposalStore()
    proposal_id = _stash_update_proposal(store, change={"name": "New Name"})
    app = _app_with(client, store, es_client)

    result = await _confirm(app, proposal_id)

    assert result["saved_search"]["status"] == "ok"
    assert result["saved_search"]["name"] == "New Name"
    client.update.assert_awaited_once()
    _, kwargs = client.update.call_args
    assert kwargs["es_query"] == '{"bool": {"marker": "fresh"}}'
    assert kwargs["search_filters"] == stored.search_filters


async def test_confirming_twice_updates_once_and_replay_returns_already_saved() -> None:
    stored = _record()
    client = _client([stored])
    es_client = _es_query_client()
    store = ProposalStore()
    proposal_id = _stash_update_proposal(store, change={"name": "New Name"})
    app = _app_with(client, store, es_client)

    first = await _confirm(app, proposal_id)
    second = await _confirm(app, proposal_id)

    assert first["saved_search"]["status"] == "ok"
    assert second["saved_search"]["status"] == "already_saved"
    client.update.assert_awaited_once()


async def test_live_duplicate_criteria_check_re_runs_at_confirm_time() -> None:
    stored = _record(saved_search_id=42, search_filters={"city": "Del Mar"})
    other = _record(saved_search_id=7, name="A Newer Duplicate", search_filters={"city": "Malibu"})
    client = _client([stored, other])
    es_client = _es_query_client()
    store = ProposalStore()
    fingerprint = criteria_fingerprint({"city": "Malibu"}, "forSale")
    proposal_id = _stash_update_proposal(
        store,
        change={
            "search_filters": {"city": "Malibu"},
            "fresh_es_query": '{"bool": {}}',
            "new_search_url_query": "city=Malibu",
        },
        fingerprint=fingerprint,
    )
    app = _app_with(client, store, es_client)

    result = await _confirm(app, proposal_id)

    assert result["saved_search"]["status"] == "criteria_already_saved"
    assert result["saved_search"]["existingName"] == "A Newer Duplicate"
    client.update.assert_not_called()


async def test_live_name_collision_check_re_runs_at_confirm_time() -> None:
    stored = _record(saved_search_id=42)
    other = _record(saved_search_id=7, name="Taken Name", search_filters={"city": "Malibu"})
    client = _client([stored, other])
    es_client = _es_query_client()
    store = ProposalStore()
    proposal_id = _stash_update_proposal(store, change={"name": "Taken Name"})
    app = _app_with(client, store, es_client)

    result = await _confirm(app, proposal_id)

    assert result["saved_search"]["status"] == "name_exists"
    client.update.assert_not_called()


async def test_criteria_change_with_no_duplicate_succeeds() -> None:
    """Exercises the criteria-changed re-check's non-collision path -- a
    change to genuinely unique criteria must fall through to the write."""
    stored = _record(saved_search_id=42, search_filters={"city": "Del Mar"})
    client = _client([stored])
    es_client = _es_query_client()
    store = ProposalStore()
    proposal_id = _stash_update_proposal(
        store,
        change={
            "search_filters": {"city": "Malibu"},
            "fresh_es_query": '{"bool": {"city": "malibu"}}',
            "new_search_url_query": "city=Malibu",
        },
        fingerprint=criteria_fingerprint({"city": "Malibu"}, "forSale"),
    )
    app = _app_with(client, store, es_client)

    result = await _confirm(app, proposal_id)

    assert result["saved_search"]["status"] == "ok"
    client.update.assert_awaited_once()


async def test_update_never_collides_with_the_record_being_updated_itself() -> None:
    stored = _record(saved_search_id=42, name="Del Mar Homes")
    client = _client([stored])
    es_client = _es_query_client()
    store = ProposalStore()
    proposal_id = _stash_update_proposal(store, change={"notification_frequency": "never"})
    app = _app_with(client, store, es_client)

    result = await _confirm(app, proposal_id)

    assert result["saved_search"]["status"] == "ok"


async def test_upstream_failure_never_produces_a_success_status() -> None:
    client = _client([_record()])
    client.update.side_effect = SavedSearchUpstreamError("upstream is down")
    es_client = _es_query_client()
    store = ProposalStore()
    proposal_id = _stash_update_proposal(store, change={"name": "New Name"})
    app = _app_with(client, store, es_client)

    result = await _confirm(app, proposal_id)

    assert result["saved_search"]["status"] == "error"


async def test_deleted_record_at_confirm_time_becomes_a_clean_error() -> None:
    """The saved search vanished between propose and confirm (e.g. deleted
    in another tab) -- apply_update's "not found" must surface cleanly."""
    client = _client([])  # empty: record 42 no longer exists
    es_client = _es_query_client()
    store = ProposalStore()
    proposal_id = _stash_update_proposal(store, change={"name": "New Name"})
    app = _app_with(client, store, es_client)

    result = await _confirm(app, proposal_id)

    assert result["saved_search"]["status"] == "error"
    client.update.assert_not_called()
