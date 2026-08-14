"""The update / notifications / delete flow over a REAL MCP client (steps
N7/N8/N9, VA-404/405/406), with both the GuestSite API and the S4 esQuery
endpoint faked.

Level 2b per the build plan. Faking the S4 endpoint here (rather than
requiring a real running property-search server) is deliberate: level 2b is
"real protocol, faked externals" -- the real S4 proof lives in
tests/test_contract_updates.py, run against a locally running
vesta-mcp-server.
"""

from __future__ import annotations

import json
from typing import Any

import httpx
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.es_query_client import EsQueryClient
from vesta_saved_search.identity import META_TOKEN_KEY
from vesta_saved_search.proposals import ProposalStore
from vesta_saved_search.server import create_app

pytestmark = pytest.mark.component

GUESTSITE_BASE_URL = "https://www.dev.themls.com/GuestSiteApi"
PROPERTY_SEARCH_BASE_URL = "http://localhost:6000"

TOKEN_A = "eyJhbGciOiJIUzI1NiJ9.fake-user-a.update-flow-sig-a"
TOKEN_B = "eyJhbGciOiJIUzI1NiJ9.fake-user-b.update-flow-sig-b"


class _FakeGuestSite:
    def __init__(self) -> None:
        self._accounts: dict[str, list[dict[str, Any]]] = {}
        self._next_id = 2000
        self.update_calls: list[dict[str, Any]] = []
        self.delete_calls: list[int] = []

    def seed(self, token: str, *, name: str, filters: dict[str, Any], city: str) -> int:
        self._next_id += 1
        record_id = self._next_id
        # The URL is built from the REAL assigned id, never a guessed one --
        # a mismatch here would make the URL-format precondition fail this
        # test for the wrong reason (a bad fixture, not a real bug).
        encoded_city = city.replace(" ", "%20")
        url = f"explore/listings/saved-search/{record_id}/for-sale?city={encoded_city}"
        record = {
            "savedSearchId": record_id,
            "searchNumber": "fake",
            "searchName": name,
            "searchType": filters.get("mode", "forSale"),
            "criteria": None,
            "searchFilters": json.dumps(filters),
            "searchUrl": url,
            "lastUpdate": "2026-01-01T00:00:00",
            "consumerId": 1,
            "newListingsSinceCertainDate": 0,
            "notify": False,
            "scheduleId": 3,
            "createdDate": "2026-01-01T00:00:00",
        }
        self._accounts.setdefault(token, []).append(record)
        return record_id

    def handler(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        account = self._accounts.setdefault(token, [])

        if request.method == "GET":
            return httpx.Response(200, json=account)

        if request.method == "DELETE":
            saved_search_id = int(request.url.path.rsplit("/", 1)[-1])
            self.delete_calls.append(saved_search_id)
            self._accounts[token] = [r for r in account if r["savedSearchId"] != saved_search_id]
            return httpx.Response(200, json={"status": "ok", "savedSearch": None})

        if request.method == "POST":
            body = json.loads(request.content)
            saved_search_id = body.get("savedSearchId")
            self.update_calls.append(body)
            for record in account:
                if record["savedSearchId"] == saved_search_id:
                    record["searchName"] = body["searchName"]
                    record["searchFilters"] = json.dumps(body["searchFilters"])
                    record["searchUrl"] = body["searchUrl"]
                    # Full-replace: scheduleInterval 0/absent -> never (scheduleId null).
                    schedule_interval = body.get("scheduleInterval")
                    record["scheduleId"] = {0: None, 1: 3, 2: 1}.get(schedule_interval)
                    record["notify"] = schedule_interval == 2
                    return httpx.Response(
                        200, json={"savedSearch": json.dumps([record]), "status": "ok"}
                    )
            return httpx.Response(404, json={"error": "not found"})

        return httpx.Response(405)


def _es_query_handler(request: httpx.Request) -> httpx.Response:
    body = json.loads(request.content)
    marker = f"fresh-for-{body['searchFilters']}"
    return httpx.Response(200, json={"esQuery": json.dumps({"marker": marker})})


def _app(fake: _FakeGuestSite) -> Any:
    guestsite_http = httpx.AsyncClient(
        transport=httpx.MockTransport(fake.handler), base_url=GUESTSITE_BASE_URL
    )
    client = SavedSearchClient(GUESTSITE_BASE_URL, http_client=guestsite_http)

    es_http = httpx.AsyncClient(
        transport=httpx.MockTransport(_es_query_handler), base_url=PROPERTY_SEARCH_BASE_URL
    )
    es_client = EsQueryClient(PROPERTY_SEARCH_BASE_URL, http_client=es_http)

    return create_app(
        saved_search_client=client, proposal_store=ProposalStore(), es_query_client=es_client
    )


def _envelope(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    return dict(json.loads(result.content[0].text))


async def test_rename_only_over_a_real_mcp_client_preserves_frequency_and_filters() -> None:
    fake = _FakeGuestSite()
    saved_search_id = fake.seed(
        TOKEN_A, name="Old Name", filters={"city": "Del Mar", "mode": "forSale"}, city="Del Mar"
    )
    stored_url = f"explore/listings/saved-search/{saved_search_id}/for-sale?city=Del%20Mar"

    async with create_connected_server_and_client_session(_app(fake)) as client:
        proposed = await client.call_tool(
            "update_saved_search",
            {
                "params": {
                    "savedSearchId": saved_search_id,
                    "name": "New Name",
                    "nameWasGenerated": False,
                    "searchFilters": {"city": "Del Mar", "mode": "forSale"},
                    "esQuery": '{"bool": {"stale": true}}',
                    "searchUrl": stored_url,
                    "searchMode": "forSale",
                    "criteriaSummary": "Del Mar, for sale",
                    "unsupportedFilters": [],
                    "notificationFrequency": "daily",
                }
            },
            meta={META_TOKEN_KEY: TOKEN_A},
        )
        proposal_id = _envelope(proposed)["update_saved_search"]["proposalId"]

        confirmed = await client.call_tool(
            "update_saved_search",
            {"params": {"proposalId": proposal_id, "confirmed": True}},
            meta={META_TOKEN_KEY: TOKEN_A},
        )

    body = _envelope(confirmed)["update_saved_search"]
    assert body["status"] == "ok"
    assert body["name"] == "New Name"
    assert len(fake.update_calls) == 1
    sent = fake.update_calls[0]
    # 🔴 Field-by-field: rename must carry filters/URL/frequency through
    # unchanged, and the esQuery must be the FRESH one from S4, never the
    # stale value the model happened to resend.
    assert sent["searchFilters"] == {"city": "Del Mar", "mode": "forSale"}
    assert sent["searchUrl"] == stored_url
    assert sent["esQuery"] != '{"bool": {"stale": true}}'
    assert "fresh-for-" in sent["esQuery"]


async def test_notification_frequency_change_via_update_saved_search() -> None:
    """The frequency-only path a prior version of this server exposed as a
    separate `update_saved_search_notifications` tool -- folded into
    `update_saved_search` since a frequency-only call needs only
    `savedSearchId` + `notificationFrequency`, exactly as the dedicated tool
    did."""
    fake = _FakeGuestSite()
    saved_search_id = fake.seed(
        TOKEN_A, name="Del Mar Daily", filters={"city": "Del Mar", "mode": "forSale"}, city="Del Mar"
    )

    async with create_connected_server_and_client_session(_app(fake)) as client:
        proposed = await client.call_tool(
            "update_saved_search",
            {"params": {"savedSearchId": saved_search_id, "notificationFrequency": "never"}},
            meta={META_TOKEN_KEY: TOKEN_A},
        )
        assert _envelope(proposed)["update_saved_search"]["status"] == "ready"
        assert len(fake.update_calls) == 0, "update_saved_search must not write before confirmation"

        proposal_id = _envelope(proposed)["update_saved_search"]["proposalId"]
        confirmed = await client.call_tool(
            "update_saved_search",
            {"params": {"proposalId": proposal_id, "confirmed": True}},
            meta={META_TOKEN_KEY: TOKEN_A},
        )

    body = _envelope(confirmed)["update_saved_search"]
    assert body["status"] == "ok"
    assert body["notificationFrequency"] == "never"
    sent = fake.update_calls[0]
    assert sent["searchFilters"] == {"city": "Del Mar", "mode": "forSale"}
    expected_url = f"explore/listings/saved-search/{saved_search_id}/for-sale?city=Del%20Mar"
    assert sent["searchUrl"] == expected_url


async def test_delete_ordering_propose_save_then_propose_delete_then_confirm() -> None:
    """🔴 Propose a save, then propose a delete, then confirm: the save
    proposal is GONE rather than executable; exactly one delete, zero saves."""
    fake = _FakeGuestSite()
    saved_search_id = fake.seed(
        TOKEN_A, name="To Be Deleted", filters={"city": "Malibu", "mode": "forSale"}, city="Malibu"
    )

    async with create_connected_server_and_client_session(_app(fake)) as client:
        save_proposal = await client.call_tool(
            "propose_saved_search",
            {
                "params": {
                    "name": "A New Save",
                    "nameWasGenerated": False,
                    "searchFilters": {"city": "Santa Monica"},
                    "esQuery": "{}",
                    "searchUrl": "explore/listings/for-sale?city=Santa%20Monica",
                    "searchMode": "forSale",
                    "criteriaSummary": "Santa Monica, for sale",
                    "unsupportedFilters": [],
                    "notificationFrequency": "never",
                }
            },
            meta={META_TOKEN_KEY: TOKEN_A},
        )
        save_proposal_id = _envelope(save_proposal)["propose_saved_search"]["proposalId"]

        delete_proposal = await client.call_tool(
            "delete_saved_search",
            {"params": {"savedSearchId": saved_search_id}},
            meta={META_TOKEN_KEY: TOKEN_A},
        )
        delete_proposal_id = _envelope(delete_proposal)["delete_saved_search"]["proposalId"]

        stale_save_attempt = await client.call_tool(
            "save_search",
            {"params": {"proposalId": save_proposal_id, "confirmed": True}},
            meta={META_TOKEN_KEY: TOKEN_A},
        )

        confirmed_delete = await client.call_tool(
            "delete_saved_search",
            {"params": {"proposalId": delete_proposal_id, "confirmed": True}},
            meta={META_TOKEN_KEY: TOKEN_A},
        )

    assert _envelope(stale_save_attempt)["save_search"]["status"] == "proposal_expired"
    assert _envelope(confirmed_delete)["delete_saved_search"]["status"] == "ok"
    assert fake.delete_calls == [saved_search_id]
    assert len(fake.update_calls) == 0
