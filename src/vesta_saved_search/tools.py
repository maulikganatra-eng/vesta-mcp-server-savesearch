"""MCP tools exposed by this server. First one: `list_saved_searches` (step N3).

Registering a tool is the job of :func:`register_tools`, called once from
:func:`vesta_saved_search.server.create_app`. Kept in its own module, separate
from `server.py`'s transport/health-route concerns, so a test can register these
tools on a bare `FastMCP` instance without going through the full app factory.
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any, Literal
from urllib.parse import urlsplit

from mcp.server.fastmcp import Context, FastMCP
from pydantic import BaseModel, Field, model_validator

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.config import PROPERTY_SEARCH_INTERNAL_URL, SAVED_SEARCH_NAME_MAX_LENGTH
from vesta_saved_search.errors import SavedSearchApiError, SavedSearchNameExistsError
from vesta_saved_search.es_query_client import EsQueryClient, search_mode_for_es_query
from vesta_saved_search.fingerprint import criteria_fingerprint
from vesta_saved_search.frequency import frequency_to_schedule_interval
from vesta_saved_search.identity import bearer_token_from_meta
from vesta_saved_search.models import SavedSearchRecord
from vesta_saved_search.naming import (
    dedupe_fallback_name,
    fallback_name,
    find_by_fingerprint,
    find_by_name,
    mentions_unsupported_filter,
    normalise_name,
    url_matches_filters,
)
from vesta_saved_search.proposals import Proposal, ProposalAction, ProposalStore, user_key_from_token
from vesta_saved_search.updates import UpdateChange, apply_update

#: Every tool's own name doubles as its response envelope's single top-level
#: key -- matching `vesta-mcp-server`'s convention ("each tool returns a
#: single-key envelope named after the tool", that repo's README) and, since
#: this rewrite, ENFORCED from the orchestrator side too:
#: `capability_executor.py` unwraps a response's single top-level dict-valued
#: key for its DATA, but always names `mode_key` after the calling tool, never
#: after that wrapping key. A server that reused one constant across every
#: tool (this module's own prior design) could no longer collide with itself
#: even if it tried -- but every branch below still keeps one tool per key,
#: because a self-describing key is still what a reader (or the response LLM)
#: sees, and because the exported names below are what tests import instead
#: of hardcoding the literal.
LIST_SAVED_SEARCHES_KEY = "list_saved_searches"
PROPOSE_SAVED_SEARCH_KEY = "propose_saved_search"
SAVE_SEARCH_KEY = "save_search"
UPDATE_SAVED_SEARCH_KEY = "update_saved_search"
DELETE_SAVED_SEARCH_KEY = "delete_saved_search"

#: Field names this tool emits on each record. Checked against
#: `contains_sensitive_substring` by `test_sensitive_keys.py` so a name added here
#: later is automatically covered by that check, not just the ones present today.
RECORD_FIELD_NAMES = (
    "savedSearchId",
    "name",
    "notificationFrequency",
    "searchMode",
    "searchFilters",
    "searchUrl",
    "newListingsCount",
    "createdAt",
    "lastUpdate",
)

#: Field names `propose_saved_search`, `save_search` and `update_saved_search`
#: emit, beyond the ones already covered by `RECORD_FIELD_NAMES`. Same reason
#: those are tracked: `test_sensitive_keys.py` loops over this tuple too, so a
#: name added here later that happens to contain a sensitive substring is
#: caught by that test rather than discovered as a field silently missing
#: from production.
PROPOSAL_FIELD_NAMES = (
    "status",
    "proposalId",
    "criteriaSummary",
    "unsupportedFilters",
    "previousName",
    "existingName",
    "takenNames",
    "reason",
    "message",
    "savedSearchId",
    "name",
    "notificationFrequency",
)


class ProposeSavedSearchParams(BaseModel):
    """Input to `propose_saved_search` (step N5 / VA-401). Writes nothing upstream.

    Create-only -- see `update_saved_search` for renaming, changing criteria,
    or changing notification frequency on an EXISTING saved search.

    `searchFilters`, `esQuery`, `searchUrl`, `searchMode`, `criteriaSummary`
    and `unsupportedFilters` match `build_saved_search_input`'s output shape
    in the property-search repo (see the synced fixture
    `tests/fixtures/shared/saved_search_input_for_sale.json`) — this tool
    does not invent its own shape for wire-level criteria. `name`,
    `nameWasGenerated` and `notificationFrequency` come from the model's own
    reading of the conversation, not from that fixture.
    """

    name: str
    nameWasGenerated: bool
    searchFilters: dict[str, Any]
    esQuery: str
    searchUrl: str
    searchMode: Literal["forSale", "forRent", "Sold"]
    criteriaSummary: str
    unsupportedFilters: list[str] = Field(default_factory=list)
    notificationFrequency: str
    #: A create is always a NEW search -- `auto` and `create_new` are
    #: functionally identical today. Kept (rather than collapsed to a bare
    #: bool) because `_settle_generated_name` still branches on it to choose
    #: between `name_exists` and `name_conflict_create_only` on a user-stated
    #: collision, and a caller stating its intent explicitly reads better
    #: than an unexplained default.
    intent: Literal["auto", "create_new"] = "auto"


class SaveSearchParams(BaseModel):
    """Input to `save_search` (step N6 / VA-402). Nothing else. Confirms a
    CREATE only -- see `update_saved_search` for confirming a rename,
    criteria change, or frequency change.

    See `vesta_saved_search.proposals`'s module docstring for why this is
    `{proposalId, confirmed}` and not a resend of the whole payload.
    """

    proposalId: str
    confirmed: bool


class UpdateSavedSearchParams(BaseModel):
    """Input to `update_saved_search` (steps N7 + N8 / VA-404 + VA-405): rename,
    change criteria, and/or change notification frequency on an EXISTING saved
    search. Exactly one of two shapes:

    * **Propose:** `savedSearchId` plus at least one change (a new `name`, a
      criteria bundle, and/or `notificationFrequency`). Writes nothing;
      returns a `proposalId` and a confirmation summary.
    * **Confirm:** `proposalId` + `confirmed`. The same shape `save_search`
      uses, deliberately.

    A rename and a criteria change may never be requested in the SAME call —
    see the validator below. `notificationFrequency` may ride along with
    either, since it does not change what the confirmation summary is about.

    A frequency-only change needs only `savedSearchId` + `notificationFrequency`
    — nothing else is required, which is what makes this tool also cover what
    used to be `update_saved_search_notifications`'s whole job.
    """

    savedSearchId: int | None = None
    name: str | None = None
    #: Required whenever `name` is set -- `None` (rather than a `False`
    #: default) so the validator below can tell "explicitly stated" apart
    #: from "omitted"; a default would let a forgotten flag on a rename to a
    #: generated name silently read as user-stated (see the validator).
    nameWasGenerated: bool | None = None
    searchFilters: dict[str, Any] | None = None
    esQuery: str | None = None
    searchUrl: str | None = None
    searchMode: Literal["forSale", "forRent", "Sold"] | None = None
    #: Required whenever `name` or `searchFilters` is set -- `_settle_generated_name`'s
    #: deterministic-fallback path needs a plain-language description of the
    #: criteria to build a fallback name from, regardless of whether criteria
    #: itself is what changed.
    criteriaSummary: str | None = None
    unsupportedFilters: list[str] = Field(default_factory=list)
    notificationFrequency: str | None = None

    proposalId: str | None = None
    confirmed: bool | None = None

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> UpdateSavedSearchParams:
        is_propose = self.savedSearchId is not None
        is_confirm = self.proposalId is not None
        if is_propose == is_confirm:
            raise ValueError(
                "provide exactly one of: savedSearchId (+ at least one change, to propose "
                "an update), or proposalId + confirmed (to confirm one already proposed)"
            )
        if is_propose:
            has_name = self.name is not None
            has_criteria = self.searchFilters is not None
            has_frequency = self.notificationFrequency is not None
            if not (has_name or has_criteria or has_frequency):
                raise ValueError(
                    "a propose call needs at least one change: name, a criteria bundle "
                    "(searchFilters + esQuery + searchUrl + searchMode + criteriaSummary), "
                    "or notificationFrequency"
                )
            # Deliberately NOT rejecting "both name and searchFilters present"
            # here. A caller may legitimately resend an unchanged field
            # alongside a real change (the create-path params model has
            # always worked this way) -- FIELD PRESENCE cannot tell "resent
            # unchanged" apart from "actually changing both", only a DIFF
            # against the stored record can. That diff-based rejection lives
            # in `_propose_update`, after the record is fetched, right after
            # `name_changed`/`criteria_changed` are computed.
            #
            # All-or-nothing across the whole criteria bundle, not just
            # "searchFilters implies the rest" -- `searchMode` (or `esQuery`
            # / `searchUrl`) sent alone, without `searchFilters`, used to
            # pass validation and then be silently ignored by `_propose_update`
            # (which only reads them inside the `searchFilters is not None`
            # branch), with no error telling the caller their mode change
            # never took effect.
            has_any_criteria_field = any(
                value is not None
                for value in (self.searchFilters, self.esQuery, self.searchUrl, self.searchMode)
            )
            if has_any_criteria_field and not (
                has_criteria
                and self.esQuery is not None
                and self.searchUrl is not None
                and self.searchMode is not None
            ):
                raise ValueError(
                    "a criteria change requires searchFilters, esQuery, searchUrl and "
                    "searchMode together"
                )
            if (has_name or has_criteria) and self.criteriaSummary is None:
                raise ValueError(
                    "criteriaSummary is required whenever name or searchFilters is set -- "
                    "needed for the deterministic fallback name if a generated name collides"
                )
            if has_name and self.nameWasGenerated is None:
                raise ValueError("nameWasGenerated is required whenever name is set")
        return self


class DeleteSavedSearchParams(BaseModel):
    """Input to `delete_saved_search` (step N9 / VA-406). Exactly one of two shapes:

    * **Propose:** `savedSearchId` alone. Writes nothing; returns a
      `proposalId` and the record's name for a confirmation prompt.
    * **Confirm:** `proposalId` + `confirmed`. The same shape `save_search`
      uses, deliberately -- a caller already following that pattern needs no
      new one here.

    Never both, never neither: see the validator below. One tool, two
    shapes, rather than a second top-level tool name — the plan defines the
    single name `delete_saved_search` and describes propose-then-confirm as
    this tool's own behaviour, not a pair of tools.
    """

    savedSearchId: int | None = None
    proposalId: str | None = None
    confirmed: bool | None = None

    @model_validator(mode="after")
    def _exactly_one_shape(self) -> DeleteSavedSearchParams:
        is_propose = self.savedSearchId is not None
        is_confirm = self.proposalId is not None
        if is_propose == is_confirm:
            raise ValueError(
                "provide exactly one of: savedSearchId (to propose a delete), or "
                "proposalId + confirmed (to confirm one already proposed)"
            )
        return self


def _record_to_dict(record: SavedSearchRecord) -> dict[str, Any]:
    """Shape one decoded record into the wire form `list_saved_searches` returns.

    `searchUrl` is never omitted, even if somehow empty — it IS the payload of the
    "load" operation (see the tool's own docstring), so its absence must be visible
    as an empty string, not a missing key a caller might code around.
    """
    return {
        "savedSearchId": record.saved_search_id,
        "name": record.name,
        "notificationFrequency": record.notification_frequency,
        "searchMode": record.search_mode,
        "searchFilters": record.search_filters,
        "searchUrl": record.search_url,
        "newListingsCount": record.new_listings_count,
        "createdAt": record.created_at,
        "lastUpdate": record.last_update,
    }


def _settle_generated_name(
    store: ProposalStore,
    user_key: str,
    fingerprint: str,
    *,
    candidate_name: str,
    name_was_generated: bool,
    criteria_summary: str,
    unsupported_filters: list[str],
    collision_records: list[SavedSearchRecord],
    all_records: list[SavedSearchRecord],
    user_stated_collision_status: str,
    also_avoid_name: str | None = None,
) -> dict[str, Any] | tuple[str, int]:
    """Resolve `candidate_name` to `(final_name, regeneration_attempts)`, or a
    bare status body the caller should envelope and return immediately.

    The name-collision / length / unsupported-filter / regeneration /
    deterministic-fallback state machine shared by `propose_saved_search`'s
    create path and `update_saved_search`'s rename path — kept in one place so
    the two cannot drift on it independently, and so a rename gets the exact
    same `mentions_unsupported_filter` protection a create already had (a
    generated rename can reference a filter the search does not actually
    apply, same as a generated create name could). Only what happens on a
    user-STATED collision differs between the two callers
    (`name_conflict_create_only` vs `name_exists`), supplied via
    `user_stated_collision_status`.

    Returns a BARE dict on early-return, deliberately -- each caller wraps it
    under its own tool's envelope key, since the two callers no longer share
    one.

    The deterministic fallback is re-checked against `collision_records`
    too (`dedupe_fallback_name`) — two structurally different searches can
    share the same `criteriaSummary`-derived fallback, and a collision
    caught here at propose time is cheaper than one first discovered at
    confirm time as `name_exists`.

    `also_avoid_name` (update path only): the record's OWN current name,
    which is not in `collision_records` (excluded there as "not a collision
    with a different search") but which the deterministic fallback must
    still not reproduce -- otherwise a rename that fell all the way through
    to the fallback could silently collapse into the record's existing
    name, contradicting the `name_changed` reasoning that got it here.
    """
    name_collision = find_by_name(collision_records, candidate_name)
    too_long = len(candidate_name) > SAVED_SEARCH_NAME_MAX_LENGTH

    if name_collision is not None:
        if not name_was_generated:
            return {"status": user_stated_collision_status, "existingName": name_collision.name}
        reason = "name_collision"
    elif too_long:
        if not name_was_generated:
            return {
                "status": "invalid",
                "message": f"name exceeds {SAVED_SEARCH_NAME_MAX_LENGTH} characters",
            }
        reason = "name_too_long"
    elif name_was_generated and mentions_unsupported_filter(candidate_name, unsupported_filters):
        reason = "mentions_unsupported_filter"
    else:
        return candidate_name, 0

    attempts = store.note_naming_failure(user_key, fingerprint)
    if attempts < 2:
        return {
            "status": "name_needs_regeneration",
            "reason": reason,
            "takenNames": [record.name for record in all_records],
        }

    fallback = fallback_name(
        criteria_summary,
        max_length=SAVED_SEARCH_NAME_MAX_LENGTH,
        unsupported_filters=unsupported_filters,
    )
    final_name = dedupe_fallback_name(
        fallback,
        collision_records,
        max_length=SAVED_SEARCH_NAME_MAX_LENGTH,
        avoid_names=(also_avoid_name,) if also_avoid_name is not None else (),
    )
    return final_name, attempts


def register_tools(
    app: FastMCP,
    client: SavedSearchClient,
    *,
    proposal_store: ProposalStore | None = None,
    es_query_client: EsQueryClient | None = None,
) -> None:
    """Register every tool this server exposes onto `app`.

    Takes the client as a parameter, rather than constructing one internally, so
    tests can register these tools against a client built on `httpx.MockTransport`
    without this module knowing tests exist — the same pattern
    `vesta_saved_search.server.create_app` uses for the whole app.

    `proposal_store` is accepted the same way: production leaves it `None` and
    gets a fresh `ProposalStore()` per app instance (one per server process,
    never a module-level global — see that class's docstring for why). Tests
    pass one explicitly to control its clock or TTL directly, e.g. to assert
    expiry without sleeping for real seconds.

    `es_query_client` (step N7 / VA-404) is accepted the same way again:
    production leaves it `None` and gets a real `EsQueryClient` pointed at
    :data:`vesta_saved_search.config.PROPERTY_SEARCH_INTERNAL_URL`. Tests pass
    one built on `httpx.MockTransport`.
    """
    store = proposal_store or ProposalStore()
    es_client = es_query_client or EsQueryClient(PROPERTY_SEARCH_INTERNAL_URL)

    async def _find_record_or_invalid(
        saved_search_id: int, token: str, *, envelope_key: str
    ) -> tuple[list[SavedSearchRecord], SavedSearchRecord] | dict[str, Any]:
        """Shared by every tool that needs "is this id one of the caller's own
        saved searches" -- `update_saved_search`'s propose path and
        `delete_saved_search`'s propose path both open with exactly this
        sequence. Returns `(all_records, the_matching_one)` on success, or an
        already-enveloped error/invalid body to return immediately.
        """
        try:
            existing = await client.list_saved_searches(token)
        except SavedSearchApiError as exc:
            return {envelope_key: {"status": "error", "message": str(exc)}}

        current = next((r for r in existing if r.saved_search_id == saved_search_id), None)
        if current is None:
            return {
                envelope_key: {
                    "status": "invalid",
                    "message": f"no saved search with id {saved_search_id}",
                }
            }
        return existing, current

    async def _propose_update(
        params: UpdateSavedSearchParams, token: str, *, envelope_key: str
    ) -> dict[str, Any]:
        """`update_saved_search`'s propose path (steps N7 + N8 / VA-404 + VA-405).

        Covers a rename, a criteria change, a frequency change, or a
        frequency change riding alongside either of the other two -- never a
        rename and a criteria change together, enforced by
        `UpdateSavedSearchParams`'s own validator before this function is
        even reached.

        Unlike a create proposal, the model may resend the record's OWN
        unchanged `searchFilters`/`searchUrl`/`esQuery` when only renaming or
        changing frequency — `apply_update` never trusts a caller-supplied
        `esQuery` for an unchanged-criteria update anyway (it always fetches
        a fresh one via S4), so a stale value here is harmless. In practice a
        pure rename/frequency call from this tool's params model does not
        carry those fields at all -- they are only set on a criteria change.

        🔴 The URL-format precondition is enforced STRUCTURALLY, not by
        inspecting `params.searchUrl`'s raw text: only its QUERY STRING is
        ever extracted (see `UpdateChange` below), and `apply_update` always
        re-attaches that to the record's own stored path. A create-shaped
        URL from the model therefore has no code path left that could ever
        send its path anywhere — there is nothing to assert against because
        there is nothing left to go wrong.

        Check ordering below is deliberate, in three passes:

        1. Cheap, params-only checks that need no HTTP call (frequency
           FORMAT only -- not yet whether it is safe to carry forward).
        2. Fetch the record, then compute what actually changed (name /
           criteria / frequency) as pure diffs against it -- no rejecting
           yet. The rename/criteria exclusivity guard runs FIRST among the
           diff-dependent checks, before either the criteria-duplicate/URL
           checks or name resolution, so a caller combining the two always
           hears about the combination, not whichever single-concern check
           happens to run first.
        3. Only once a real (non-empty) diff is confirmed -- i.e. AFTER the
           `no_change` short-circuit -- do the checks that exist purely to
           guard what is about to be SENT run: the stored `search_mode`'s
           mappability (when criteria is not what changed) and the stored
           `notification_frequency`'s validity (when frequency is not what
           changed, since a corrupted/unmappable `"unknown"` stored value
           would otherwise be silently carried into `apply_update` and
           crash there uncaught). A genuinely no-op call on a record with
           either kind of corrupted stored data must still resolve to
           `no_change`, never `invalid`.
        """
        key = envelope_key
        saved_search_id = params.savedSearchId
        if saved_search_id is None:  # pragma: no cover - unreachable, guarded by the model validator
            return {key: {"status": "invalid", "message": "savedSearchId is required"}}

        if params.notificationFrequency is not None:
            try:
                frequency_to_schedule_interval(params.notificationFrequency)
            except ValueError as exc:
                return {key: {"status": "invalid", "message": str(exc)}}

        found = await _find_record_or_invalid(saved_search_id, token, envelope_key=key)
        if isinstance(found, dict):
            return found
        existing, current = found

        others = [r for r in existing if r.saved_search_id != saved_search_id]
        user_key = user_key_from_token(token)

        # Bound to a local so the two fields stay narrowed together wherever
        # `criteria_changed` is later checked -- the params validator
        # guarantees `searchFilters`/`searchMode`/`searchUrl` all arrive
        # together, but mypy has no way to know that fact about a LATER,
        # separate `if criteria_changed:` block, only about code still
        # inside THIS `if`.
        search_filters = params.searchFilters
        current_fingerprint = criteria_fingerprint(current.search_filters, current.search_mode)
        criteria_changed = False
        new_fingerprint = current_fingerprint
        if search_filters is not None and params.searchMode is not None:
            new_fingerprint = criteria_fingerprint(search_filters, params.searchMode)
            criteria_changed = new_fingerprint != current_fingerprint

        # `candidate_name` (rather than testing `params.name` again below) is
        # what lets mypy narrow it to `str` inside the `if candidate_name is
        # not None:` block -- `name_changed` alone would be a `bool` with no
        # memory of which branch proved it, and this repo's lint config bans
        # `assert` in production code (S101; it is stripped by `-O`), so a
        # narrowing assert is not the way to bridge that gap here.
        candidate_name = params.name
        name_changed = candidate_name is not None and normalise_name(candidate_name) != normalise_name(
            current.name
        )
        # ⚠️ One change per call. `UpdateSavedSearchParams`'s validator does
        # NOT reject a call carrying both `name` and `searchFilters` present
        # -- field PRESENCE can't tell "resent unchanged" apart from
        # "actually changing both", only a DIFF against the stored record
        # can. This is that diff-based guard: it can only ever fire if BOTH
        # turn out, after comparing against the stored record, to be genuine
        # changes. Runs BEFORE the criteria-specific duplicate/URL checks
        # below, so a combined rename+criteria call always hears about the
        # combination first, rather than a criteria-only rejection that
        # leaves the real problem undiscovered until the caller retries.
        if name_changed and criteria_changed:
            return {
                key: {
                    "status": "invalid",
                    "message": (
                        "a rename and a criteria change cannot be combined in one call -- "
                        "change one at a time"
                    ),
                }
            }

        if criteria_changed:
            # Precedence matches N5's create path: criteria before name.
            duplicate = find_by_fingerprint(others, new_fingerprint)
            if duplicate is not None:
                return {key: {"status": "criteria_already_saved", "existingName": duplicate.name}}
            # `criteria_changed` is only ever True when `search_filters` and
            # `params.searchUrl` are both already set (see above) -- the
            # `is None` legs are unreachable, kept only so mypy can narrow
            # both to non-None for the `url_matches_filters` call.
            if (
                search_filters is None  # pragma: no cover
                or params.searchUrl is None  # pragma: no cover
                or not url_matches_filters(params.searchUrl, search_filters)
            ):
                return {key: {"status": "invalid", "message": "searchUrl does not match searchFilters"}}

        update_fingerprint = new_fingerprint if criteria_changed else current_fingerprint

        final_name = current.name
        regeneration_attempts = 0
        if candidate_name is not None and name_changed:
            name_was_generated = params.nameWasGenerated
            if name_was_generated is None:  # pragma: no cover - unreachable, guarded by the validator
                name_was_generated = False
            settled = _settle_generated_name(
                store,
                user_key,
                update_fingerprint,
                candidate_name=candidate_name,
                name_was_generated=name_was_generated,
                criteria_summary=params.criteriaSummary or "",
                unsupported_filters=params.unsupportedFilters,
                collision_records=others,
                all_records=existing,
                user_stated_collision_status="name_exists",
                also_avoid_name=current.name,
            )
            if isinstance(settled, dict):
                return {key: settled}
            final_name, regeneration_attempts = settled

        store.clear_naming_attempts(user_key, update_fingerprint)

        # 🔴 Casefolded, matching frequency.py's own case-insensitive lookup
        # -- a raw `!=` here would treat the model resending "Daily" during
        # a pure rename as a real frequency change, bypassing the exact
        # casefold fix this PR adds to `frequency_to_schedule_interval`.
        frequency_changed = (
            params.notificationFrequency is not None
            and params.notificationFrequency.strip().casefold() != current.notification_frequency
        )

        renamed = normalise_name(final_name) != normalise_name(current.name)
        change: dict[str, Any] = {}
        if renamed:
            change["name"] = final_name
        if frequency_changed:
            change["notification_frequency"] = params.notificationFrequency
        if criteria_changed:
            change["search_filters"] = params.searchFilters
            change["fresh_es_query"] = params.esQuery
            change["new_search_url_query"] = urlsplit(params.searchUrl).query

        if not change:
            # An empty diff means there is nothing to confirm -- never issue
            # a ticket for a no-op. Returned at PROPOSE time, before
            # `store.put` stashes anything, and BEFORE either of the
            # send-time guards below -- a record with corrupted/unmappable
            # stored data (search_mode or notification_frequency) that the
            # caller is not actually touching must still resolve to
            # `no_change`, not `invalid`.
            no_change_response: dict[str, Any] = {
                "status": "no_change",
                "savedSearchId": saved_search_id,
                "name": current.name,
            }
            if params.notificationFrequency is not None:
                no_change_response["notificationFrequency"] = params.notificationFrequency
            return {key: no_change_response}

        if not criteria_changed:
            try:
                # Fail fast, at PROPOSE time, but ONLY when criteria is
                # unchanged: `apply_update`'s step 4 maps `current.search_mode`
                # via `search_mode_for_es_query` ONLY on that path (a criteria
                # change instead uses the caller-supplied `fresh_es_query`
                # directly and never touches this mapping at all) -- so a
                # rename or frequency-only change on a record with an
                # unmappable stored `searchType` would otherwise be blocked
                # forever, while a criteria change on that same record would
                # succeed. Checking here means the model gets a clear,
                # specific reason immediately for the case that actually
                # needs it, instead of a generic upstream `error` only after
                # the user has already confirmed a proposal that could never
                # have succeeded. Genuinely reachable here (this branch also
                # covers the old dedicated notifications tool's frequency-only
                # path, which has no fingerprint gate of its own).
                search_mode_for_es_query(current.search_mode)
            except SavedSearchApiError as exc:
                return {
                    key: {
                        "status": "invalid",
                        "message": f"cannot update this saved search: {exc}",
                    }
                }

        if not frequency_changed:
            try:
                # Mirrors the `search_mode` guard just above, for the same
                # reason: `apply_update` carries `current.notification_frequency`
                # forward UNCHANGED whenever frequency is not what changed, and
                # sends it straight to `client.update` -> `frequency_to_schedule_interval`.
                # A record whose stored frequency is the reachable `"unknown"`
                # state (no `(notify, scheduleId)` pair this server recognises,
                # see `schedule_pair_to_frequency`) would otherwise reach that
                # call at CONFIRM time and raise an uncaught `ValueError` there
                # instead of answering `invalid` here, at propose time.
                frequency_to_schedule_interval(current.notification_frequency)
            except ValueError:
                return {
                    key: {
                        "status": "invalid",
                        "message": (
                            "cannot update this saved search: its stored notification "
                            "frequency is not one this server recognises -- a new "
                            "notificationFrequency must be supplied to fix it"
                        ),
                    }
                }

        proposal = store.put(
            user_key,
            action="update",
            payload={"saved_search_id": saved_search_id, "change": change},
            name=final_name,
            fingerprint=update_fingerprint,
            regeneration_attempts=regeneration_attempts,
        )

        ready_response: dict[str, Any] = {
            "status": "ready",
            "proposalId": proposal.proposal_id,
            "savedSearchId": saved_search_id,
            "name": final_name,
        }
        if params.notificationFrequency is not None:
            ready_response["notificationFrequency"] = params.notificationFrequency
        if criteria_changed:
            ready_response["criteriaSummary"] = params.criteriaSummary
            ready_response["unsupportedFilters"] = params.unsupportedFilters
        return {key: ready_response}

    async def _confirm_update(proposal: Proposal, token: str, *, envelope_key: str) -> dict[str, Any]:
        """`update_saved_search`'s confirm path (steps N7 + N8 / VA-404 + VA-405).

        Re-runs both duplicate checks against LIVE data, same as the create
        path's own re-check in `save_search` -- time passes between propose
        and confirm, and a defence that only runs once is a defence a future
        code path can skip. Excludes the record being updated from both
        checks: it always matches its own current name and fingerprint,
        which is not a collision with itself.
        """
        key = envelope_key
        saved_search_id = proposal.payload["saved_search_id"]
        change = UpdateChange(**proposal.payload["change"])

        try:
            existing = await client.list_saved_searches(token)
        except SavedSearchApiError as exc:
            return {key: {"status": "error", "message": str(exc)}}

        others = [r for r in existing if r.saved_search_id != saved_search_id]

        if change.search_filters is not None:
            duplicate = find_by_fingerprint(others, proposal.fingerprint)
            if duplicate is not None:
                return {key: {"status": "criteria_already_saved", "existingName": duplicate.name}}
            # Re-asserted here too, even though propose already checked it --
            # aligning with save_search's create-confirm branch, which
            # re-runs its own URL/filters check at write time rather than
            # trusting the propose-time result alone.
            if not url_matches_filters(f"?{change.new_search_url_query or ''}", change.search_filters):
                return {
                    key: {
                        "status": "invalid",
                        "message": "searchUrl no longer matches searchFilters",
                    }
                }

        if change.name is not None:
            name_collision = find_by_name(others, change.name)
            if name_collision is not None:
                return {key: {"status": "name_exists", "existingName": name_collision.name}}

        try:
            record = await apply_update(
                client,
                es_client,
                token,
                saved_search_id=saved_search_id,
                change=change,
                records=existing,
            )
        except SavedSearchApiError as exc:
            # Never a success status for an upstream failure -- covers the
            # URL-precondition refusal, an unmappable searchMode, and an
            # id that has since vanished from the account, in addition to
            # any ordinary upstream/network failure.
            return {key: {"status": "error", "message": str(exc)}}

        result = _record_to_dict(record)
        store.mark_consumed(proposal, result=result)
        return {key: {"status": "ok", **result}}

    @app.tool(name="list_saved_searches")
    async def list_saved_searches(ctx: Context) -> dict[str, Any]:  # type: ignore[type-arg]
        """List the caller's saved searches, and answer "open my X search" by name.

        Takes NO parameters beyond the implicit MCP context. In particular: no
        user-id parameter, ever. If this tool accepted one, the model could pass
        the wrong one — an LLM-supplied identity is not an identity. The token on
        `_meta` is the only identity this tool will ever use.

        This tool is also how "load" works. "Open my Del Mar search" means: find
        that record here, and hand back its stored `searchUrl` VERBATIM. It must
        never be answered by re-running a property search instead — a fresh
        property search produces a generic explore URL with no association to the
        saved search, landing the user on a plain results page rather than the
        saved-search view they asked for. This server enforces its half of that by
        construction: it holds no property-search tool and makes no outbound call
        to one, so nothing here could satisfy "load" any other way even if asked.
        Whether the model chooses to call this tool instead of a property-search
        tool at all is a prompt/orchestrator-level concern, verified on that side.
        """
        token = bearer_token_from_meta(ctx)
        if token is None:
            return {LIST_SAVED_SEARCHES_KEY: {"status": "sign_in_required"}}

        try:
            records = await client.list_saved_searches(token)
        except SavedSearchApiError as exc:
            return {LIST_SAVED_SEARCHES_KEY: {"status": "error", "message": str(exc)}}

        return {
            LIST_SAVED_SEARCHES_KEY: {
                "status": "ok",
                "count": len(records),
                "savedSearches": [_record_to_dict(r) for r in records],
            }
        }

    @app.tool(name="propose_saved_search")
    async def propose_saved_search(
        params: ProposeSavedSearchParams,
        ctx: Context,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        """Validate a NEW save and stash it for confirmation. Writes nothing
        upstream. Create-only -- see `update_saved_search` to rename, change
        criteria, or change notification frequency on an EXISTING saved search.

        The name is GENERATED by the model, never asked for — this tool
        validates it deterministically (no collision over a fully-paged
        list, within a length limit, mentioning nothing in
        `unsupportedFilters`) rather than trusting it. On `ready` it stashes
        the fully-assembled payload server-side and returns an opaque
        `proposalId`; nothing is written until `save_search` is called with
        `confirmed: true` on a LATER turn.

        Returns one of: `ready`, `name_exists`, `name_conflict_create_only`,
        `criteria_already_saved`, `name_needs_regeneration`, `invalid`,
        `error`, `sign_in_required`.
        """
        token = bearer_token_from_meta(ctx)
        if token is None:
            return {PROPOSE_SAVED_SEARCH_KEY: {"status": "sign_in_required"}}

        try:
            frequency_to_schedule_interval(params.notificationFrequency)
        except ValueError as exc:
            return {PROPOSE_SAVED_SEARCH_KEY: {"status": "invalid", "message": str(exc)}}

        if not url_matches_filters(params.searchUrl, params.searchFilters):
            # Failing here, before any HTTP call, tells the user before they
            # confirm rather than after this server has said "saved" — the
            # upstream API performs no such validation and will happily
            # store a contradictory record.
            return {
                PROPOSE_SAVED_SEARCH_KEY: {
                    "status": "invalid",
                    "message": "searchUrl does not match searchFilters",
                }
            }

        try:
            existing = await client.list_saved_searches(token)
        except SavedSearchApiError as exc:
            return {PROPOSE_SAVED_SEARCH_KEY: {"status": "error", "message": str(exc)}}

        user_key = user_key_from_token(token)
        fingerprint = criteria_fingerprint(params.searchFilters, params.searchMode)

        # Precedence: criteria_already_saved before name_exists -- requirement
        # 4 carries more useful information than requirement 2/3's name check.
        duplicate = find_by_fingerprint(existing, fingerprint)
        if duplicate is not None:
            return {
                PROPOSE_SAVED_SEARCH_KEY: {
                    "status": "criteria_already_saved",
                    "existingName": duplicate.name,
                }
            }

        settled = _settle_generated_name(
            store,
            user_key,
            fingerprint,
            candidate_name=params.name,
            name_was_generated=params.nameWasGenerated,
            criteria_summary=params.criteriaSummary,
            unsupported_filters=params.unsupportedFilters,
            collision_records=existing,
            all_records=existing,
            user_stated_collision_status=(
                "name_conflict_create_only" if params.intent == "create_new" else "name_exists"
            ),
        )
        if isinstance(settled, dict):
            return {PROPOSE_SAVED_SEARCH_KEY: settled}
        final_name, regeneration_attempts = settled

        store.clear_naming_attempts(user_key, fingerprint)

        # `previousName` is populated ONLY when a GENERATED name changed
        # from the user's one still-pending proposal -- never for a
        # user-stated name (we never second-guess a name the user chose)
        # and never when nothing about the name actually changed.
        previous_name: str | None = None
        current_proposal = store.current(user_key, action="save")
        if (
            current_proposal is not None
            and params.nameWasGenerated
            and current_proposal.name != final_name
        ):
            previous_name = current_proposal.name

        payload: dict[str, Any] = {
            "search_filters": params.searchFilters,
            "search_url": params.searchUrl,
            "es_query": params.esQuery,
            "notification_frequency": params.notificationFrequency,
            "search_mode": params.searchMode,
        }
        proposal = store.put(
            user_key,
            action="save",
            payload=payload,
            name=final_name,
            fingerprint=fingerprint,
            previous_name=previous_name,
            regeneration_attempts=regeneration_attempts,
        )

        response: dict[str, Any] = {
            "status": "ready",
            "proposalId": proposal.proposal_id,
            "name": final_name,
            "criteriaSummary": params.criteriaSummary,
            "notificationFrequency": params.notificationFrequency,
            "unsupportedFilters": params.unsupportedFilters,
        }
        if previous_name is not None:
            response["previousName"] = previous_name
        return {PROPOSE_SAVED_SEARCH_KEY: response}

    def _resolve_confirmed_proposal(
        proposal_id: str,
        user_key: str,
        *,
        confirmed: bool | None,
        expected_actions: tuple[ProposalAction, ...],
        already_done_status: str,
        envelope_key: str,
    ) -> dict[str, Any] | Proposal:
        """The confirm-guard sequence shared by `save_search`,
        `update_saved_search` and `delete_saved_search`.

        Returns either an envelope dict the caller should return immediately
        (not confirmed, unknown/expired/foreign id, action mismatch, a
        replay of an already-consumed proposal, or a confirm already in
        flight for this exact proposal), or the validated, now-CLAIMED
        `Proposal` to proceed with. Factored out so the three writers cannot
        drift on this sequence independently — each has (and keeps,
        deliberately) one real difference, `already_done_status`
        (`already_saved` / `already_updated` / `already_deleted`), and a
        fourth writer would otherwise need a hand-copied version with no
        guardrail against getting a step wrong or out of order.

        🔴 The `consumed` check and the `store.claim()` call below run with
        no `await` between them, which is what makes them race-free -- see
        `ProposalStore.claim`'s docstring. A caller that gets back a
        `Proposal` from this function MUST eventually call either
        `store.mark_consumed` (on success) or `store.release` (on any other
        return) — see each writer's `try/finally` around this call.
        """
        if confirmed is not True:
            return {envelope_key: {"status": "not_confirmed"}}

        proposal = store.get(proposal_id, user_key)
        if proposal is None:
            # Collapses "unknown", "expired" AND "belongs to another user"
            # into one response -- see ProposalStore.get's docstring for why
            # that collapse is the point. A's proposalId with B's token
            # lands here, indistinguishable from an id that never existed.
            return {envelope_key: {"status": "proposal_expired"}}

        if proposal.action not in expected_actions:
            return {envelope_key: {"status": "proposal_action_mismatch"}}

        if proposal.consumed:
            # Idempotent replay: the same proposalId confirmed twice acts
            # once. Returns the ORIGINAL result rather than re-deriving one,
            # so a crashed client retrying its own successful request sees
            # the same outcome it already got.
            return {envelope_key: {"status": already_done_status, **(proposal.result or {})}}

        if not store.claim(proposal):
            # Another confirm of this exact proposalId is still executing
            # its upstream call -- refuse this one rather than racing it.
            return {envelope_key: {"status": "proposal_in_progress"}}

        return proposal

    @contextmanager
    def _release_unless_consumed(proposal: Proposal) -> Iterator[None]:
        """The `try/finally: store.release(...)` shared by `save_search`,
        `update_saved_search` and `delete_saved_search`, once each has a
        CLAIMED `Proposal` in hand from `_resolve_confirmed_proposal`.

        Every return from the wrapped block except the one that calls
        `store.mark_consumed` falls through with `proposal.consumed` still
        `False` -- release the claim so a legitimate retry (fix the name,
        try again) is not permanently stuck reporting `proposal_in_progress`.
        """
        try:
            yield
        finally:
            if not proposal.consumed:
                store.release(proposal)

    @app.tool(name="save_search")
    async def save_search(params: SaveSearchParams, ctx: Context) -> dict[str, Any]:  # type: ignore[type-arg]
        """The writer for a NEW saved search. Input is `{proposalId, confirmed}`.
        Confirms a `save` proposal only -- see `update_saved_search` to confirm
        a rename, criteria change, or frequency change, and `delete_saved_search`
        to confirm a delete.

        Every check below is server-enforced and never trusts the model:
        an unconfirmed call writes nothing, an unknown/expired/foreign
        `proposalId` returns `proposal_expired` (never "closest matching
        pending proposal"), a proposal of any other action returns
        `proposal_action_mismatch`, and both duplicate checks re-run
        against LIVE data before this writes anything -- time passes
        between propose and confirm, and a defence that only runs once is a
        defence a future code path can skip.
        """
        token = bearer_token_from_meta(ctx)
        if token is None:
            return {SAVE_SEARCH_KEY: {"status": "sign_in_required"}}

        user_key = user_key_from_token(token)
        resolved = _resolve_confirmed_proposal(
            params.proposalId,
            user_key,
            confirmed=params.confirmed,
            expected_actions=("save",),
            already_done_status="already_saved",
            envelope_key=SAVE_SEARCH_KEY,
        )
        if isinstance(resolved, dict):
            return resolved
        proposal = resolved

        with _release_unless_consumed(proposal):
            try:
                existing = await client.list_saved_searches(token)
            except SavedSearchApiError as exc:
                return {SAVE_SEARCH_KEY: {"status": "error", "message": str(exc)}}

            duplicate = find_by_fingerprint(existing, proposal.fingerprint)
            if duplicate is not None:
                return {
                    SAVE_SEARCH_KEY: {
                        "status": "criteria_already_saved",
                        "existingName": duplicate.name,
                    }
                }

            name_collision = find_by_name(existing, proposal.name)
            if name_collision is not None:
                return {SAVE_SEARCH_KEY: {"status": "name_exists", "existingName": name_collision.name}}

            payload = proposal.payload
            if not url_matches_filters(payload["search_url"], payload["search_filters"]):
                return {
                    SAVE_SEARCH_KEY: {
                        "status": "invalid",
                        "message": "searchUrl no longer matches searchFilters",
                    }
                }

            try:
                record = await client.create(
                    token,
                    name=proposal.name,
                    search_filters=payload["search_filters"],
                    search_url=payload["search_url"],
                    es_query=payload["es_query"],
                    notification_frequency=payload["notification_frequency"],
                )
            except SavedSearchNameExistsError:
                # The free backstop: verified to key on name only, so this can
                # fire even if our own client-side check above somehow missed
                # it. Mapped to the same clean message rather than a raw 400.
                return {SAVE_SEARCH_KEY: {"status": "name_exists"}}
            except SavedSearchApiError as exc:
                # Never a success status for an upstream failure -- a bare
                # exception here would otherwise crash the tool call instead of
                # answering "couldn't save right now".
                return {SAVE_SEARCH_KEY: {"status": "error", "message": str(exc)}}

            result = _record_to_dict(record)
            store.mark_consumed(proposal, result=result)
            return {SAVE_SEARCH_KEY: {"status": "ok", **result}}

    @app.tool(name="update_saved_search")
    async def update_saved_search(
        params: UpdateSavedSearchParams,
        ctx: Context,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        """Propose, then confirm, a rename, a criteria change, and/or a
        notification-frequency change on an EXISTING saved search. Never a
        one-shot on first mention -- every change still goes through the
        confirm-then-write pattern every other write here uses.

        Two shapes -- see `UpdateSavedSearchParams`. PROPOSE (`savedSearchId`
        + at least one change) writes nothing and returns a `proposalId`.
        CONFIRM (`proposalId` + `confirmed: true`) executes it,
        server-enforced the same way `save_search` enforces a create: an
        unconfirmed call writes nothing, an unknown/expired/foreign id
        returns `proposal_expired`, and a `save`-or-`delete` proposal
        presented here returns `proposal_action_mismatch`.

        A rename and a criteria change can never be combined in one call --
        the confirmation summary the user approves must describe exactly one
        thing. `notificationFrequency` may ride along with either.

        A change that is already true of the stored record (e.g. setting an
        already-Daily search to Daily again) returns `no_change` rather than
        proposing a no-op write.

        Also covers what a prior version of this server exposed as a
        separate `update_saved_search_notifications` tool -- a
        frequency-only call needs only `savedSearchId` + `notificationFrequency`.
        It still cannot be a two-field passthrough to the API: full-replace
        semantics mean every OTHER field must be resent on write, so this
        runs through the exact same six-step recipe as any other update
        (`vesta_saved_search.updates.apply_update`).
        """
        token = bearer_token_from_meta(ctx)
        if token is None:
            return {UPDATE_SAVED_SEARCH_KEY: {"status": "sign_in_required"}}

        if params.savedSearchId is not None:
            return await _propose_update(params, token, envelope_key=UPDATE_SAVED_SEARCH_KEY)

        # Confirm shape: proposalId + confirmed. The params validator
        # guarantees proposalId is set whenever savedSearchId is not.
        user_key = user_key_from_token(token)
        resolved = _resolve_confirmed_proposal(
            params.proposalId,  # type: ignore[arg-type]
            user_key,
            confirmed=params.confirmed,
            expected_actions=("update",),
            already_done_status="already_updated",
            envelope_key=UPDATE_SAVED_SEARCH_KEY,
        )
        if isinstance(resolved, dict):
            return resolved
        proposal = resolved

        with _release_unless_consumed(proposal):
            return await _confirm_update(proposal, token, envelope_key=UPDATE_SAVED_SEARCH_KEY)

    @app.tool(name="delete_saved_search")
    async def delete_saved_search(
        params: DeleteSavedSearchParams,
        ctx: Context,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        """Propose, then confirm, a delete. Never a one-shot on first mention.

        Two shapes — see `DeleteSavedSearchParams`. PROPOSE (`savedSearchId`
        alone) writes nothing and returns a `proposalId`. CONFIRM
        (`proposalId` + `confirmed: true`) executes it, server-enforced the
        same way `save_search` enforces a save: unconfirmed writes nothing,
        an unknown/expired/foreign id returns `proposal_expired`, and a
        `save`-or-`update` proposal presented here returns
        `proposal_action_mismatch`.

        🔴 Why this is safe even with a save proposal and a delete proposal
        both recently discussed in one conversation: N5's two rules --
        **one pending proposal per user** and **the stash records its
        action** -- mean there is never a set of pending proposals to choose
        the wrong one from, and a mismatched action is refused rather than
        silently executed as the wrong operation.
        """
        token = bearer_token_from_meta(ctx)
        if token is None:
            return {DELETE_SAVED_SEARCH_KEY: {"status": "sign_in_required"}}

        user_key = user_key_from_token(token)

        if params.savedSearchId is not None:
            found = await _find_record_or_invalid(
                params.savedSearchId, token, envelope_key=DELETE_SAVED_SEARCH_KEY
            )
            if isinstance(found, dict):
                return found
            _existing, current = found

            new_proposal = store.put(
                user_key,
                action="delete",
                payload={"saved_search_id": params.savedSearchId},
                name=current.name,
                fingerprint=criteria_fingerprint(current.search_filters, current.search_mode),
            )
            return {
                DELETE_SAVED_SEARCH_KEY: {
                    "status": "ready",
                    "proposalId": new_proposal.proposal_id,
                    "savedSearchId": params.savedSearchId,
                    "name": current.name,
                }
            }

        # Confirm shape: proposalId + confirmed. The pydantic validator
        # guarantees proposalId is set whenever savedSearchId is not.
        proposal_id = params.proposalId
        if proposal_id is None:  # pragma: no cover - unreachable, guarded by the model validator
            return {DELETE_SAVED_SEARCH_KEY: {"status": "invalid", "message": "proposalId is required"}}

        resolved = _resolve_confirmed_proposal(
            proposal_id,
            user_key,
            confirmed=params.confirmed,
            expected_actions=("delete",),
            already_done_status="already_deleted",
            envelope_key=DELETE_SAVED_SEARCH_KEY,
        )
        if isinstance(resolved, dict):
            return resolved
        stashed_proposal = resolved

        with _release_unless_consumed(stashed_proposal):
            saved_search_id = stashed_proposal.payload["saved_search_id"]
            try:
                await client.delete(token, saved_search_id)
            except SavedSearchApiError as exc:
                return {DELETE_SAVED_SEARCH_KEY: {"status": "error", "message": str(exc)}}

            result = {"savedSearchId": saved_search_id}
            store.mark_consumed(stashed_proposal, result=result)
            return {DELETE_SAVED_SEARCH_KEY: {"status": "ok", **result}}


__all__ = [
    "DELETE_SAVED_SEARCH_KEY",
    "LIST_SAVED_SEARCHES_KEY",
    "PROPOSAL_FIELD_NAMES",
    "PROPOSE_SAVED_SEARCH_KEY",
    "RECORD_FIELD_NAMES",
    "SAVE_SEARCH_KEY",
    "UPDATE_SAVED_SEARCH_KEY",
    "register_tools",
]
