"""🔴 The propose -> confirm -> save flow over a REAL MCP client (steps N5/N6,
VA-401/VA-402), with the GuestSite API faked.

Level 2b per the build plan. This is also the cross-user leak test the plan
calls out specifically -- the proposal store is the only server-side state
this feature introduces, and therefore the only place a cross-user leak can
be created. See `test_a_proposalid_from_one_user_is_refused_with_another_users_token`
below: the single most important assertion in this feature, run
CONCURRENTLY so both users have a proposal pending at once.

Both users share ONE app instance (and therefore one ProposalStore) in every
test here -- the in-process equivalent of the orchestrator's pooled MCP
sessions forcing `MCP_POOL_SIZE=1`. With the default pool size of 3 and only
two users, they might each land on a separate session and this test would
pass by luck while the bug was still there; sharing one app removes that luck.
"""

from __future__ import annotations

import asyncio
import json
from typing import Any

import httpx
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.identity import META_TOKEN_KEY
from vesta_saved_search.proposals import ProposalStore
from vesta_saved_search.server import create_app

pytestmark = pytest.mark.component

BASE_URL = "https://www.dev.themls.com/GuestSiteApi"

TOKEN_A = "eyJhbGciOiJIUzI1NiJ9.fake-user-a.propose-confirm-sig-a"
TOKEN_B = "eyJhbGciOiJIUzI1NiJ9.fake-user-b.propose-confirm-sig-b"

_VALID_PROPOSAL_INPUT: dict[str, Any] = {
    "name": "Del Mar Homes",
    "nameWasGenerated": False,
    "searchFilters": {"city": "Del Mar"},
    "esQuery": '{"bool": {}}',
    "searchUrl": "explore/listings/for-sale?city=Del%20Mar",
    "searchMode": "forSale",
    "criteriaSummary": "Del Mar, for sale",
    "unsupportedFilters": [],
    "notificationFrequency": "never",
}


class _FakeGuestSite:
    """A minimal, real-shaped fake of the GuestSite SavedSearches API.

    Tracks each account's records independently by bearer token, so
    write-isolation between two "accounts" sharing one fake server is
    actually exercised, not just assumed.
    """

    def __init__(self) -> None:
        self._accounts: dict[str, list[dict[str, Any]]] = {}
        self._next_id = 1000
        self.create_calls: list[tuple[str, dict[str, Any]]] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        auth = request.headers.get("authorization", "")
        token = auth.removeprefix("Bearer ")
        account = self._accounts.setdefault(token, [])

        if request.method == "GET":
            return httpx.Response(200, json=account)

        if request.method == "POST":
            body = json.loads(request.content)
            name = body["searchName"]
            if any(r["searchName"] == name for r in account):
                return httpx.Response(400, json={"status": "exists", "savedSearch": None})

            self._next_id += 1
            # Post-GS-8670 decode of scheduleInterval -> (notify, scheduleId), matching
            # what QA returns as of 2026-09-10 (see tests/fixtures/guestsite_qa/README.md):
            # never -> (False, None), daily -> (True, 3), instantly -> (True, 1). Before
            # this fake decoded every create as (False, None) regardless of what was
            # sent, so no create-path test here could ever observe anything but "never"
            # in the response -- exactly the GS-8693/GS-8694 blind spot.
            schedule_interval = body.get("scheduleInterval")
            schedule_id = {0: None, 1: 3, 2: 1}.get(schedule_interval)
            notify = schedule_interval in (1, 2)
            record = {
                "savedSearchId": self._next_id,
                "searchNumber": "fake",
                "searchName": name,
                "searchType": body["searchFilters"].get("mode", "forSale"),
                "criteria": None,
                "searchFilters": json.dumps(body["searchFilters"]),
                "searchUrl": body["searchUrl"],
                "lastUpdate": "2026-01-01T00:00:00",
                "consumerId": 1,
                "newListingsSinceCertainDate": 0,
                "notify": notify,
                "scheduleId": schedule_id,
                "createdDate": "2026-01-01T00:00:00",
            }
            account.append(record)
            self.create_calls.append((token, body))
            return httpx.Response(200, json={"savedSearch": json.dumps([record]), "status": "ok"})

        return httpx.Response(405)


def _client_over(fake: _FakeGuestSite) -> SavedSearchClient:
    transport = httpx.MockTransport(fake.handler)
    http_client = httpx.AsyncClient(transport=transport, base_url=BASE_URL)
    return SavedSearchClient(BASE_URL, http_client=http_client)


def _app(fake: _FakeGuestSite) -> Any:
    return create_app(saved_search_client=_client_over(fake), proposal_store=ProposalStore())


def _envelope(result: Any) -> dict[str, Any]:
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    return dict(json.loads(result.content[0].text))


async def test_full_propose_confirm_write_over_a_real_mcp_client() -> None:
    fake = _FakeGuestSite()
    async with create_connected_server_and_client_session(_app(fake)) as client:
        proposed = await client.call_tool(
            "propose_saved_search", {"params": _VALID_PROPOSAL_INPUT}, meta={META_TOKEN_KEY: TOKEN_A}
        )
        proposal_body = _envelope(proposed)["propose_saved_search"]
        assert proposal_body["status"] == "ready"
        proposal_id = proposal_body["proposalId"]

        confirmed = await client.call_tool(
            "save_search",
            {"params": {"proposalId": proposal_id, "confirmed": True}},
            meta={META_TOKEN_KEY: TOKEN_A},
        )
        save_body = _envelope(confirmed)["save_search"]

    assert save_body["status"] == "ok"
    assert save_body["name"] == "Del Mar Homes"
    assert len(fake.create_calls) == 1


async def test_create_with_daily_frequency_reports_daily_in_the_ok_envelope() -> None:
    """GS-8693/GS-8669: this is the create-path assertion that was structurally
    impossible before -- the fake used to hard-code (notify=False, scheduleId=None)
    for every create regardless of what was sent, so nothing here could ever catch
    a create-confirm envelope reporting the wrong frequency. Now the fake decodes
    scheduleInterval realistically (post-GS-8670 shape), so this pins the exact
    regression QA hit: confirm a Daily save and see "daily" back, not "never" or
    "unknown".
    """
    fake = _FakeGuestSite()
    daily_input = {**_VALID_PROPOSAL_INPUT, "notificationFrequency": "daily"}
    async with create_connected_server_and_client_session(_app(fake)) as client:
        proposed = await client.call_tool(
            "propose_saved_search", {"params": daily_input}, meta={META_TOKEN_KEY: TOKEN_A}
        )
        proposal_id = _envelope(proposed)["propose_saved_search"]["proposalId"]

        confirmed = await client.call_tool(
            "save_search",
            {"params": {"proposalId": proposal_id, "confirmed": True}},
            meta={META_TOKEN_KEY: TOKEN_A},
        )
        save_body = _envelope(confirmed)["save_search"]

    assert save_body["status"] == "ok"
    assert save_body["notificationFrequency"] == "daily"


async def test_walking_every_propose_branch_keeps_the_write_count_at_zero() -> None:
    fake = _FakeGuestSite()
    async with create_connected_server_and_client_session(_app(fake)) as client:
        # sign_in_required -- no meta at all
        anon = await client.call_tool("propose_saved_search", {"params": _VALID_PROPOSAL_INPUT})
        assert _envelope(anon)["propose_saved_search"]["status"] == "sign_in_required"

        # invalid frequency
        bad_freq = dict(_VALID_PROPOSAL_INPUT, notificationFrequency="weekly")
        result = await client.call_tool(
            "propose_saved_search", {"params": bad_freq}, meta={META_TOKEN_KEY: TOKEN_A}
        )
        assert _envelope(result)["propose_saved_search"]["status"] == "invalid"

        # a name collision after a first successful proposal+save
        first = await client.call_tool(
            "propose_saved_search", {"params": _VALID_PROPOSAL_INPUT}, meta={META_TOKEN_KEY: TOKEN_A}
        )
        proposal_id = _envelope(first)["propose_saved_search"]["proposalId"]
        await client.call_tool(
            "save_search",
            {"params": {"proposalId": proposal_id, "confirmed": True}},
            meta={META_TOKEN_KEY: TOKEN_A},
        )
        collision_input = dict(
            _VALID_PROPOSAL_INPUT,
            searchFilters={"city": "Malibu"},
            searchUrl="explore/listings/for-sale?city=Malibu",
        )
        collision = await client.call_tool(
            "propose_saved_search", {"params": collision_input}, meta={META_TOKEN_KEY: TOKEN_A}
        )
        assert _envelope(collision)["propose_saved_search"]["status"] == "name_exists"

    # Exactly the one save from the successful branch above -- every other
    # branch (sign_in_required, invalid, name_exists) wrote nothing.
    assert len(fake.create_calls) == 1


async def test_a_proposalid_from_one_user_is_refused_with_another_users_token() -> None:
    """🔴 The single most important assertion in the feature.

    Run CONCURRENTLY, both proposals pending at once, on ONE shared app --
    the in-process equivalent of forcing MCP_POOL_SIZE=1. Interleaved so the
    overlap is the entire point: A proposes, B proposes, B confirms, A
    confirms, and separately A's proposalId is tried against B's token.
    """
    fake = _FakeGuestSite()
    app = _app(fake)

    async with create_connected_server_and_client_session(app) as client:
        proposal_a_input = dict(
            _VALID_PROPOSAL_INPUT,
            name="A-Malibu",
            searchFilters={"city": "Malibu"},
            searchUrl="explore/listings/for-sale?city=Malibu",
        )
        proposal_b_input = dict(
            _VALID_PROPOSAL_INPUT,
            name="B-DelMar",
            searchFilters={"city": "Del Mar"},
            searchUrl="explore/listings/for-sale?city=Del%20Mar",
        )

        proposed_a, proposed_b = await asyncio.gather(
            client.call_tool(
                "propose_saved_search", {"params": proposal_a_input}, meta={META_TOKEN_KEY: TOKEN_A}
            ),
            client.call_tool(
                "propose_saved_search", {"params": proposal_b_input}, meta={META_TOKEN_KEY: TOKEN_B}
            ),
        )
        proposal_id_a = _envelope(proposed_a)["propose_saved_search"]["proposalId"]
        proposal_id_b = _envelope(proposed_b)["propose_saved_search"]["proposalId"]

        # Both pending at once -- confirm B first, then A, then attack.
        confirmed_b = await client.call_tool(
            "save_search",
            {"params": {"proposalId": proposal_id_b, "confirmed": True}},
            meta={META_TOKEN_KEY: TOKEN_B},
        )
        confirmed_a = await client.call_tool(
            "save_search",
            {"params": {"proposalId": proposal_id_a, "confirmed": True}},
            meta={META_TOKEN_KEY: TOKEN_A},
        )

        # The attack: A's proposalId, presented with B's token, AFTER A's own
        # confirm already consumed it -- so this also proves consumption
        # doesn't leak a "yes, but wrong action" style signal to another user.
        stolen = await client.call_tool(
            "save_search",
            {"params": {"proposalId": proposal_id_a, "confirmed": True}},
            meta={META_TOKEN_KEY: TOKEN_B},
        )

        # And the same attack BEFORE any confirmation ever happens, on a
        # fresh pair of proposals -- this is the real cross-user scenario.
        proposal_c_input = dict(
            _VALID_PROPOSAL_INPUT,
            name="A-SecondSearch",
            searchFilters={"city": "Santa Monica"},
            searchUrl="explore/listings/for-sale?city=Santa%20Monica",
        )
        proposed_c = await client.call_tool(
            "propose_saved_search", {"params": proposal_c_input}, meta={META_TOKEN_KEY: TOKEN_A}
        )
        proposal_id_c = _envelope(proposed_c)["propose_saved_search"]["proposalId"]
        stolen_unconsumed = await client.call_tool(
            "save_search",
            {"params": {"proposalId": proposal_id_c, "confirmed": True}},
            meta={META_TOKEN_KEY: TOKEN_B},
        )

        list_a = await client.call_tool("list_saved_searches", {}, meta={META_TOKEN_KEY: TOKEN_A})
        list_b = await client.call_tool("list_saved_searches", {}, meta={META_TOKEN_KEY: TOKEN_B})

    assert _envelope(confirmed_a)["save_search"]["status"] == "ok"
    assert _envelope(confirmed_b)["save_search"]["status"] == "ok"
    assert _envelope(stolen)["save_search"]["status"] == "proposal_expired"
    assert _envelope(stolen_unconsumed)["save_search"]["status"] == "proposal_expired"

    names_a = {r["name"] for r in _envelope(list_a)["list_saved_searches"]["savedSearches"]}
    names_b = {r["name"] for r in _envelope(list_b)["list_saved_searches"]["savedSearches"]}
    assert names_a == {"A-Malibu"}, "B's write, or the stolen write, leaked into A's account"
    assert names_b == {"B-DelMar"}, "A's write leaked into B's account"

    # A-SecondSearch was never confirmed by A (only B's forged attempt was
    # tried, and refused) -- it must not exist anywhere.
    assert "A-SecondSearch" not in names_a
    assert "A-SecondSearch" not in names_b


async def test_criteria_did_not_swap_between_concurrent_users() -> None:
    fake = _FakeGuestSite()
    app = _app(fake)

    async with create_connected_server_and_client_session(app) as client:
        proposal_a_input = dict(
            _VALID_PROPOSAL_INPUT,
            name="A-Malibu",
            searchFilters={"city": "Malibu"},
            searchUrl="explore/listings/for-sale?city=Malibu",
        )
        proposal_b_input = dict(
            _VALID_PROPOSAL_INPUT,
            name="B-DelMar",
            searchFilters={"city": "Del Mar"},
            searchUrl="explore/listings/for-sale?city=Del%20Mar",
        )
        proposed_a, proposed_b = await asyncio.gather(
            client.call_tool(
                "propose_saved_search", {"params": proposal_a_input}, meta={META_TOKEN_KEY: TOKEN_A}
            ),
            client.call_tool(
                "propose_saved_search", {"params": proposal_b_input}, meta={META_TOKEN_KEY: TOKEN_B}
            ),
        )
        proposal_id_a = _envelope(proposed_a)["propose_saved_search"]["proposalId"]
        proposal_id_b = _envelope(proposed_b)["propose_saved_search"]["proposalId"]

        await asyncio.gather(
            client.call_tool(
                "save_search",
                {"params": {"proposalId": proposal_id_b, "confirmed": True}},
                meta={META_TOKEN_KEY: TOKEN_B},
            ),
            client.call_tool(
                "save_search",
                {"params": {"proposalId": proposal_id_a, "confirmed": True}},
                meta={META_TOKEN_KEY: TOKEN_A},
            ),
        )

        list_a = await client.call_tool("list_saved_searches", {}, meta={META_TOKEN_KEY: TOKEN_A})
        list_b = await client.call_tool("list_saved_searches", {}, meta={META_TOKEN_KEY: TOKEN_B})

    record_a = _envelope(list_a)["list_saved_searches"]["savedSearches"][0]
    record_b = _envelope(list_b)["list_saved_searches"]["savedSearches"][0]
    assert record_a["searchFilters"]["city"] == "Malibu"
    assert record_b["searchFilters"]["city"] == "Del Mar"
