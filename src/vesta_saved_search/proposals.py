"""The propose -> confirm proposal stash (steps N5, N6, N9 / VA-401, VA-402, VA-406).

🔴 This is the ONLY server-side state this feature introduces, and therefore
the only place a cross-user leak can be created. Everything else in this
server is stateless per request — every other tool reads or writes the
GuestSite API directly with no state of its own. This module exists because
`save_search` and `delete_saved_search` take `{proposalId, confirmed}` and
nothing else, per the plan's `proposalId`-not-resend design: the "yes" turn
must not have to reproduce `searchFilters`, a ~500-character `esQuery` blob
and `searchUrl` verbatim across two turns, both because silent drift there
would permanently break notification matching and because it would put that
machine data into conversation history.

Why identity validation on every read of a proposal is not optional: MCP
sessions are pooled and shared across users with no user dimension
(`pool.py` round-robins `MCP_POOL_SIZE` sessions per URL). Two consecutive
tool calls on one session are routinely two different people. See
`vesta_saved_search.identity.bearer_token_from_meta` for the matching
argument at the point identity first enters this server.

Keyed on a hash of the bearer token, not a claim decoded out of it: the real
GuestSite tokens observed in dev and sprint are opaque strings, not JWTs, so
there is no `sub` (or equivalent) to read. Hashing means no credential is
ever held in this store, so there is nothing to leak in a log line or a heap
dump if this process is ever inspected. The cost is that a token refresh
between propose and confirm invalidates the pending proposal — it degrades
to `proposal_expired`, which is the same safe, recoverable outcome as
ordinary TTL expiry, so this is not a defect, just a documented trade-off.

An in-process dict is the simple choice for a single-replica deployment. It
loses pending proposals on restart and is invisible to a second replica
serving the confirm turn — both degrade to `proposal_expired` -> re-propose,
which is safe. If this server ever runs multi-replica, this store moves to
shared storage (e.g. Redis, which the orchestrator already runs) and this
module's public interface is written narrowly enough that the swap should
not touch any caller.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
import hashlib
import secrets
import time
from typing import Any, Literal

from vesta_saved_search.config import PROPOSAL_TTL_SECONDS

#: What a stashed proposal will do when confirmed. Declared as a Literal
#: (rather than left as a bare `str`) so a typo in a call site — `"save "`,
#: `"Save"` — is a mypy error, not a proposal that silently never matches
#: any tool's expected action and always reports `proposal_action_mismatch`.
ProposalAction = Literal["save", "update", "delete"]


def user_key_from_token(token: str) -> str:
    """Derive the proposal store's per-user key from a bearer token.

    A SHA-256 hash, not the token itself: see the module docstring for why
    this store holds no credential material. This function is the one place
    that decision lives, so every caller — `propose_saved_search`,
    `save_search`, `delete_saved_search` — derives the same key the same way
    rather than each hashing (or forgetting to hash) independently.
    """
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


@dataclass
class Proposal:
    """One stashed write, awaiting an explicit confirm.

    Mutable (not frozen), unlike :class:`vesta_saved_search.models.SavedSearchRecord`
    — `mark_consumed` flips `consumed` in place once `save_search` /
    `delete_saved_search` executes it, so a replay of the same `proposalId`
    is recognised as `already_saved` rather than writing a second time.
    """

    proposal_id: str
    user_key: str
    action: ProposalAction
    payload: dict[str, Any]
    #: The name this proposal would save under. Tracked on the proposal
    #: itself (not just inside `payload`) because `previous_name` comparisons
    #: and re-proposal `regeneration_attempts` both need it independent of
    #: whatever shape `payload` happens to take for a given action.
    name: str
    #: The N4 fingerprint of this proposal's criteria — stashed once at
    #: propose time so `save_search`'s live re-check compares against the
    #: SAME fingerprint the user was shown, not a value recomputed from a
    #: payload that (by construction) cannot have changed between propose
    #: and confirm, since there is no field left for the model to resend.
    fingerprint: str
    #: Populated only when a *generated* name changed between this proposal
    #: and the one it replaced — never for a user-stated name, and never for
    #: a frequency-only change. See `propose_saved_search`'s re-proposal
    #: rules; this field is what lets it answer "keep the old name?"
    #: without re-deriving history the store does not otherwise keep.
    previous_name: str | None
    #: How many times naming has already failed and been silently retried
    #: for this proposal's lineage. Two failures triggers the deterministic
    #: fallback name; this counter is what makes that count survive a
    #: re-proposal rather than resetting every call.
    regeneration_attempts: int
    created_at: float
    consumed: bool = False
    #: What to hand back on an idempotent replay of an already-consumed
    #: proposal — e.g. the created record's id — so `save_search` can answer
    #: `already_saved` with something concrete rather than a bare status.
    result: dict[str, Any] | None = field(default=None)


class ProposalStore:
    """One pending proposal per user, short-lived, validated on every read.

    Every property in this class name is load-bearing:

    * **One pending proposal per user.** `put()` supersedes and fully forgets
      any prior proposal for the same `user_key` — not merely stops treating
      it as current, but removes it from lookup entirely. This is what makes
      a bare "yes" unambiguous even when the model or a confused user issues
      it after re-proposing: there is never a *set* of proposals to choose
      the wrong one from.
    * **Short TTL.** Old proposals do not accumulate as attack surface.
    * **Validated against the caller's identity on every read.** `get()`
      returns `None` — identically — for a wrong user, an unknown id, and an
      expired id. There is deliberately no way to distinguish "this proposal
      belongs to someone else" from "this proposal never existed": either
      finding tells an attacker something, and this store tells them
      nothing. See `save_search`'s cross-user test, the single most
      important assertion in this feature.
    """

    def __init__(
        self,
        *,
        ttl_seconds: float = PROPOSAL_TTL_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        # `clock` is injected — not `time.monotonic` called directly — so a
        # test can assert TTL expiry deterministically instead of sleeping
        # for real seconds in a suite that otherwise runs in milliseconds.
        self._ttl_seconds = ttl_seconds
        self._clock = clock
        self._by_id: dict[str, Proposal] = {}
        self._by_user: dict[str, Proposal] = {}
        # Naming-failure counts for a save attempt still in progress -- see
        # `note_naming_failure`. Deliberately NOT expired by the same TTL as
        # proposals: an unresolved naming loop that runs longer than a
        # proposal's TTL is still the same single conversation turn from the
        # user's point of view, and losing the count early would let a
        # generated name mentioning `unsupportedFilters` slip through on a
        # slow third attempt that should have gone to the deterministic
        # fallback on the second.
        self._naming_attempts: dict[tuple[str, str], int] = {}

    def put(
        self,
        user_key: str,
        *,
        action: ProposalAction,
        payload: dict[str, Any],
        name: str,
        fingerprint: str,
        previous_name: str | None = None,
        regeneration_attempts: int = 0,
    ) -> Proposal:
        """Stash a new proposal for `user_key`, superseding any prior one.

        The prior proposal (if any) is removed from BOTH internal indexes —
        not just replaced in `_by_user` — so its id becomes unrecognisable
        immediately. A model that issued the old id before the user's new
        request replaced it gets the same `proposal_expired` a genuinely
        unknown id would, never a stale write.
        """
        prior = self._by_user.get(user_key)
        if prior is not None:
            self._by_id.pop(prior.proposal_id, None)

        proposal = Proposal(
            proposal_id=secrets.token_urlsafe(32),
            user_key=user_key,
            action=action,
            payload=payload,
            name=name,
            fingerprint=fingerprint,
            previous_name=previous_name,
            regeneration_attempts=regeneration_attempts,
            created_at=self._clock(),
        )
        self._by_id[proposal.proposal_id] = proposal
        self._by_user[user_key] = proposal
        return proposal

    def get(self, proposal_id: str, user_key: str) -> Proposal | None:
        """Return the proposal, or `None` for wrong user / unknown / expired id.

        All three failure modes collapse to the same `None` — see the class
        docstring for why that collapse is the point, not an oversight.
        """
        proposal = self._by_id.get(proposal_id)
        if proposal is None:
            return None
        if proposal.user_key != user_key:
            return None
        if self._expired(proposal):
            self._forget(proposal)
            return None
        return proposal

    def current(self, user_key: str) -> Proposal | None:
        """The user's one pending proposal, if any and not expired.

        Used by `propose_saved_search` to decide re-proposal fields
        (`previousName`) against whatever the user was already shown, without
        needing the model to supply the prior proposal's id.
        """
        proposal = self._by_user.get(user_key)
        if proposal is None or self._expired(proposal):
            return None
        return proposal

    def note_naming_failure(self, user_key: str, fingerprint: str) -> int:
        """Record one more naming failure for this user's current save attempt.

        Keyed on `(user_key, fingerprint)`, not on any proposal id — nothing
        is stashed as a `Proposal` yet while naming is still being resolved,
        since `put()` only happens once a name is finally settled. The
        fingerprint identifies "this same underlying save attempt" across
        however many candidate names the model tries, without needing the
        model to carry a counter itself; a *different* fingerprint (the user
        changed criteria) starts a fresh count, which is correct — it is a
        different save attempt.

        Returns the new count so the caller can decide, in one place, when
        to stop asking the model for another name and fall back instead.
        """
        key = (user_key, fingerprint)
        count = self._naming_attempts.get(key, 0) + 1
        self._naming_attempts[key] = count
        return count

    def clear_naming_attempts(self, user_key: str, fingerprint: str) -> None:
        """Forget the naming-failure count once a name is finally settled.

        Called on every successful `put()` for the resolved fingerprint, so a
        later, unrelated save attempt for the SAME criteria (e.g. after a
        prior save completed and was later deleted) starts its own naming
        loop from zero rather than inheriting a stale count.
        """
        self._naming_attempts.pop((user_key, fingerprint), None)

    def mark_consumed(self, proposal: Proposal, *, result: dict[str, Any] | None = None) -> None:
        """Flip a proposal to consumed after `save_search` / `delete_saved_search` executes it.

        Left in both indexes rather than removed: a second confirm of the
        SAME id must still resolve via `get()` so the caller can recognise it
        as a replay and answer `already_saved`, not `proposal_expired` — a
        crashed client retrying its own successful request should never look
        like it needs to start over.
        """
        proposal.consumed = True
        proposal.result = result

    def _expired(self, proposal: Proposal) -> bool:
        return (self._clock() - proposal.created_at) > self._ttl_seconds

    def _forget(self, proposal: Proposal) -> None:
        self._by_id.pop(proposal.proposal_id, None)
        if self._by_user.get(proposal.user_key) is proposal:
            del self._by_user[proposal.user_key]


__all__ = ["Proposal", "ProposalAction", "ProposalStore", "user_key_from_token"]
