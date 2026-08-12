"""Unit tests for the update recipe (step N7 / VA-404), against mocked
`SavedSearchClient` / `EsQueryClient`.

Level 1. The real full-replace and URL-format contract-level proof lives in
tests/test_contract_updates.py, run against real dev.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

import pytest

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.errors import SavedSearchUnexpectedResponseError
from vesta_saved_search.es_query_client import EsQueryClient
from vesta_saved_search.models import SavedSearchRecord
from vesta_saved_search.updates import UpdateChange, apply_update

pytestmark = pytest.mark.unit

TOKEN = "tok"


def _record(**overrides: Any) -> SavedSearchRecord:
    defaults: dict[str, Any] = {
        "saved_search_id": 42,
        "name": "Del Mar Homes",
        "search_mode": "forSale",
        "search_filters": {"city": "Del Mar", "consumerId": 999},
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
        return _record(saved_search_id=kwargs["saved_search_id"], **_strip_client_kwargs(kwargs))

    client.update.side_effect = _update
    return client


def _strip_client_kwargs(kwargs: dict[str, Any]) -> dict[str, Any]:
    return {
        "name": kwargs["name"],
        "search_filters": kwargs["search_filters"],
        "search_url": kwargs["search_url"],
        "notification_frequency": kwargs["notification_frequency"],
    }


def _es_query_client(return_value: str = '{"bool": {"fresh": true}}') -> AsyncMock:
    client = AsyncMock(spec=EsQueryClient)
    client.fetch_es_query.return_value = return_value
    return client


async def test_rename_only_carries_frequency_filters_url_and_a_fresh_es_query() -> None:
    """🔴 Assert field by field -- this is the silent-destruction case."""
    stored = _record(name="Old Name", notification_frequency="daily")
    client = _client([stored])
    es_client = _es_query_client()

    await apply_update(
        client, es_client, TOKEN, saved_search_id=42, change=UpdateChange(name="New Name")
    )

    client.update.assert_awaited_once()
    _, kwargs = client.update.call_args
    assert kwargs["name"] == "New Name"
    assert kwargs["notification_frequency"] == "daily"
    assert kwargs["search_filters"] == stored.search_filters
    assert kwargs["search_url"] == stored.search_url
    assert kwargs["es_query"] == '{"bool": {"fresh": true}}'


async def test_es_query_is_never_carried_over_from_the_get() -> None:
    """Spy proof: the esQuery sent came from the S4 client, not from the GET."""
    stored = _record()
    client = _client([stored])
    es_client = _es_query_client(return_value='{"bool": {"marker": "from-s4"}}')

    await apply_update(client, es_client, TOKEN, saved_search_id=42, change=UpdateChange(name="Renamed"))

    es_client.fetch_es_query.assert_awaited_once_with(stored.search_filters, "forSale")
    _, kwargs = client.update.call_args
    assert kwargs["es_query"] == '{"bool": {"marker": "from-s4"}}'


async def test_frequency_change_leaves_filters_and_url_untouched() -> None:
    stored = _record(notification_frequency="never")
    client = _client([stored])
    es_client = _es_query_client()

    await apply_update(
        client,
        es_client,
        TOKEN,
        saved_search_id=42,
        change=UpdateChange(notification_frequency="daily"),
    )

    _, kwargs = client.update.call_args
    assert kwargs["notification_frequency"] == "daily"
    assert kwargs["search_filters"] == stored.search_filters
    assert kwargs["search_url"] == stored.search_url
    assert kwargs["name"] == stored.name


async def test_criteria_change_keeps_the_stored_path_and_gets_a_new_query_string() -> None:
    stored = _record(search_url="explore/listings/saved-search/42/for-sale?city=Del%20Mar")
    client = _client([stored])
    es_client = _es_query_client()

    await apply_update(
        client,
        es_client,
        TOKEN,
        saved_search_id=42,
        change=UpdateChange(
            search_filters={"city": "Malibu"},
            fresh_es_query='{"bool": {"city": "malibu"}}',
            new_search_url_query="city=Malibu&mode=forSale",
        ),
    )

    _, kwargs = client.update.call_args
    assert kwargs["search_url"] == "explore/listings/saved-search/42/for-sale?city=Malibu&mode=forSale"
    assert kwargs["search_filters"] == {"city": "Malibu"}
    assert kwargs["es_query"] == '{"bool": {"city": "malibu"}}'
    # The criteria-changed path must never call the S4 endpoint -- the fresh
    # esQuery was already supplied by build_saved_search_input.
    es_client.fetch_es_query.assert_not_called()


async def test_criteria_change_without_fresh_es_query_raises() -> None:
    client = _client([_record()])
    es_client = _es_query_client()

    with pytest.raises(SavedSearchUnexpectedResponseError):
        await apply_update(
            client,
            es_client,
            TOKEN,
            saved_search_id=42,
            change=UpdateChange(search_filters={"city": "Malibu"}, new_search_url_query="city=Malibu"),
        )


async def test_criteria_change_without_new_query_string_raises() -> None:
    client = _client([_record()])
    es_client = _es_query_client()

    with pytest.raises(SavedSearchUnexpectedResponseError):
        await apply_update(
            client,
            es_client,
            TOKEN,
            saved_search_id=42,
            change=UpdateChange(search_filters={"city": "Malibu"}, fresh_es_query='{"bool": {}}'),
        )


async def test_precondition_rejects_a_create_shaped_url_on_update() -> None:
    """🔴 The create-shaped URL trap: GuestSite stores a literal
    '{SavedSearchId}' placeholder rather than rejecting it, so this must be
    caught before ever sending."""
    stored = _record(saved_search_id=42, search_url="explore/listings/for-sale?city=Del%20Mar")
    client = _client([stored])
    es_client = _es_query_client()

    with pytest.raises(SavedSearchUnexpectedResponseError):
        await apply_update(
            client,
            es_client,
            TOKEN,
            saved_search_id=42,
            change=UpdateChange(
                search_filters={"city": "Malibu"},
                fresh_es_query='{"bool": {}}',
                new_search_url_query="city=Malibu",
            ),
        )
    client.update.assert_not_called()


async def test_unmappable_search_mode_is_refused_before_any_es_query_call() -> None:
    stored = _record(search_mode="commercial")
    client = _client([stored])
    es_client = _es_query_client()

    with pytest.raises(SavedSearchUnexpectedResponseError):
        await apply_update(
            client, es_client, TOKEN, saved_search_id=42, change=UpdateChange(name="Renamed")
        )
    es_client.fetch_es_query.assert_not_called()
    client.update.assert_not_called()


async def test_unknown_saved_search_id_raises() -> None:
    client = _client([_record(saved_search_id=1)])
    es_client = _es_query_client()

    with pytest.raises(SavedSearchUnexpectedResponseError):
        await apply_update(client, es_client, TOKEN, saved_search_id=999, change=UpdateChange(name="X"))


async def test_reads_the_record_fresh_every_call_rather_than_reusing_a_cached_one() -> None:
    """Last-write-wins: apply_update must GET immediately before writing, not
    accept a record from the caller."""
    stored = _record()
    client = _client([stored])
    es_client = _es_query_client()

    await apply_update(client, es_client, TOKEN, saved_search_id=42, change=UpdateChange(name="X"))

    client.list_saved_searches.assert_awaited_once_with(TOKEN)
