"""MCP tools exposed by this server. First one: `list_saved_searches` (step N3).

Registering a tool is the job of :func:`register_tools`, called once from
:func:`vesta_saved_search.server.create_app`. Kept in its own module, separate
from `server.py`'s transport/health-route concerns, so a test can register these
tools on a bare `FastMCP` instance without going through the full app factory.
"""

from __future__ import annotations

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

#: The single top-level key of every response this server's tools return.
#:
#: `capability_executor.py` unwraps a response with exactly one top-level
#: dict-valued key and makes that key the `mode_key` (VA-398's contract with the
#: orchestrator). Every branch below — success, sign-in-required, error — MUST
#: keep this as the only top-level key, or a different branch would silently
#: change `mode_key` for every response that hits it.
_ENVELOPE_KEY = "saved_search"

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

#: Field names `propose_saved_search` and `save_search` emit, beyond the ones
#: already covered by `RECORD_FIELD_NAMES`. Same reason those are tracked:
#: `test_sensitive_keys.py` loops over this tuple too, so a name added here
#: later that happens to contain a sensitive substring is caught by that
#: test rather than discovered as a field silently missing from production.
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
    #: `update_existing` requires `savedSearchId` and is handled by step N7's
    #: `_propose_update` branch below -- see that function's docstring.
    intent: Literal["auto", "create_new", "update_existing"] = "auto"
    #: Required when `intent="update_existing"`. Identifies which of the
    #: caller's own saved searches is being changed.
    savedSearchId: int | None = None


class SaveSearchParams(BaseModel):
    """Input to `save_search` (step N6/N7 / VA-402/VA-404). Nothing else.

    See `vesta_saved_search.proposals`'s module docstring for why this is
    `{proposalId, confirmed}` and not a resend of the whole payload. Confirms
    BOTH `save` and `update` proposals -- see the tool's own docstring.
    """

    proposalId: str
    confirmed: bool


class UpdateSavedSearchNotificationsParams(BaseModel):
    """Input to `update_saved_search_notifications` (step N8 / VA-405).

    Exactly these two fields, per the plan -- everything else the full
    six-step update recipe needs is read fresh from the stored record by
    `apply_update`, never resent by the caller.
    """

    savedSearchId: int
    notificationFrequency: str


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
) -> dict[str, Any] | tuple[str, int]:
    """Resolve `candidate_name` to `(final_name, regeneration_attempts)`, or an
    envelope body the caller should return immediately.

    The name-collision / length / unsupported-filter / regeneration /
    deterministic-fallback state machine shared by `propose_saved_search`'s
    create path and `_propose_update`'s rename path — kept in one place so
    the two cannot drift on it independently, and so a rename gets the exact
    same `mentions_unsupported_filter` protection a create already had (a
    generated rename can reference a filter the search does not actually
    apply, same as a generated create name could). Only what happens on a
    user-STATED collision differs between the two callers
    (`name_conflict_create_only` vs `name_exists`), supplied via
    `user_stated_collision_status`.

    The deterministic fallback is re-checked against `collision_records`
    too (`dedupe_fallback_name`) — two structurally different searches can
    share the same `criteriaSummary`-derived fallback, and a collision
    caught here at propose time is cheaper than one first discovered at
    confirm time as `name_exists`.
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
        fallback, collision_records, max_length=SAVED_SEARCH_NAME_MAX_LENGTH
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

    async def _propose_update(params: ProposeSavedSearchParams, token: str) -> dict[str, Any]:
        """`propose_saved_search`'s `intent=update_existing` branch (step N7 / VA-404).

        Unlike a create proposal, the model may resend the record's OWN
        unchanged `searchFilters`/`searchUrl`/`esQuery` when only renaming or
        changing frequency — `apply_update` never trusts a caller-supplied
        `esQuery` for an unchanged-criteria update anyway (it always fetches
        a fresh one via S4), so a stale value here is harmless.

        🔴 The URL-format precondition is enforced STRUCTURALLY, not by
        inspecting `params.searchUrl`'s raw text: only its QUERY STRING is
        ever extracted (see `UpdateChange` below), and `apply_update` always
        re-attaches that to the record's own stored path. A create-shaped
        URL from the model therefore has no code path left that could ever
        send its path anywhere — there is nothing to assert against because
        there is nothing left to go wrong.
        """
        if params.savedSearchId is None:
            return {
                _ENVELOPE_KEY: {
                    "status": "invalid",
                    "message": "savedSearchId is required when intent=update_existing",
                }
            }

        try:
            frequency_to_schedule_interval(params.notificationFrequency)
        except ValueError as exc:
            return {_ENVELOPE_KEY: {"status": "invalid", "message": str(exc)}}

        try:
            existing = await client.list_saved_searches(token)
        except SavedSearchApiError as exc:
            return {_ENVELOPE_KEY: {"status": "error", "message": str(exc)}}

        current = next((r for r in existing if r.saved_search_id == params.savedSearchId), None)
        if current is None:
            return {
                _ENVELOPE_KEY: {
                    "status": "invalid",
                    "message": f"no saved search with id {params.savedSearchId}",
                }
            }

        others = [r for r in existing if r.saved_search_id != params.savedSearchId]
        user_key = user_key_from_token(token)

        new_fingerprint = criteria_fingerprint(params.searchFilters, params.searchMode)
        current_fingerprint = criteria_fingerprint(current.search_filters, current.search_mode)
        criteria_changed = new_fingerprint != current_fingerprint

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
                # have succeeded.
                #
                # Provably unreachable via THIS function today, and kept
                # anyway: `criteria_fingerprint` bakes `search_mode` into the
                # hash it compares, casefolded — so `criteria_changed` being
                # `False` already guarantees `current.search_mode` casefolds
                # to one of `params.searchMode`'s three valid literals, which
                # are exactly the three `search_mode_for_es_query` maps. This
                # guard is defensive against that fingerprint relationship
                # ever changing, and it keeps this branch symmetric with
                # `update_saved_search_notifications`'s identical check
                # below, which genuinely IS reachable — that tool has no
                # fingerprint gate at all, so a corrupted stored `searchType`
                # reaches it directly. See that tool's test for the real
                # coverage of this exact failure mode.
                search_mode_for_es_query(current.search_mode)
            except SavedSearchApiError as exc:  # pragma: no cover
                return {
                    _ENVELOPE_KEY: {
                        "status": "invalid",
                        "message": f"cannot update this saved search: {exc}",
                    }
                }

        if criteria_changed:
            # Precedence matches N5's create path: criteria before name.
            duplicate = find_by_fingerprint(others, new_fingerprint)
            if duplicate is not None:
                return {
                    _ENVELOPE_KEY: {
                        "status": "criteria_already_saved",
                        "existingName": duplicate.name,
                    }
                }
            if not url_matches_filters(params.searchUrl, params.searchFilters):
                return {
                    _ENVELOPE_KEY: {
                        "status": "invalid",
                        "message": "searchUrl does not match searchFilters",
                    }
                }

        update_fingerprint = new_fingerprint if criteria_changed else current_fingerprint
        name_changed = normalise_name(params.name) != normalise_name(current.name)
        final_name = current.name
        regeneration_attempts = 0

        if name_changed:
            settled = _settle_generated_name(
                store,
                user_key,
                update_fingerprint,
                candidate_name=params.name,
                name_was_generated=params.nameWasGenerated,
                criteria_summary=params.criteriaSummary,
                unsupported_filters=params.unsupportedFilters,
                collision_records=others,
                all_records=existing,
                user_stated_collision_status="name_exists",
            )
            if isinstance(settled, dict):
                return {_ENVELOPE_KEY: settled}
            final_name, regeneration_attempts = settled

        store.clear_naming_attempts(user_key, update_fingerprint)

        # 🔴 Casefolded, matching frequency.py's own case-insensitive lookup
        # -- a raw `!=` here would treat the model resending "Daily" during
        # a pure rename as a real frequency change, bypassing the exact
        # casefold fix this PR adds to `frequency_to_schedule_interval`.
        frequency_changed = (
            params.notificationFrequency.strip().casefold() != current.notification_frequency
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

        proposal = store.put(
            user_key,
            action="update",
            payload={"saved_search_id": params.savedSearchId, "change": change},
            name=final_name,
            fingerprint=update_fingerprint,
            regeneration_attempts=regeneration_attempts,
        )

        response: dict[str, Any] = {
            "status": "ready",
            "proposalId": proposal.proposal_id,
            "savedSearchId": params.savedSearchId,
            "name": final_name,
            "notificationFrequency": params.notificationFrequency,
        }
        if criteria_changed:
            response["criteriaSummary"] = params.criteriaSummary
            response["unsupportedFilters"] = params.unsupportedFilters
        return {_ENVELOPE_KEY: response}

    async def _confirm_update(proposal: Proposal, token: str) -> dict[str, Any]:
        """`save_search`'s `update`-action branch (step N7 / VA-404).

        Re-runs both duplicate checks against LIVE data, same as the create
        path's own re-check in `save_search` proper -- time passes between
        propose and confirm, and a defence that only runs once is a defence
        a future code path can skip. Excludes the record being updated from
        both checks: it always matches its own current name and fingerprint,
        which is not a collision with itself.
        """
        saved_search_id = proposal.payload["saved_search_id"]
        change = UpdateChange(**proposal.payload["change"])

        try:
            existing = await client.list_saved_searches(token)
        except SavedSearchApiError as exc:
            return {_ENVELOPE_KEY: {"status": "error", "message": str(exc)}}

        others = [r for r in existing if r.saved_search_id != saved_search_id]

        if change.search_filters is not None:
            duplicate = find_by_fingerprint(others, proposal.fingerprint)
            if duplicate is not None:
                return {
                    _ENVELOPE_KEY: {
                        "status": "criteria_already_saved",
                        "existingName": duplicate.name,
                    }
                }
            # Re-asserted here too, even though propose already checked it --
            # aligning with save_search's create-confirm branch a few dozen
            # lines below, which re-runs its own URL/filters check at write
            # time rather than trusting the propose-time result alone.
            if not url_matches_filters(f"?{change.new_search_url_query or ''}", change.search_filters):
                return {
                    _ENVELOPE_KEY: {
                        "status": "invalid",
                        "message": "searchUrl no longer matches searchFilters",
                    }
                }

        if change.name is not None:
            name_collision = find_by_name(others, change.name)
            if name_collision is not None:
                return {_ENVELOPE_KEY: {"status": "name_exists", "existingName": name_collision.name}}

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
            return {_ENVELOPE_KEY: {"status": "error", "message": str(exc)}}

        result = _record_to_dict(record)
        store.mark_consumed(proposal, result=result)
        return {_ENVELOPE_KEY: {"status": "ok", **result}}

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
            return {_ENVELOPE_KEY: {"status": "sign_in_required"}}

        try:
            records = await client.list_saved_searches(token)
        except SavedSearchApiError as exc:
            return {_ENVELOPE_KEY: {"status": "error", "message": str(exc)}}

        return {
            _ENVELOPE_KEY: {
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
        """Validate a save and stash it for confirmation. Writes nothing upstream.

        The name is GENERATED by the model, never asked for — this tool
        validates it deterministically (no collision over a fully-paged
        list, within a length limit, mentioning nothing in
        `unsupportedFilters`) rather than trusting it. On `ready` it stashes
        the fully-assembled payload server-side and returns an opaque
        `proposalId`; nothing is written until `save_search` is called with
        `confirmed: true` on a LATER turn.

        Returns one of: `ready`, `name_exists`, `name_conflict_create_only`,
        `criteria_already_saved`, `name_needs_regeneration`, `invalid`,
        `sign_in_required`.
        """
        token = bearer_token_from_meta(ctx)
        if token is None:
            return {_ENVELOPE_KEY: {"status": "sign_in_required"}}

        if params.intent == "update_existing":
            return await _propose_update(params, token)

        try:
            frequency_to_schedule_interval(params.notificationFrequency)
        except ValueError as exc:
            return {_ENVELOPE_KEY: {"status": "invalid", "message": str(exc)}}

        if not url_matches_filters(params.searchUrl, params.searchFilters):
            # Failing here, before any HTTP call, tells the user before they
            # confirm rather than after this server has said "saved" — the
            # upstream API performs no such validation and will happily
            # store a contradictory record.
            return {
                _ENVELOPE_KEY: {
                    "status": "invalid",
                    "message": "searchUrl does not match searchFilters",
                }
            }

        try:
            existing = await client.list_saved_searches(token)
        except SavedSearchApiError as exc:
            return {_ENVELOPE_KEY: {"status": "error", "message": str(exc)}}

        user_key = user_key_from_token(token)
        fingerprint = criteria_fingerprint(params.searchFilters, params.searchMode)

        # Precedence: criteria_already_saved before name_exists -- requirement
        # 4 carries more useful information than requirement 2/3's name check.
        duplicate = find_by_fingerprint(existing, fingerprint)
        if duplicate is not None:
            return {
                _ENVELOPE_KEY: {
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
            return {_ENVELOPE_KEY: settled}
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
        return {_ENVELOPE_KEY: response}

    def _resolve_confirmed_proposal(
        proposal_id: str,
        user_key: str,
        *,
        confirmed: bool | None,
        expected_actions: tuple[ProposalAction, ...],
        already_done_status: str,
    ) -> dict[str, Any] | Proposal:
        """The confirm-guard sequence shared by `save_search` and `delete_saved_search`.

        Returns either an envelope dict the caller should return immediately
        (not confirmed, unknown/expired/foreign id, action mismatch, a
        replay of an already-consumed proposal, or a confirm already in
        flight for this exact proposal), or the validated, now-CLAIMED
        `Proposal` to proceed with. Factored out so the two writers cannot
        drift on this sequence independently — they already had (and keep,
        deliberately) one real difference, `already_done_status`
        (`already_saved` vs `already_deleted`), and the next propose/confirm
        verb would otherwise need a third hand-copied version with no
        guardrail against getting a step wrong or out of order.

        🔴 The `consumed` check and the `store.claim()` call below run with
        no `await` between them, which is what makes them race-free -- see
        `ProposalStore.claim`'s docstring. A caller that gets back a
        `Proposal` from this function MUST eventually call either
        `store.mark_consumed` (on success) or `store.release` (on any other
        return) — see `save_search` / `delete_saved_search`'s `try/finally`
        around this call.
        """
        if confirmed is not True:
            return {_ENVELOPE_KEY: {"status": "not_confirmed"}}

        proposal = store.get(proposal_id, user_key)
        if proposal is None:
            # Collapses "unknown", "expired" AND "belongs to another user"
            # into one response -- see ProposalStore.get's docstring for why
            # that collapse is the point. A's proposalId with B's token
            # lands here, indistinguishable from an id that never existed.
            return {_ENVELOPE_KEY: {"status": "proposal_expired"}}

        if proposal.action not in expected_actions:
            return {_ENVELOPE_KEY: {"status": "proposal_action_mismatch"}}

        if proposal.consumed:
            # Idempotent replay: the same proposalId confirmed twice acts
            # once. Returns the ORIGINAL result rather than re-deriving one,
            # so a crashed client retrying its own successful request sees
            # the same outcome it already got.
            return {_ENVELOPE_KEY: {"status": already_done_status, **(proposal.result or {})}}

        if not store.claim(proposal):
            # Another confirm of this exact proposalId is still executing
            # its upstream call -- refuse this one rather than racing it.
            return {_ENVELOPE_KEY: {"status": "proposal_in_progress"}}

        return proposal

    @app.tool(name="save_search")
    async def save_search(params: SaveSearchParams, ctx: Context) -> dict[str, Any]:  # type: ignore[type-arg]
        """The writer for both creates and updates. Input is `{proposalId, confirmed}`.

        Every check below is server-enforced and never trusts the model:
        an unconfirmed call writes nothing, an unknown/expired/foreign
        `proposalId` returns `proposal_expired` (never "closest matching
        pending proposal"), a `delete` proposal returns
        `proposal_action_mismatch`, and both duplicate checks re-run
        against LIVE data before this writes anything -- time passes
        between propose and confirm, and a defence that only runs once is a
        defence a future code path can skip.

        Confirms a `save` proposal (step N6) by calling `client.create`, or
        an `update` proposal (step N7) by running the full six-step update
        recipe in `vesta_saved_search.updates.apply_update` -- never a
        two-field passthrough, per that module's full-replace warning.
        """
        token = bearer_token_from_meta(ctx)
        if token is None:
            return {_ENVELOPE_KEY: {"status": "sign_in_required"}}

        user_key = user_key_from_token(token)
        resolved = _resolve_confirmed_proposal(
            params.proposalId,
            user_key,
            confirmed=params.confirmed,
            expected_actions=("save", "update"),
            already_done_status="already_saved",
        )
        if isinstance(resolved, dict):
            return resolved
        proposal = resolved

        try:
            if proposal.action == "update":
                return await _confirm_update(proposal, token)

            try:
                existing = await client.list_saved_searches(token)
            except SavedSearchApiError as exc:
                return {_ENVELOPE_KEY: {"status": "error", "message": str(exc)}}

            duplicate = find_by_fingerprint(existing, proposal.fingerprint)
            if duplicate is not None:
                return {
                    _ENVELOPE_KEY: {
                        "status": "criteria_already_saved",
                        "existingName": duplicate.name,
                    }
                }

            name_collision = find_by_name(existing, proposal.name)
            if name_collision is not None:
                return {_ENVELOPE_KEY: {"status": "name_exists", "existingName": name_collision.name}}

            payload = proposal.payload
            if not url_matches_filters(payload["search_url"], payload["search_filters"]):
                return {
                    _ENVELOPE_KEY: {
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
                return {_ENVELOPE_KEY: {"status": "name_exists"}}
            except SavedSearchApiError as exc:
                # Never a success status for an upstream failure -- a bare
                # exception here would otherwise crash the tool call instead of
                # answering "couldn't save right now".
                return {_ENVELOPE_KEY: {"status": "error", "message": str(exc)}}

            result = _record_to_dict(record)
            store.mark_consumed(proposal, result=result)
            return {_ENVELOPE_KEY: {"status": "ok", **result}}
        finally:
            # Every return above except the `mark_consumed` one falls
            # through to here with `proposal.consumed` still `False` --
            # release the claim so a legitimate retry (fix the name, try
            # again) is not permanently stuck reporting `proposal_in_progress`.
            if not proposal.consumed:
                store.release(proposal)

    @app.tool(name="update_saved_search_notifications")
    async def update_saved_search_notifications(
        params: UpdateSavedSearchNotificationsParams,
        ctx: Context,  # type: ignore[type-arg]
    ) -> dict[str, Any]:
        """Propose a frequency-only change. Still confirmed via `save_search`.

        Cannot be a two-field passthrough to the API — full-replace
        semantics mean every OTHER field must be resent on write, so this
        runs through the exact same six-step recipe as any other update
        (`vesta_saved_search.updates.apply_update`); it just supplies only a
        frequency to change. A frequency edit is still a change to the
        user's data, and full-replace means a bug here could silently reset
        other fields, so it does not get to skip confirmation.
        """
        token = bearer_token_from_meta(ctx)
        if token is None:
            return {_ENVELOPE_KEY: {"status": "sign_in_required"}}

        try:
            frequency_to_schedule_interval(params.notificationFrequency)
        except ValueError as exc:
            return {_ENVELOPE_KEY: {"status": "invalid", "message": str(exc)}}

        try:
            existing = await client.list_saved_searches(token)
        except SavedSearchApiError as exc:
            return {_ENVELOPE_KEY: {"status": "error", "message": str(exc)}}

        current = next((r for r in existing if r.saved_search_id == params.savedSearchId), None)
        if current is None:
            return {
                _ENVELOPE_KEY: {
                    "status": "invalid",
                    "message": f"no saved search with id {params.savedSearchId}",
                }
            }

        try:
            # A frequency-only change never touches criteria, so
            # `apply_update`'s step 4 always maps `current.search_mode` for
            # this path (never the caller-supplied `fresh_es_query` branch)
            # -- see `_propose_update`'s identical check for why this must
            # fail fast, at propose time, rather than only at confirm.
            search_mode_for_es_query(current.search_mode)
        except SavedSearchApiError as exc:
            return {
                _ENVELOPE_KEY: {
                    "status": "invalid",
                    "message": f"cannot update this saved search: {exc}",
                }
            }

        user_key = user_key_from_token(token)
        fingerprint = criteria_fingerprint(current.search_filters, current.search_mode)
        proposal = store.put(
            user_key,
            action="update",
            payload={
                "saved_search_id": params.savedSearchId,
                "change": {"notification_frequency": params.notificationFrequency},
            },
            name=current.name,
            fingerprint=fingerprint,
        )
        return {
            _ENVELOPE_KEY: {
                "status": "ready",
                "proposalId": proposal.proposal_id,
                "savedSearchId": params.savedSearchId,
                "name": current.name,
                "notificationFrequency": params.notificationFrequency,
            }
        }

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
            return {_ENVELOPE_KEY: {"status": "sign_in_required"}}

        user_key = user_key_from_token(token)

        if params.savedSearchId is not None:
            try:
                existing = await client.list_saved_searches(token)
            except SavedSearchApiError as exc:
                return {_ENVELOPE_KEY: {"status": "error", "message": str(exc)}}

            current = next((r for r in existing if r.saved_search_id == params.savedSearchId), None)
            if current is None:
                return {
                    _ENVELOPE_KEY: {
                        "status": "invalid",
                        "message": f"no saved search with id {params.savedSearchId}",
                    }
                }

            new_proposal = store.put(
                user_key,
                action="delete",
                payload={"saved_search_id": params.savedSearchId},
                name=current.name,
                fingerprint=criteria_fingerprint(current.search_filters, current.search_mode),
            )
            return {
                _ENVELOPE_KEY: {
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
            return {_ENVELOPE_KEY: {"status": "invalid", "message": "proposalId is required"}}

        resolved = _resolve_confirmed_proposal(
            proposal_id,
            user_key,
            confirmed=params.confirmed,
            expected_actions=("delete",),
            already_done_status="already_deleted",
        )
        if isinstance(resolved, dict):
            return resolved
        stashed_proposal = resolved

        try:
            saved_search_id = stashed_proposal.payload["saved_search_id"]
            try:
                await client.delete(token, saved_search_id)
            except SavedSearchApiError as exc:
                return {_ENVELOPE_KEY: {"status": "error", "message": str(exc)}}

            result = {"savedSearchId": saved_search_id}
            store.mark_consumed(stashed_proposal, result=result)
            return {_ENVELOPE_KEY: {"status": "ok", **result}}
        finally:
            if not stashed_proposal.consumed:
                store.release(stashed_proposal)


__all__ = ["register_tools"]
