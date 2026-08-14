"""Unit tests for `update_saved_search`'s propose path (steps N7 + N8 /
VA-404 + VA-405), against a mocked client.

This dedicated tool replaced `propose_saved_search`'s old
`intent="update_existing"` branch -- the underlying logic (now
`_propose_update` inside tools.py) is the same, just moved to its own tool
with its own params model.

Every test here also asserts `client.create`/`client.update` were never
called -- propose writes nothing upstream, for updates just as much as for
creates.
"""

from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock

from mcp.server.fastmcp import FastMCP
import pytest

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.errors import SavedSearchUpstreamError
from vesta_saved_search.fingerprint import criteria_fingerprint
from vesta_saved_search.models import SavedSearchRecord
from vesta_saved_search.proposals import ProposalStore, user_key_from_token
from vesta_saved_search.tools import UPDATE_SAVED_SEARCH_KEY, UpdateSavedSearchParams, register_tools

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


def _params(**overrides: Any) -> UpdateSavedSearchParams:
    defaults: dict[str, Any] = {
        "savedSearchId": 42,
        "name": "Del Mar Homes",
        "nameWasGenerated": False,
        "searchFilters": {"city": "Del Mar"},
        "esQuery": '{"bool": {}}',
        "searchUrl": "explore/listings/saved-search/42/for-sale?city=Del%20Mar",
        "searchMode": "forSale",
        "criteriaSummary": "Del Mar, for sale",
        "unsupportedFilters": [],
        "notificationFrequency": "daily",
    }
    defaults.update(overrides)
    return UpdateSavedSearchParams(**defaults)


def _app_with(client: SavedSearchClient, store: ProposalStore) -> FastMCP:
    app = FastMCP("test")
    register_tools(app, client, proposal_store=store)
    return app


async def _propose(app: FastMCP, params: UpdateSavedSearchParams, ctx: Any) -> dict[str, Any]:
    tool = app._tool_manager.get_tool("update_saved_search")
    assert tool is not None
    result: dict[str, Any] = await tool.fn(params, ctx)
    return result


def _client(existing: list[SavedSearchRecord]) -> AsyncMock:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.return_value = existing
    return client


async def test_out_of_range_frequency_never_reaches_the_http_layer() -> None:
    client = _client([])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(notificationFrequency="weekly"), _FakeCtx(TOKEN))

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "invalid"
    client.list_saved_searches.assert_not_called()


async def test_client_error_becomes_an_error_envelope() -> None:
    client = AsyncMock(spec=SavedSearchClient)
    client.list_saved_searches.side_effect = SavedSearchUpstreamError("down")
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(), _FakeCtx(TOKEN))

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "error"


async def test_unknown_saved_search_id_is_invalid() -> None:
    client = _client([])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(savedSearchId=999), _FakeCtx(TOKEN))

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "invalid"
    client.create.assert_not_called()
    client.update.assert_not_called()


async def test_rename_only_is_ready_and_writes_nothing() -> None:
    stored = _record()
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(name="New Name"), _FakeCtx(TOKEN))

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "ready"
    assert result[UPDATE_SAVED_SEARCH_KEY]["name"] == "New Name"
    client.create.assert_not_called()
    client.update.assert_not_called()


async def test_frequency_only_change_is_ready() -> None:
    stored = _record(notification_frequency="never")
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(notificationFrequency="daily"), _FakeCtx(TOKEN))

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "ready"
    assert result[UPDATE_SAVED_SEARCH_KEY]["notificationFrequency"] == "daily"


async def test_rename_colliding_with_another_record_returns_name_exists() -> None:
    stored = _record(saved_search_id=42, name="Del Mar Homes")
    other = _record(saved_search_id=7, name="Malibu Homes", search_filters={"city": "Malibu"})
    client = _client([stored, other])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(name="Malibu Homes"), _FakeCtx(TOKEN))

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "name_exists"


async def test_renaming_to_its_own_current_name_is_not_a_collision() -> None:
    """A record must never collide with itself when nothing about the name
    changed -- it must resolve as a genuine no-op (`no_change`), never the
    false `name_exists` a naive "does this name exist anywhere" check
    against the full list (including the record's own current name) would
    produce."""
    stored = _record(saved_search_id=42, name="Del Mar Homes")
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(name="Del Mar Homes"), _FakeCtx(TOKEN))

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "no_change"


async def test_criteria_change_duplicating_another_record_returns_criteria_already_saved() -> None:
    stored = _record(saved_search_id=42, search_filters={"city": "Del Mar"})
    other = _record(saved_search_id=7, name="Malibu Homes", search_filters={"city": "Malibu"})
    client = _client([stored, other])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        _params(
            searchFilters={"city": "Malibu"},
            searchUrl="explore/listings/saved-search/42/for-sale?city=Malibu",
        ),
        _FakeCtx(TOKEN),
    )

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "criteria_already_saved"
    assert result[UPDATE_SAVED_SEARCH_KEY]["existingName"] == "Malibu Homes"


async def test_criteria_change_url_inconsistent_with_filters_is_invalid() -> None:
    stored = _record(saved_search_id=42)
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        _params(
            searchFilters={"area": "17"},
            searchUrl="explore/listings/saved-search/42/for-sale?area=11",
        ),
        _FakeCtx(TOKEN),
    )

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "invalid"


async def test_criteria_change_stashes_a_ready_update_proposal() -> None:
    stored = _record(saved_search_id=42)
    client = _client([stored])
    store = ProposalStore()
    app = _app_with(client, store)

    result = await _propose(
        app,
        _params(
            searchFilters={"city": "Malibu"},
            searchUrl="explore/listings/saved-search/42/for-sale?city=Malibu",
        ),
        _FakeCtx(TOKEN),
    )

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "ready"
    proposal = store.get(result[UPDATE_SAVED_SEARCH_KEY]["proposalId"], user_key_from_token(TOKEN))
    assert proposal is not None
    assert proposal.action == "update"
    assert proposal.payload["saved_search_id"] == 42
    assert proposal.payload["change"]["search_filters"] == {"city": "Malibu"}


async def test_create_shaped_url_from_the_model_cannot_corrupt_the_stored_path() -> None:
    """🔴 Structural proof of the URL-format precondition: even if the model
    supplies a plain CREATE-shaped URL (no saved-search/<id>/ path) for an
    update, only its QUERY STRING is ever extracted -- the stashed proposal
    carries no path at all for apply_update to misuse."""
    stored = _record(
        saved_search_id=42, search_url="explore/listings/saved-search/42/for-sale?city=Del%20Mar"
    )
    client = _client([stored])
    store = ProposalStore()
    app = _app_with(client, store)

    result = await _propose(
        app,
        _params(
            searchFilters={"city": "Malibu"},
            # A create-shaped URL -- no saved-search/<id>/ path segment.
            searchUrl="explore/listings/for-sale?city=Malibu",
        ),
        _FakeCtx(TOKEN),
    )

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "ready"
    proposal = store.get(result[UPDATE_SAVED_SEARCH_KEY]["proposalId"], user_key_from_token(TOKEN))
    assert proposal is not None
    assert proposal.payload["change"]["new_search_url_query"] == "city=Malibu"
    assert "new_search_url_query" in proposal.payload["change"]
    # No key in the stashed change carries a full URL/path at all.
    assert all(
        "saved-search" not in str(v) for v in proposal.payload["change"].values() if v is not None
    )


async def test_generated_name_collision_on_rename_never_asks_the_user() -> None:
    """🔴 Same 2x2 as the create path, now proven for renames too: a
    GENERATED name colliding on rename must silently regenerate/fall back,
    never surface name_exists the way a user-stated collision does."""
    stored = _record(saved_search_id=42, name="Del Mar Homes")
    other = _record(saved_search_id=7, name="Malibu Homes", search_filters={"city": "Malibu"})
    client = _client([stored, other])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(name="Malibu Homes", nameWasGenerated=True), _FakeCtx(TOKEN))

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "name_needs_regeneration"
    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] != "name_exists"
    client.update.assert_not_called()


async def test_generated_name_collision_on_rename_falls_back_after_two_attempts() -> None:
    stored = _record(saved_search_id=42, name="Del Mar Homes")
    other = _record(saved_search_id=7, name="Malibu Homes", search_filters={"city": "Malibu"})
    client = _client([stored, other])
    store = ProposalStore()
    app = _app_with(client, store)

    first = await _propose(app, _params(name="Malibu Homes", nameWasGenerated=True), _FakeCtx(TOKEN))
    assert first[UPDATE_SAVED_SEARCH_KEY]["status"] == "name_needs_regeneration"

    second = await _propose(app, _params(name="Malibu Homes", nameWasGenerated=True), _FakeCtx(TOKEN))

    assert second[UPDATE_SAVED_SEARCH_KEY]["status"] == "ready"
    assert second[UPDATE_SAVED_SEARCH_KEY]["name"] != "Malibu Homes"


async def test_user_stated_over_length_name_on_rename_is_invalid_not_regenerated() -> None:
    stored = _record(saved_search_id=42)
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(app, _params(name="X" * 100, nameWasGenerated=False), _FakeCtx(TOKEN))

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "invalid"
    client.update.assert_not_called()


async def test_generated_over_length_name_on_rename_regenerates_then_falls_back() -> None:
    stored = _record(saved_search_id=42)
    client = _client([stored])
    store = ProposalStore()
    app = _app_with(client, store)

    first = await _propose(app, _params(name="X" * 100, nameWasGenerated=True), _FakeCtx(TOKEN))
    assert first[UPDATE_SAVED_SEARCH_KEY]["status"] == "name_needs_regeneration"

    second = await _propose(app, _params(name="Y" * 100, nameWasGenerated=True), _FakeCtx(TOKEN))
    assert second[UPDATE_SAVED_SEARCH_KEY]["status"] == "ready"
    assert len(second[UPDATE_SAVED_SEARCH_KEY]["name"]) <= 60


async def test_resending_display_cased_frequency_during_a_pure_rename_is_not_a_change() -> None:
    """🔴 The update path's frequency-changed check must be casefolded too --
    otherwise resending "Daily" (display casing) during a pure rename would
    register a phantom frequency change and bypass the exact casefold fix
    this PR adds to frequency_to_schedule_interval."""
    stored = _record(saved_search_id=42, notification_frequency="daily")
    client = _client([stored])
    store = ProposalStore()
    app = _app_with(client, store)

    result = await _propose(
        app, _params(name="New Name", notificationFrequency="Daily"), _FakeCtx(TOKEN)
    )

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "ready"
    proposal = store.get(result[UPDATE_SAVED_SEARCH_KEY]["proposalId"], user_key_from_token(TOKEN))
    assert proposal is not None
    assert "notification_frequency" not in proposal.payload["change"]
    assert proposal.payload["change"]["name"] == "New Name"


async def test_unmappable_search_mode_does_not_block_a_criteria_change() -> None:
    """The unmappable-searchType guard only applies when criteria is
    unchanged -- a criteria change supplies its own fresh esQuery and never
    touches search_mode_for_es_query at all."""
    stored = _record(saved_search_id=42, search_mode="commercial")
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        _params(
            searchFilters={"city": "Malibu"},
            searchUrl="explore/listings/saved-search/42/for-sale?city=Malibu",
        ),
        _FakeCtx(TOKEN),
    )

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "ready"


async def test_rename_and_criteria_change_together_is_rejected_by_diff() -> None:
    """🔴 The one-change-per-call rule the caller of this tool must obey,
    enforced as a DIFF against the stored record, not a field-presence check
    -- `_params()`'s defaults always resend the full bundle, so this test
    proves the guard fires only when BOTH the name and the criteria have
    genuinely changed relative to what's stored, not merely because both
    fields happen to be present on the call."""
    stored = _record(saved_search_id=42, name="Del Mar Homes", search_filters={"city": "Del Mar"})
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        _params(
            name="New Name",
            searchFilters={"city": "Malibu"},
            searchUrl="explore/listings/saved-search/42/for-sale?city=Malibu",
        ),
        _FakeCtx(TOKEN),
    )

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "invalid"
    assert "one at a time" in result[UPDATE_SAVED_SEARCH_KEY]["message"]
    client.update.assert_not_called()


async def test_resending_unchanged_criteria_alongside_a_real_rename_is_allowed() -> None:
    """The other half of the diff-based guard: resending the SAME, unchanged
    searchFilters alongside a genuine rename must NOT trip the
    one-change-per-call rule -- only a call where criteria actually differs
    counts as a criteria change."""
    stored = _record(saved_search_id=42, name="Del Mar Homes", search_filters={"city": "Del Mar"})
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        _params(name="New Name", searchFilters={"city": "Del Mar"}),
        _FakeCtx(TOKEN),
    )

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "ready"


async def test_exactly_one_shape_rejects_both_and_neither() -> None:
    """Same "exactly one of propose or confirm" shape `DeleteSavedSearchParams`
    enforces -- providing both `savedSearchId` and `proposalId`, or neither,
    is rejected at the params layer."""
    with pytest.raises(ValueError, match="provide exactly one of"):
        UpdateSavedSearchParams(savedSearchId=42, proposalId="abc", confirmed=True)
    with pytest.raises(ValueError, match="provide exactly one of"):
        UpdateSavedSearchParams()


async def test_confirm_shape_rejects_a_resent_propose_only_field() -> None:
    """🔴 Unlike `SaveSearchParams` (deliberately JUST `{proposalId,
    confirmed}`), this model merges propose and confirm into one shape with
    every propose field optional -- without this check, a caller changing
    their mind between propose and confirm turns (e.g. resending
    `notificationFrequency` on the confirm call) would have that field
    silently ignored: `update_saved_search`'s confirm branch reads only
    `proposalId`/`confirmed`, so the write would carry the STALE, originally
    proposed value with no error telling the caller their new value never
    took effect."""
    with pytest.raises(ValueError, match="must not be resent here"):
        UpdateSavedSearchParams(proposalId="abc", confirmed=True, notificationFrequency="daily")
    with pytest.raises(ValueError, match="must not be resent here"):
        UpdateSavedSearchParams(proposalId="abc", confirmed=True, name="New Name")
    with pytest.raises(ValueError, match="must not be resent here"):
        UpdateSavedSearchParams(proposalId="abc", confirmed=True, unsupportedFilters=["home theater"])


async def test_confirm_shape_accepts_bare_proposal_id_and_confirmed() -> None:
    """The unaffected, actually-supported confirm shape must still validate cleanly."""
    UpdateSavedSearchParams(proposalId="abc", confirmed=True)
    UpdateSavedSearchParams(proposalId="abc", confirmed=False)


async def test_propose_requires_at_least_one_change() -> None:
    """The params validator's presence check: savedSearchId alone, with no
    name/criteria/frequency at all, is rejected before any HTTP call."""
    client = _client([])
    with pytest.raises(ValueError, match="at least one change"):
        UpdateSavedSearchParams(savedSearchId=42)
    client.list_saved_searches.assert_not_called()


async def test_criteria_bundle_requires_esquery_url_and_mode_together() -> None:
    """searchFilters alone, without esQuery/searchUrl/searchMode, is rejected
    at the params-validation layer -- never reaches `_propose_update` at all."""
    with pytest.raises(ValueError, match="searchFilters, esQuery, searchUrl and searchMode"):
        UpdateSavedSearchParams(savedSearchId=42, searchFilters={"city": "Malibu"})


async def test_criteria_summary_required_whenever_name_or_criteria_present() -> None:
    """A rename with no `criteriaSummary` is rejected at the params layer --
    `_settle_generated_name`'s deterministic fallback needs it regardless of
    whether criteria itself changed."""
    with pytest.raises(ValueError, match="criteriaSummary is required"):
        UpdateSavedSearchParams(savedSearchId=42, name="New Name")


async def test_propose_omits_notification_frequency_from_response_when_unset() -> None:
    """A rename with no notificationFrequency at all must not fabricate one
    in the `ready` response."""
    stored = _record(saved_search_id=42, name="Del Mar Homes")
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        UpdateSavedSearchParams(
            savedSearchId=42,
            name="New Name",
            nameWasGenerated=False,
            criteriaSummary="Del Mar, for sale",
        ),
        _FakeCtx(TOKEN),
    )

    body = result[UPDATE_SAVED_SEARCH_KEY]
    assert body["status"] == "ready"
    assert "notificationFrequency" not in body


async def test_unknown_stored_frequency_is_invalid_not_a_crash_when_carried_forward() -> None:
    """🔴 A record whose stored `notification_frequency` is the reachable
    `"unknown"` state (no `(notify, scheduleId)` pair this server
    recognises) must never reach `apply_update` -> `client.update` ->
    `frequency_to_schedule_interval("unknown")` uncaught -- that raises a
    bare `ValueError`, not a `SavedSearchApiError`, which would otherwise
    crash this tool call instead of answering `invalid`. Renaming without
    resending `notificationFrequency` carries the stored value forward."""
    stored = _record(saved_search_id=42, name="Del Mar Homes", notification_frequency="unknown")
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        UpdateSavedSearchParams(
            savedSearchId=42,
            name="New Name",
            nameWasGenerated=False,
            criteriaSummary="Del Mar, for sale",
        ),
        _FakeCtx(TOKEN),
    )

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "invalid"
    client.update.assert_not_called()


async def test_unknown_stored_frequency_does_not_block_a_genuine_no_change() -> None:
    """🔴 The stored-frequency guard (tools.py's `if not frequency_changed:`
    block) must never even run for a genuine no-op -- it only exists to
    catch what would otherwise be SENT to `apply_update`, and a no-op sends
    nothing. This must NOT be exercised by requesting `notificationFrequency
    ="unknown"` -- that trips the separate, cheap FORMAT-validity guard on
    the REQUESTED value, at the very top of `_propose_update`, before the
    record is even fetched, which would pass this test for the wrong reason
    even if the `no_change` short-circuit were ever reordered after the
    stored-frequency guard. Instead: the call resends the record's own
    unchanged criteria bundle (satisfying "at least one change" via
    `searchFilters`) and never mentions `notificationFrequency` at all, so
    the ONLY way this can resolve to `no_change` is via the diff against the
    stored record being genuinely empty -- which is exactly the path that
    would otherwise hit the corrupted `"unknown"` stored frequency."""
    stored = _record(
        saved_search_id=42,
        name="Del Mar Homes",
        search_filters={"city": "Del Mar"},
        search_mode="forSale",
        notification_frequency="unknown",
    )
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        UpdateSavedSearchParams(
            savedSearchId=42,
            searchFilters={"city": "Del Mar"},
            esQuery='{"bool": {}}',
            searchUrl="explore/listings/saved-search/42/for-sale?city=Del%20Mar",
            searchMode="forSale",
            criteriaSummary="Del Mar, for sale",
        ),
        _FakeCtx(TOKEN),
    )

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "no_change"
    client.update.assert_not_called()


async def test_unknown_stored_frequency_explicitly_fixed_by_the_call_succeeds() -> None:
    """Supplying a real `notificationFrequency` on the same call that fixes
    an `"unknown"` stored value must succeed -- the guard only fires for the
    UNCHANGED, carried-forward value, never for one the caller is actively
    replacing."""
    stored = _record(saved_search_id=42, name="Del Mar Homes", notification_frequency="unknown")
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        UpdateSavedSearchParams(savedSearchId=42, notificationFrequency="daily"),
        _FakeCtx(TOKEN),
    )

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "ready"


async def test_rename_and_criteria_together_rejected_before_duplicate_check() -> None:
    """The rename+criteria exclusivity guard must fire BEFORE the
    criteria-duplicate check -- combining both in one call where the new
    criteria also happens to collide with another of the caller's own
    searches must report the combination problem, not `criteria_already_saved`,
    so the caller isn't misled into thinking fixing the duplicate alone would
    let the call through."""
    stored = _record(saved_search_id=42, name="Del Mar Homes", search_filters={"city": "Del Mar"})
    other = _record(
        saved_search_id=7,
        name="Malibu Homes",
        search_filters={"city": "Malibu"},
        search_url="explore/listings/saved-search/7/for-sale?city=Malibu",
    )
    client = _client([stored, other])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        _params(
            name="New Name",
            searchFilters={"city": "Malibu"},
            searchUrl="explore/listings/saved-search/42/for-sale?city=Malibu",
        ),
        _FakeCtx(TOKEN),
    )

    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "invalid"
    assert "one at a time" in result[UPDATE_SAVED_SEARCH_KEY]["message"]


async def test_search_mode_alone_without_search_filters_is_rejected() -> None:
    """`searchMode` sent without `searchFilters` used to pass validation and
    then be silently ignored by `_propose_update` -- now the whole criteria
    bundle is all-or-nothing, symmetric with the existing "searchFilters
    requires the rest" check."""
    with pytest.raises(ValueError, match="searchFilters, esQuery, searchUrl and searchMode"):
        UpdateSavedSearchParams(
            savedSearchId=42,
            name="New Name",
            nameWasGenerated=False,
            searchMode="forRent",
            criteriaSummary="Del Mar, for sale",
        )


async def test_name_was_generated_required_whenever_name_is_set() -> None:
    """`nameWasGenerated` must be explicitly stated whenever `name` is --
    unlike a default of `False`, an omitted flag can no longer silently be
    read as "user-stated" (which would treat a collision on a generated
    rename as `name_exists` instead of transparently regenerating)."""
    with pytest.raises(ValueError, match="nameWasGenerated is required"):
        UpdateSavedSearchParams(savedSearchId=42, name="New Name", criteriaSummary="Del Mar, for sale")


async def test_no_change_response_includes_notification_frequency_when_requested() -> None:
    """Every other status branch echoes back `notificationFrequency` when the
    caller sent one -- `no_change` must too, so a caller confirming "it's
    already Daily" doesn't need to special-case this one status."""
    stored = _record(saved_search_id=42, notification_frequency="daily")
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        UpdateSavedSearchParams(savedSearchId=42, notificationFrequency="daily"),
        _FakeCtx(TOKEN),
    )

    body = result[UPDATE_SAVED_SEARCH_KEY]
    assert body["status"] == "no_change"
    assert body["notificationFrequency"] == "daily"


async def test_no_change_response_omits_notification_frequency_when_not_requested() -> None:
    """The other half of the `no_change` echo: a no-op driven purely by
    resending unchanged criteria, with no `notificationFrequency` on the
    call at all, must not fabricate one in the response."""
    stored = _record(saved_search_id=42, search_filters={"city": "Del Mar"}, search_mode="forSale")
    client = _client([stored])
    app = _app_with(client, ProposalStore())

    result = await _propose(
        app,
        UpdateSavedSearchParams(
            savedSearchId=42,
            searchFilters={"city": "Del Mar"},
            esQuery='{"bool": {}}',
            searchUrl="explore/listings/saved-search/42/for-sale?city=Del%20Mar",
            searchMode="forSale",
            criteriaSummary="Del Mar, for sale",
        ),
        _FakeCtx(TOKEN),
    )

    body = result[UPDATE_SAVED_SEARCH_KEY]
    assert body["status"] == "no_change"
    assert "notificationFrequency" not in body


async def test_deterministic_fallback_avoids_reproducing_the_current_name() -> None:
    """🔴 The deterministic fallback (after two regeneration failures) is
    built from `criteriaSummary`, which has no reason to know the record's
    OWN current name -- if it coincidentally reproduces it, the resulting
    `_settle_generated_name` call must still avoid it, via `also_avoid_name`,
    rather than silently collapsing an intended rename into a no-op."""
    # `fallback_name("Del Mar Homes", max_length=60)` deterministically
    # produces "Del Mar Homes Search" -- distinct from "Del Mar Homes" itself,
    # so this drives the fallback via a criteriaSummary chosen to reproduce
    # the CURRENT name after `dedupe_fallback_name` would otherwise accept it
    # outright, by pre-seeding a same-named collision record `dedupe_fallback_name`
    # must dodge into "(2)", then asserting that suffixed form still isn't
    # blocked by the current name. In other words: the current name behaves
    # as an implicit taken name whether or not any OTHER record has it.
    stored = _record(saved_search_id=42, name="Del Mar Homes Search")
    client = _client([stored])
    store = ProposalStore()
    user_key = user_key_from_token(TOKEN)
    fingerprint = criteria_fingerprint(stored.search_filters, stored.search_mode)
    store.note_naming_failure(user_key, fingerprint)
    store.note_naming_failure(user_key, fingerprint)
    app = _app_with(client, store)

    result = await _propose(
        app,
        _params(name="Some Generated Name", nameWasGenerated=True, criteriaSummary="Del Mar Homes"),
        _FakeCtx(TOKEN),
    )

    body = result[UPDATE_SAVED_SEARCH_KEY]
    assert body["status"] == "ready"
    assert body["name"] != stored.name


async def test_frequency_only_call_does_not_clear_an_in_progress_naming_negotiation() -> None:
    """🔴 `store.clear_naming_attempts` must only fire when THIS call actually
    settled a name -- a frequency-only (or criteria-only, no-rename) propose
    never makes a naming decision at all, and must not reset an unrelated,
    still-in-progress naming negotiation for the same fingerprint (e.g. an
    earlier rename attempt that already hit `name_needs_regeneration` once)
    just because the caller happened to change something else in between."""
    stored = _record(saved_search_id=42, notification_frequency="never")
    client = _client([stored])
    store = ProposalStore()
    user_key = user_key_from_token(TOKEN)
    fingerprint = criteria_fingerprint(stored.search_filters, stored.search_mode)
    assert store.note_naming_failure(user_key, fingerprint) == 1
    app = _app_with(client, store)

    result = await _propose(
        app,
        UpdateSavedSearchParams(savedSearchId=42, notificationFrequency="daily"),
        _FakeCtx(TOKEN),
    )
    assert result[UPDATE_SAVED_SEARCH_KEY]["status"] == "ready"

    # If the frequency-only call above had cleared the counter, this would
    # restart at 1 instead of continuing the same negotiation at 2.
    assert store.note_naming_failure(user_key, fingerprint) == 2
