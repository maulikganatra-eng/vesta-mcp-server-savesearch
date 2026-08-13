"""Level 3 contract tests: the update recipe against REAL dev GuestSite and a
REALLY RUNNING local property-search S4 endpoint (steps N7/N8/N9,
VA-404/405/406).

Marked `contract` -- never runs in CI. Requires:

    GUESTSITE_DEV_BEARER_TOKEN in the environment (same token used by
    tests/test_contract_saved_search.py)

    vesta-mcp-server running locally with the S4 route:
        cd ../vesta-mcp-server && git checkout feat/va-403-internal-es-query-endpoint
        docker compose -f docker-compose.local.yml up -d

Skips cleanly if either is missing.

Every record this file creates is deleted in a `finally` block, and every
name carries the `zz-contract-update-` prefix.

🔴 **This is the test that earns its keep.** Set a record to Daily, rename
it, read it back, and assert `scheduleId == 3` -- NOT `notify`, which stays
`false` for Daily -- criteria unchanged, `searchUrl` unchanged, and no
literal `{SavedSearchId}` anywhere. This is the exact scenario the plan
verified by hand on dev and sprint before any of this code existed:
`scheduleInterval` omitted on an update silently reset a Daily record's
`scheduleId` to `null`, HTTP 200, no error, no warning.
"""

from __future__ import annotations

import logging
import os
from uuid import uuid4

import pytest

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.es_query_client import EsQueryClient
from vesta_saved_search.updates import UpdateChange, apply_update

pytestmark = pytest.mark.contract

logger = logging.getLogger(__name__)

GUESTSITE_BASE_URL = "https://www.dev.themls.com/GuestSiteApi"
TOKEN_ENV_VAR = "GUESTSITE_DEV_BEARER_TOKEN"
PROPERTY_SEARCH_URL = os.environ.get("PROPERTY_SEARCH_INTERNAL_URL", "http://localhost:6000")

NAME_PREFIX = "zz-contract-update-"


def _require_token() -> str:
    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        pytest.skip(f"{TOKEN_ENV_VAR} not set -- contract tests need a real GuestSite dev token")
    return token


def _require_property_search_reachable() -> None:
    import httpx

    try:
        httpx.get(f"{PROPERTY_SEARCH_URL}/internal/es-query", timeout=3.0)
    except httpx.RequestError:
        pytest.skip(
            f"{PROPERTY_SEARCH_URL} is not reachable -- start vesta-mcp-server locally "
            "(feat/va-403-internal-es-query-endpoint) to run this test"
        )


def _unique_name(label: str) -> str:
    return f"{NAME_PREFIX}{label}-{uuid4().hex[:8]}"


async def _delete_quietly(client: SavedSearchClient, token: str, saved_search_id: int) -> None:
    try:
        await client.delete(token, saved_search_id)
    except Exception:
        logger.exception("cleanup_failed saved_search_id=%s", saved_search_id)


async def test_rename_a_daily_search_preserves_its_schedule_and_criteria() -> None:
    """🔴 The full-replace regression this feature exists to prevent."""
    token = _require_token()
    _require_property_search_reachable()

    client = SavedSearchClient(GUESTSITE_BASE_URL)
    es_client = EsQueryClient(PROPERTY_SEARCH_URL)
    original_name = _unique_name("daily")
    saved_search_id: int | None = None

    try:
        created = await client.create(
            token,
            name=original_name,
            search_filters={
                "city": "Rancho Santa Fe",
                "mode": "forSale",
                "status": "active,comingSoon",
                "SortBy": "new",
            },
            search_url=(
                "explore/listings/for-sale?city=Rancho%20Santa%20Fe"
                "&mode=forSale&status=active,comingSoon&sortBy=new"
            ),
            es_query='{"bool": {}}',
            notification_frequency="daily",
        )
        saved_search_id = created.saved_search_id
        assert created.notification_frequency == "daily"

        renamed_name = _unique_name("daily-renamed")
        updated = await apply_update(
            client,
            es_client,
            token,
            saved_search_id=saved_search_id,
            change=UpdateChange(name=renamed_name),
        )

        assert updated.name == renamed_name
        # 🔴 scheduleId, not notify -- notify stays False for Daily, and a
        # reader using notify alone would wrongly report notifications off.
        assert updated.notification_frequency == "daily"
        assert updated.search_filters["city"] == "Rancho Santa Fe"
        assert updated.search_url == created.search_url
        assert "{SavedSearchId}" not in updated.search_url
        assert f"saved-search/{saved_search_id}/" in updated.search_url

        # Read back independently, not just trusting the update response.
        records = await client.list_saved_searches(token)
        record = next(r for r in records if r.saved_search_id == saved_search_id)
        assert record.name == renamed_name
        assert record.notification_frequency == "daily"
        assert record.search_filters["city"] == "Rancho Santa Fe"
        assert record.search_url == created.search_url
    finally:
        if saved_search_id is not None:
            await _delete_quietly(client, token, saved_search_id)
        await client.aclose()
        await es_client.aclose()


async def test_frequency_change_leaves_criteria_and_url_untouched() -> None:
    token = _require_token()
    _require_property_search_reachable()

    client = SavedSearchClient(GUESTSITE_BASE_URL)
    es_client = EsQueryClient(PROPERTY_SEARCH_URL)
    name = _unique_name("freq")
    saved_search_id: int | None = None

    try:
        created = await client.create(
            token,
            name=name,
            search_filters={
                "city": "Encinitas",
                "mode": "forSale",
                "status": "active,comingSoon",
                "SortBy": "new",
            },
            search_url=(
                "explore/listings/for-sale?city=Encinitas"
                "&mode=forSale&status=active,comingSoon&sortBy=new"
            ),
            es_query='{"bool": {}}',
            notification_frequency="never",
        )
        saved_search_id = created.saved_search_id

        set_daily = await apply_update(
            client,
            es_client,
            token,
            saved_search_id=saved_search_id,
            change=UpdateChange(notification_frequency="daily"),
        )
        assert set_daily.notification_frequency == "daily"
        assert set_daily.search_filters == created.search_filters
        assert set_daily.search_url == created.search_url

        stopped = await apply_update(
            client,
            es_client,
            token,
            saved_search_id=saved_search_id,
            change=UpdateChange(notification_frequency="never"),
        )
        assert stopped.notification_frequency == "never"
        assert stopped.search_filters == created.search_filters
        assert stopped.search_url == created.search_url
    finally:
        if saved_search_id is not None:
            await _delete_quietly(client, token, saved_search_id)
        await client.aclose()
        await es_client.aclose()


async def test_criteria_change_keeps_the_stored_path_and_updates_the_query_string() -> None:
    token = _require_token()
    _require_property_search_reachable()

    client = SavedSearchClient(GUESTSITE_BASE_URL)
    es_client = EsQueryClient(PROPERTY_SEARCH_URL)
    name = _unique_name("criteria")
    saved_search_id: int | None = None

    try:
        created = await client.create(
            token,
            name=name,
            search_filters={
                "city": "Carlsbad",
                "mode": "forSale",
                "status": "active,comingSoon",
                "SortBy": "new",
            },
            search_url=(
                "explore/listings/for-sale?city=Carlsbad"
                "&mode=forSale&status=active,comingSoon&sortBy=new"
            ),
            es_query='{"bool": {}}',
            notification_frequency="never",
        )
        saved_search_id = created.saved_search_id

        # ⚠️ The real MLS call behind S4 requires more than city+mode -- an
        # incomplete wire-level filter set surfaces as a real 502 (this path
        # skips resolvers entirely, so it inherits MLS's own requirements).
        # See tests/test_contract_es_query.py for where that was discovered.
        new_filters = {
            "city": "Oceanside",
            "mode": "forSale",
            "status": "active,comingSoon",
            "SortBy": "new",
        }
        fresh_es_query = await es_client.fetch_es_query(new_filters, "forSale")
        updated = await apply_update(
            client,
            es_client,
            token,
            saved_search_id=saved_search_id,
            change=UpdateChange(
                search_filters=new_filters,
                fresh_es_query=fresh_es_query,
                new_search_url_query="city=Oceanside&mode=forSale&status=active,comingSoon&sortBy=new",
            ),
        )

        assert updated.search_filters["city"] == "Oceanside"
        assert f"saved-search/{saved_search_id}/" in updated.search_url
        assert updated.search_url.endswith(
            "city=Oceanside&mode=forSale&status=active,comingSoon&sortBy=new"
        )
    finally:
        if saved_search_id is not None:
            await _delete_quietly(client, token, saved_search_id)
        await client.aclose()
        await es_client.aclose()


async def test_delete_then_list_confirms_it_is_gone() -> None:
    token = _require_token()
    client = SavedSearchClient(GUESTSITE_BASE_URL)
    name = _unique_name("delete-lifecycle")

    try:
        created = await client.create(
            token,
            name=name,
            search_filters={
                "city": "Vista",
                "mode": "forSale",
                "status": "active,comingSoon",
                "SortBy": "new",
            },
            search_url=(
                "explore/listings/for-sale?city=Vista&mode=forSale&status=active,comingSoon&sortBy=new"
            ),
            es_query='{"bool": {}}',
            notification_frequency="never",
        )

        await client.delete(token, created.saved_search_id)

        records = await client.list_saved_searches(token)
        assert all(r.saved_search_id != created.saved_search_id for r in records)
    finally:
        await client.aclose()
