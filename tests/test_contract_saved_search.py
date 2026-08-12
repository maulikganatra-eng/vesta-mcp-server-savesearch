"""Level 3 contract tests: real HTTP, real dev GuestSite records, real deletes.

Marked `contract` -- never runs in CI or the git hooks (see
`pyproject.toml`'s `filterwarnings`/`markers` and `scripts/tasks.py`'s
`EXCLUDED_MARKERS`). Run deliberately:

    uv run --frozen python scripts/tasks.py test-contract

Requires `GUESTSITE_DEV_BEARER_TOKEN` in the environment -- the same real,
live dev token already used to record `tests/fixtures/guestsite_dev/*.json`
and verified reachable (HTTP 200) against
`https://www.dev.themls.com/GuestSiteApi/SavedSearches`. Every test here
skips cleanly if it is absent, rather than failing, since a machine without
that credential is an ordinary state (e.g. CI, or a fresh clone), not an error.

**Every record this file creates is deleted in a `finally` block**, including
on assertion failure, and every name carries the `zz-contract-` prefix so a
crashed run's leftovers are easy to find and hand-clean:

    curl -s -H "Authorization: Bearer $GUESTSITE_DEV_BEARER_TOKEN" \\
      "https://www.dev.themls.com/GuestSiteApi/SavedSearches?PageSize=200" \\
      | python3 -c "import json,sys; [print(r['savedSearchId'], r['searchName']) \\
      for r in json.load(sys.stdin) if r['searchName'].startswith('zz-contract-')]"

**This is a real, shared dev account.** Other engineers' own manual testing
and Krishna's S4/routing work may be creating and deleting records on it
concurrently. Assertions below are written to tolerate that -- e.g. checking
"my records are present" rather than "the account holds exactly N records" --
but a badly-timed run could still observe a transient duplicate name from
someone else's in-flight test. Re-run if that happens; it is not this
client's bug.

🔴 **The paging test carried real production risk, which is exactly why it
exists.** Before this test ran once for real, `client.py`'s
`_PAGE_NUMBER_PARAM = "PageNumber"` was an unverified guess -- had it been
wrong and the API silently ignored it (returning page 1 again for every
"page 2" request), `SavedSearchClient.list_saved_searches`'s
walk-until-short-page loop would never terminate. It has since passed against
a real >15-record response (see `client.py`'s own docstring for the
confirmation), but every paging call below still wraps the list in
`asyncio.wait_for` with a generous timeout: a future API change silently
breaking this parameter again should fail this test with a clear diagnostic,
not hang the process.
"""

from __future__ import annotations

import asyncio
import logging
import os
from typing import Any
from uuid import uuid4

import pytest

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.errors import SavedSearchNameExistsError
from vesta_saved_search.fingerprint import criteria_fingerprint
from vesta_saved_search.naming import find_by_fingerprint

pytestmark = pytest.mark.contract

logger = logging.getLogger(__name__)

BASE_URL = "https://www.dev.themls.com/GuestSiteApi"
TOKEN_ENV_VAR = "GUESTSITE_DEV_BEARER_TOKEN"

#: Every record this file creates carries this prefix -- see the module
#: docstring's cleanup one-liner for finding stragglers by hand.
NAME_PREFIX = "zz-contract-"

#: Comfortably over the API's own undocumented PageSize=15 default and over
#: the ">15" boundary the plan calls out, with margin for a couple of records
#: created/deleted concurrently by someone else during the run.
TARGET_TOTAL_RECORDS = 17

#: How long a single paged list() call may take before this test treats a
#: hang as a failure rather than waiting forever. See the module docstring's
#: paragraph on the PageNumber parameter risk this guards against.
PAGING_TIMEOUT_SECONDS = 30.0


def _require_token() -> str:
    token = os.environ.get(TOKEN_ENV_VAR)
    if not token:
        pytest.skip(f"{TOKEN_ENV_VAR} not set -- contract tests need a real GuestSite dev token")
    return token


def _unique_name(label: str) -> str:
    return f"{NAME_PREFIX}{label}-{uuid4().hex[:8]}"


def _filters_for(city: str) -> dict[str, Any]:
    # 🔴 SortBy is REQUIRED on create -- newly confirmed here. Omitting it
    # does not silently default (unlike the traps client.py documents); the
    # real API rejects the request outright with a 400 naming the field:
    # `{"errors": {"SearchFilters.SortBy": ["The SortBy field is required."]}}`.
    return {"city": city, "mode": "forSale", "status": "active,comingSoon", "SortBy": "new"}


def _url_for(city: str) -> str:
    encoded_city = city.replace(" ", "%20")
    return (
        f"explore/listings/for-sale?city={encoded_city}&mode=forSale&status=active,comingSoon&sortBy=new"
    )


async def _delete_quietly(client: SavedSearchClient, token: str, saved_search_id: int) -> None:
    """Best-effort cleanup: one record's delete failing must not prevent the
    others from being attempted, and must not mask the test's own assertion
    failure with an unrelated cleanup exception. Logged rather than silently
    swallowed, so a leaked record still leaves a trace in the test output."""
    try:
        await client.delete(token, saved_search_id)
    except Exception:
        logger.exception("cleanup_failed saved_search_id=%s", saved_search_id)


async def test_duplicate_name_backstop_returns_exists_and_writes_nothing() -> None:
    """🔴 The free backstop, against real dev: creating with an existing name
    returns HTTP 400 `{"status": "exists"}` and writes nothing -- keyed on
    name only, so a second, completely different set of criteria under the
    same name still collides."""
    token = _require_token()
    client = SavedSearchClient(BASE_URL)
    name = _unique_name("dupname")
    created_id: int | None = None

    try:
        created = await client.create(
            token,
            name=name,
            search_filters=_filters_for("Solana Beach"),
            search_url=_url_for("Solana Beach"),
            es_query='{"bool": {}}',
            notification_frequency="never",
        )
        created_id = created.saved_search_id

        with pytest.raises(SavedSearchNameExistsError):
            await client.create(
                token,
                name=name,
                search_filters=_filters_for("Rancho Santa Fe"),
                search_url=_url_for("Rancho Santa Fe"),
                es_query='{"bool": {}}',
                notification_frequency="never",
            )

        records = await asyncio.wait_for(
            client.list_saved_searches(token), timeout=PAGING_TIMEOUT_SECONDS
        )
        matches = [r for r in records if r.name == name]
        assert len(matches) == 1, "the rejected create still wrote a second record"
        assert matches[0].search_filters["city"] == "Solana Beach", (
            "the wrong record's criteria won -- the rejected create partially applied"
        )
    finally:
        if created_id is not None:
            await _delete_quietly(client, token, created_id)
        await client.aclose()


async def test_duplicate_criteria_under_a_different_name_creates_a_second_record() -> None:
    """🔴 Criteria duplicates are NOT enforced upstream, verified directly:
    a unique name with byte-identical criteria to an existing record still
    succeeds and produces a SECOND record. Requirement 4's fingerprint check
    is entirely this repo's own responsibility -- this test is what proves
    there is no free backstop to lean on for it, unlike the name check above.
    """
    token = _require_token()
    client = SavedSearchClient(BASE_URL)
    filters = _filters_for("Rancho Palos Verdes")
    url = _url_for("Rancho Palos Verdes")
    name_a = _unique_name("dupcriteria-a")
    name_b = _unique_name("dupcriteria-b")
    created_ids: list[int] = []

    try:
        record_a = await client.create(
            token,
            name=name_a,
            search_filters=filters,
            search_url=url,
            es_query='{"bool": {}}',
            notification_frequency="never",
        )
        created_ids.append(record_a.saved_search_id)

        record_b = await client.create(
            token,
            name=name_b,
            search_filters=filters,
            search_url=url,
            es_query='{"bool": {}}',
            notification_frequency="never",
        )
        created_ids.append(record_b.saved_search_id)

        assert record_a.saved_search_id != record_b.saved_search_id
    finally:
        for saved_search_id in created_ids:
            await _delete_quietly(client, token, saved_search_id)
        await client.aclose()


async def test_paging_walks_past_fifteen_records_and_finds_a_duplicate_on_a_later_page() -> None:
    """🔴 Settles `client.py`'s `⚠️ UNVERIFIED` `_PAGE_NUMBER_PARAM` guess
    against the real API, and reproduces the >15-record duplicate-detection
    case the plan calls out as impossible to catch with a small test account.

    A small `page_size` is forced on this client specifically so pagination
    is exercised even though the real default (200) would fetch everything
    in one page for any realistic account size -- this test is about proving
    the WALK works, not about the default being safe (that is already proven
    by `test_client.py`'s trap-5 unit test).
    """
    token = _require_token()
    # Real page size for the initial, safe count -- large enough that this
    # call cannot itself trigger the untested walk.
    probe_client = SavedSearchClient(BASE_URL)
    small_page_client = SavedSearchClient(BASE_URL, page_size=5)

    created_ids: list[int] = []
    target_name = _unique_name("page-target")
    target_filters = _filters_for("Rancho Mirage")

    try:
        baseline = await asyncio.wait_for(
            probe_client.list_saved_searches(token), timeout=PAGING_TIMEOUT_SECONDS
        )
        # Reserve one padding slot short of the target so the loop below
        # creates the target record itself, not an extra padding record.
        padding_needed = max(0, TARGET_TOTAL_RECORDS - len(baseline) - 1)

        for i in range(padding_needed):
            padding_name = _unique_name(f"page-padding-{i}")
            record = await probe_client.create(
                token,
                name=padding_name,
                search_filters=_filters_for(f"Padding City {i}"),
                search_url=_url_for(f"Padding City {i}"),
                es_query='{"bool": {}}',
                notification_frequency="never",
            )
            created_ids.append(record.saved_search_id)

        target = await probe_client.create(
            token,
            name=target_name,
            search_filters=target_filters,
            search_url=_url_for("Rancho Mirage"),
            es_query='{"bool": {}}',
            notification_frequency="never",
        )
        created_ids.append(target.saved_search_id)

        try:
            full_list = await asyncio.wait_for(
                small_page_client.list_saved_searches(token), timeout=PAGING_TIMEOUT_SECONDS
            )
        except TimeoutError:
            pytest.fail(
                "list_saved_searches did not terminate within "
                f"{PAGING_TIMEOUT_SECONDS}s with page_size=5 -- this strongly suggests "
                "client.py's _PAGE_NUMBER_PARAM guess ('PageNumber') is wrong and the "
                "API is silently re-serving page 1 forever. Fix that constant before "
                "this client's paging path is trusted against a real >15-record account."
            )

        # No duplicate ids -- the symptom of a paging parameter that is
        # silently ignored and re-serves the same page.
        ids = [r.saved_search_id for r in full_list]
        assert len(ids) == len(set(ids)), (
            "duplicate savedSearchId across pages -- the paging parameter is likely "
            "being ignored and the same page is being re-served"
        )

        # Every record this test created is present in the fully-paged result.
        created_id_set = set(created_ids)
        found_ids = {r.saved_search_id for r in full_list if r.saved_search_id in created_id_set}
        assert found_ids == created_id_set, (
            "not every created record was returned by the paged walk -- "
            f"missing {created_id_set - found_ids}"
        )

        assert len(full_list) > 15, "did not actually cross the >15 boundary this test exists for"

        # 🔴 The duplicate-on-page-two case itself: fingerprint the target's
        # criteria and confirm it is found via the SAME fully-paged list a
        # real propose_saved_search call would use -- regardless of which
        # page the target actually landed on.
        target_fingerprint = criteria_fingerprint(target_filters, "forSale")
        found = find_by_fingerprint(full_list, target_fingerprint)
        assert found is not None
        assert found.saved_search_id == target.saved_search_id
    finally:
        for saved_search_id in created_ids:
            await _delete_quietly(probe_client, token, saved_search_id)
        await probe_client.aclose()
        await small_page_client.aclose()
