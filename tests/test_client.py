"""Unit tests for SavedSearchClient, against REAL recorded bytes via httpx.MockTransport.

Level 2b per the build plan: "the wrapper against httpx.MockTransport serving
recorded fixtures." Every trap-specific test replays a real dev response rather
than a hand-written approximation of one.
"""

from __future__ import annotations

from collections.abc import Callable
import json

import httpx
import pytest

from _guestsite_fixtures import fixture_body, fixture_status
from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.errors import (
    SavedSearchApiError,
    SavedSearchInvalidRequestError,
    SavedSearchNameExistsError,
    SavedSearchUnexpectedResponseError,
    SavedSearchUpstreamError,
)

pytestmark = pytest.mark.unit

BASE_URL = "https://www.dev.themls.com/GuestSiteApi"
FAKE_TOKEN = "eyJhbGciOiJIUzI1NiJ9.fake-client-test-token.sig"


_DEFAULT_TEST_PAGE_SIZE = 200


def _client_with(
    handler: Callable[[httpx.Request], httpx.Response],
    *,
    page_size: int = _DEFAULT_TEST_PAGE_SIZE,
) -> SavedSearchClient:
    transport = httpx.MockTransport(handler)
    http_client = httpx.AsyncClient(transport=transport, base_url=BASE_URL)
    return SavedSearchClient(BASE_URL, http_client=http_client, page_size=page_size)


def _json_response(status_code: int, body: object) -> httpx.Response:
    if isinstance(body, str):
        # The real "EsQuery cannot be null" 400 body is a bare JSON string, so it
        # must be re-serialised (json.dumps) rather than sent as raw text -- sending
        # it as plain text would make httpx's .json() fail to parse it at all,
        # testing a shape the real API never actually returns.
        return httpx.Response(status_code, content=json.dumps(body))
    return httpx.Response(status_code, json=body)


def _replay(fixture_name: str) -> Callable[[httpx.Request], httpx.Response]:
    """A handler that always answers with one recorded fixture, regardless of request."""

    def handler(_request: httpx.Request) -> httpx.Response:
        return _json_response(fixture_status(fixture_name), fixture_body(fixture_name))

    return handler


async def test_list_returns_decoded_records_from_a_real_response() -> None:
    client = _client_with(_replay("list_baseline"))
    records = await client.list_saved_searches(FAKE_TOKEN)
    expected_count = len(fixture_body("list_baseline"))
    assert len(records) == expected_count
    assert all(isinstance(r.search_filters, dict) for r in records)


async def test_list_sends_the_bearer_token() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["authorization"] = request.headers.get("authorization", "")
        return _json_response(200, fixture_body("list_baseline"))

    client = _client_with(handler)
    await client.list_saved_searches(FAKE_TOKEN)
    assert seen["authorization"] == f"Bearer {FAKE_TOKEN}"


async def test_list_always_requests_an_explicit_page_size_above_the_service_default() -> None:
    """🔴 Trap 5: PageSize defaults to 15 and silently truncates.

    This does not prove multi-page walking (see the fixtures' own README for why
    that cannot be proven against this account) -- it proves the client never
    relies on the service's own default, which is the part that is fully within
    this client's control.
    """
    seen_page_sizes: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen_page_sizes.append(request.url.params.get("PageSize"))
        return _json_response(200, fixture_body("list_baseline"))

    client = _client_with(handler)
    await client.list_saved_searches(FAKE_TOKEN)
    assert seen_page_sizes == ["200"]
    assert all(size is not None and int(size) > 15 for size in seen_page_sizes)


async def test_list_walks_a_second_page_when_the_first_is_full() -> None:
    """The exhaustion signal is "fewer than PageSize records came back" -- there is
    no total-count field to rely on instead. Faked here because the real account
    used to build the other fixtures has too few records to exercise this at all;
    the plan itself specifies exactly this kind of fake for this exact test.
    """
    one_record = fixture_body("list_baseline")[0]
    page_size = 3
    pages = [[one_record] * page_size, [one_record] * page_size, [one_record] * 1]
    calls: list[str | None] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params.get("PageNumber"))
        page = pages[len(calls) - 1]
        return _json_response(200, page)

    client = _client_with(handler, page_size=page_size)
    records = await client.list_saved_searches(FAKE_TOKEN)

    assert len(records) == page_size + page_size + 1
    assert calls == [None, "2", "3"]


async def test_list_raises_on_a_non_list_body() -> None:
    """The verified real shape is a bare array; a caller must never see anything
    else pass silently through as "zero records".
    """
    client = _client_with(lambda _r: httpx.Response(200, json={"savedSearches": []}))
    with pytest.raises(SavedSearchUnexpectedResponseError, match="expected a JSON array"):
        await client.list_saved_searches(FAKE_TOKEN)


async def test_create_decodes_the_double_encoded_envelope() -> None:
    """🔴 Trap 1: savedSearch is a JSON string containing a JSON array."""
    client = _client_with(_replay("create_daily"))
    record = await client.create(
        FAKE_TOKEN,
        name="anything",
        search_filters={},
        search_url="anything",
        es_query="{}",
        notification_frequency="daily",
    )
    assert record.saved_search_id == 1089
    assert record.notification_frequency == "daily"


async def test_create_sends_the_mapped_schedule_interval() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _json_response(fixture_status("create_daily"), fixture_body("create_daily"))

    client = _client_with(handler)
    await client.create(
        FAKE_TOKEN,
        name="x",
        search_filters={"city": "Del Mar"},
        search_url="explore/listings/for-sale",
        es_query="{}",
        notification_frequency="daily",
    )
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["scheduleInterval"] == 1
    assert body["searchName"] == "x"
    assert body["esQuery"] == "{}"
    assert "savedSearchId" not in body, "create must never send an id -- that is what makes it a create"


async def test_update_sends_the_saved_search_id() -> None:
    seen: dict[str, object] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = json.loads(request.content)
        return _json_response(
            fixture_status("update_rename_omit_schedule"),
            fixture_body("update_rename_omit_schedule"),
        )

    client = _client_with(handler)
    await client.update(
        FAKE_TOKEN,
        saved_search_id=1089,
        name="renamed",
        search_filters={},
        search_url="explore/listings/saved-search/1089/for-sale",
        es_query="{}",
        notification_frequency="never",
    )
    body = seen["body"]
    assert isinstance(body, dict)
    assert body["savedSearchId"] == 1089


async def test_create_raises_name_exists_on_the_real_400_shape() -> None:
    """🔴 Fed the real body: {"status": "exists", "savedSearch": null}."""
    client = _client_with(_replay("create_duplicate_name_400"))
    with pytest.raises(SavedSearchNameExistsError):
        await client.create(
            FAKE_TOKEN,
            name="dup",
            search_filters={},
            search_url="x",
            es_query="{}",
            notification_frequency="never",
        )


async def test_create_raises_invalid_request_on_the_plain_string_400_shape() -> None:
    """🔴 Fed the real body: a bare JSON string, "EsQuery cannot be null".

    This is a different shape from the exists-400 above and must not be
    misclassified as one just because both are HTTP 400.
    """
    client = _client_with(_replay("create_missing_esquery"))
    with pytest.raises(SavedSearchInvalidRequestError, match="EsQuery cannot be null"):
        await client.create(
            FAKE_TOKEN,
            name="x",
            search_filters={},
            search_url="x",
            es_query="",
            notification_frequency="never",
        )


@pytest.mark.parametrize("status_code", [500, 502, 503])
async def test_5xx_becomes_upstream_error_never_a_false_success(status_code: int) -> None:
    client = _client_with(lambda _r: httpx.Response(status_code, text="upstream is down"))
    with pytest.raises(SavedSearchUpstreamError):
        await client.list_saved_searches(FAKE_TOKEN)


async def test_timeout_becomes_upstream_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.TimeoutException("timed out")

    client = _client_with(handler)
    with pytest.raises(SavedSearchUpstreamError, match="timed out"):
        await client.list_saved_searches(FAKE_TOKEN)


async def test_connection_error_becomes_upstream_error() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("connection refused")

    client = _client_with(handler)
    with pytest.raises(SavedSearchUpstreamError):
        await client.list_saved_searches(FAKE_TOKEN)


async def test_delete_uses_the_id_as_a_path_segment_not_a_query_param() -> None:
    """The querystring form returns 405 -- discovered by trying it, not documented."""
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["query"] = str(request.url.params)
        return httpx.Response(200, json={"status": "ok", "savedSearch": None})

    client = _client_with(handler)
    await client.delete(FAKE_TOKEN, 1089)
    assert seen["path"].endswith("/SavedSearches/1089")
    assert seen["query"] == ""


async def test_delete_raises_on_5xx() -> None:
    client = _client_with(lambda _r: httpx.Response(500, text="down"))
    with pytest.raises(SavedSearchUpstreamError):
        await client.delete(FAKE_TOKEN, 1)


async def test_out_of_range_frequency_never_reaches_the_http_layer() -> None:
    """🔴 Refuse before any HTTP call. Assert the fake transport saw zero requests --
    the point is that we refuse it, not that we send it and handle a reply.
    """
    calls = 0

    def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        return httpx.Response(200, json={"status": "ok", "savedSearch": "[]"})

    client = _client_with(handler)
    with pytest.raises(ValueError, match="unknown notification frequency"):
        await client.create(
            FAKE_TOKEN,
            name="x",
            search_filters={},
            search_url="x",
            es_query="{}",
            notification_frequency="weekly",
        )
    assert calls == 0


async def test_every_client_error_is_a_saved_search_api_error() -> None:
    """One base class every tool can catch, regardless of which specific failure."""
    client = _client_with(lambda _r: httpx.Response(500, text="down"))
    with pytest.raises(SavedSearchApiError):
        await client.list_saved_searches(FAKE_TOKEN)


async def test_aclose_delegates_to_the_underlying_http_client() -> None:
    """The client owns no state of its own to release, but must not leak the
    httpx connection pool it wraps.
    """
    client = _client_with(lambda _r: httpx.Response(200, json=[]))
    await client.aclose()  # must not raise


@pytest.mark.parametrize("status_code", [401, 403, 404, 422])
async def test_a_4xx_other_than_400_is_an_upstream_error(status_code: int) -> None:
    """Only 400 has documented, parseable shapes. Every other 4xx is unexpected
    enough that a tool should treat it the same as an outage rather than guess.
    """
    client = _client_with(lambda _r: httpx.Response(status_code, text="unexpected"))
    with pytest.raises(SavedSearchUpstreamError, match=str(status_code)):
        await client.list_saved_searches(FAKE_TOKEN)


async def test_400_with_a_non_json_body_is_invalid_request() -> None:
    """Neither of the two documented 400 shapes is valid HTML/plain-text -- if the
    service ever fronts an error page instead of its own JSON body, that must
    still surface as a request-level error, not crash the caller.
    """
    client = _client_with(lambda _r: httpx.Response(400, text="<html>Bad Request</html>"))
    with pytest.raises(SavedSearchInvalidRequestError, match="non-JSON body"):
        await client.list_saved_searches(FAKE_TOKEN)


async def test_400_with_an_unrecognised_json_shape_is_invalid_request() -> None:
    """A 400 body that is valid JSON but neither the exists-dict nor a plain
    string -- e.g. the service adds a third error shape later.
    """
    client = _client_with(lambda _r: httpx.Response(400, json={"unexpected": "shape"}))
    with pytest.raises(SavedSearchInvalidRequestError, match="unrecognised body shape"):
        await client.list_saved_searches(FAKE_TOKEN)


async def test_list_body_that_is_not_json_raises() -> None:
    client = _client_with(lambda _r: httpx.Response(200, text="not json at all"))
    with pytest.raises(SavedSearchUnexpectedResponseError, match="not JSON"):
        await client.list_saved_searches(FAKE_TOKEN)


async def test_save_response_that_is_not_json_raises() -> None:
    client = _client_with(lambda _r: httpx.Response(200, text="not json at all"))
    with pytest.raises(SavedSearchUnexpectedResponseError, match="not JSON"):
        await client.create(
            FAKE_TOKEN,
            name="x",
            search_filters={},
            search_url="x",
            es_query="{}",
            notification_frequency="never",
        )


async def test_save_response_missing_saved_search_key_raises() -> None:
    client = _client_with(lambda _r: httpx.Response(200, json={"status": "ok"}))
    with pytest.raises(SavedSearchUnexpectedResponseError, match="missing 'savedSearch'"):
        await client.create(
            FAKE_TOKEN,
            name="x",
            search_filters={},
            search_url="x",
            es_query="{}",
            notification_frequency="never",
        )


async def test_save_response_with_non_string_saved_search_raises() -> None:
    """The documented shape is a JSON STRING containing a JSON array -- if the
    service ever "fixes" the double-encoding, that is a contract change this
    client must notice, not quietly start handling as if nothing changed.
    """
    client = _client_with(
        lambda _r: httpx.Response(200, json={"status": "ok", "savedSearch": [{"already": "decoded"}]})
    )
    with pytest.raises(SavedSearchUnexpectedResponseError, match=r"expected .* to be a JSON string"):
        await client.create(
            FAKE_TOKEN,
            name="x",
            search_filters={},
            search_url="x",
            es_query="{}",
            notification_frequency="never",
        )


async def test_save_response_with_unparseable_saved_search_string_raises() -> None:
    client = _client_with(
        lambda _r: httpx.Response(200, json={"status": "ok", "savedSearch": "{not valid json"})
    )
    with pytest.raises(SavedSearchUnexpectedResponseError, match="not valid JSON"):
        await client.create(
            FAKE_TOKEN,
            name="x",
            search_filters={},
            search_url="x",
            es_query="{}",
            notification_frequency="never",
        )


async def test_save_response_with_empty_saved_search_array_raises() -> None:
    client = _client_with(lambda _r: httpx.Response(200, json={"status": "ok", "savedSearch": "[]"}))
    with pytest.raises(SavedSearchUnexpectedResponseError, match="expected a non-empty array"):
        await client.create(
            FAKE_TOKEN,
            name="x",
            search_filters={},
            search_url="x",
            es_query="{}",
            notification_frequency="never",
        )
