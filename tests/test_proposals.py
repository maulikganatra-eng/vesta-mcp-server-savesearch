"""Unit tests for ProposalStore (steps N5/N6/N9 / VA-401, VA-402, VA-406).

Level 1. The cross-user isolation properties asserted here are re-verified at
the protocol level in `tests/test_component_save_flow.py` — both are wanted:
this file proves the store's own invariants in isolation, that file proves
they survive being wired into a real tool over a real MCP session.
"""

from __future__ import annotations

from collections.abc import Callable

import pytest

from vesta_saved_search.proposals import ProposalStore, user_key_from_token

pytestmark = pytest.mark.unit

TOKEN_A = "token-for-user-a"
TOKEN_B = "token-for-user-b"


def _clock(times: list[float]) -> Callable[[], float]:
    it = iter(times)

    def clock() -> float:
        return next(it)

    return clock


def test_user_key_is_a_hash_not_the_token_itself() -> None:
    key = user_key_from_token(TOKEN_A)
    assert key != TOKEN_A
    assert len(key) == 64  # sha256 hex digest length
    int(key, 16)


def test_user_key_is_deterministic_and_distinguishes_users() -> None:
    assert user_key_from_token(TOKEN_A) == user_key_from_token(TOKEN_A)
    assert user_key_from_token(TOKEN_A) != user_key_from_token(TOKEN_B)


def test_put_then_get_round_trips() -> None:
    store = ProposalStore()
    user_key = user_key_from_token(TOKEN_A)
    proposal = store.put(
        user_key, action="save", payload={"x": 1}, name="Del Mar Homes", fingerprint="fp1"
    )
    fetched = store.get(proposal.proposal_id, user_key)
    assert fetched is proposal


def test_get_returns_none_for_a_wrong_user() -> None:
    store = ProposalStore()
    key_a = user_key_from_token(TOKEN_A)
    key_b = user_key_from_token(TOKEN_B)
    proposal = store.put(key_a, action="save", payload={}, name="A-Malibu", fingerprint="fp-a")
    assert store.get(proposal.proposal_id, key_b) is None


def test_get_returns_none_for_an_unknown_id() -> None:
    store = ProposalStore()
    assert store.get("does-not-exist", user_key_from_token(TOKEN_A)) is None


def test_second_proposal_for_the_same_user_removes_the_first() -> None:
    """N5 unit test list: 'issuing a second proposal for the same user removes the first.'"""
    store = ProposalStore()
    user_key = user_key_from_token(TOKEN_A)
    first = store.put(user_key, action="save", payload={}, name="First", fingerprint="fp1")
    second = store.put(user_key, action="save", payload={}, name="Second", fingerprint="fp2")

    assert store.get(first.proposal_id, user_key) is None
    assert store.get(second.proposal_id, user_key) is second


def test_current_returns_the_one_pending_proposal() -> None:
    store = ProposalStore()
    user_key = user_key_from_token(TOKEN_A)
    assert store.current(user_key) is None
    proposal = store.put(user_key, action="save", payload={}, name="X", fingerprint="fp")
    assert store.current(user_key) is proposal


def test_ttl_expiry_via_injected_clock() -> None:
    # created_at reads 0.0; get() reads 100.0 -- past a 10s TTL.
    store = ProposalStore(ttl_seconds=10.0, clock=_clock([0.0, 100.0, 100.0]))
    user_key = user_key_from_token(TOKEN_A)
    proposal = store.put(user_key, action="save", payload={}, name="X", fingerprint="fp")
    assert store.get(proposal.proposal_id, user_key) is None
    assert store.current(user_key) is None


def test_not_yet_expired_is_still_retrievable() -> None:
    store = ProposalStore(ttl_seconds=100.0, clock=_clock([0.0, 5.0]))
    user_key = user_key_from_token(TOKEN_A)
    proposal = store.put(user_key, action="save", payload={}, name="X", fingerprint="fp")
    assert store.get(proposal.proposal_id, user_key) is proposal


def test_mark_consumed_keeps_the_proposal_retrievable_for_replay_detection() -> None:
    store = ProposalStore()
    user_key = user_key_from_token(TOKEN_A)
    proposal = store.put(user_key, action="save", payload={}, name="X", fingerprint="fp")
    store.mark_consumed(proposal, result={"savedSearchId": 42})

    fetched = store.get(proposal.proposal_id, user_key)
    assert fetched is not None
    assert fetched.consumed is True
    assert fetched.result == {"savedSearchId": 42}


def test_note_naming_failure_increments_per_user_and_fingerprint() -> None:
    store = ProposalStore()
    user_key = user_key_from_token(TOKEN_A)
    assert store.note_naming_failure(user_key, "fp1") == 1
    assert store.note_naming_failure(user_key, "fp1") == 2


def test_note_naming_failure_is_independent_per_fingerprint() -> None:
    """A different fingerprint is a different underlying save attempt."""
    store = ProposalStore()
    user_key = user_key_from_token(TOKEN_A)
    assert store.note_naming_failure(user_key, "fp1") == 1
    assert store.note_naming_failure(user_key, "fp2") == 1


def test_note_naming_failure_is_independent_per_user() -> None:
    store = ProposalStore()
    key_a = user_key_from_token(TOKEN_A)
    key_b = user_key_from_token(TOKEN_B)
    assert store.note_naming_failure(key_a, "fp1") == 1
    assert store.note_naming_failure(key_b, "fp1") == 1


def test_clear_naming_attempts_resets_the_count() -> None:
    store = ProposalStore()
    user_key = user_key_from_token(TOKEN_A)
    store.note_naming_failure(user_key, "fp1")
    store.note_naming_failure(user_key, "fp1")
    store.clear_naming_attempts(user_key, "fp1")
    assert store.note_naming_failure(user_key, "fp1") == 1


def test_action_and_previous_name_are_stashed() -> None:
    store = ProposalStore()
    user_key = user_key_from_token(TOKEN_A)
    proposal = store.put(
        user_key,
        action="delete",
        payload={"savedSearchId": 7},
        name="Old Name",
        fingerprint="fp",
        previous_name="Even Older Name",
        regeneration_attempts=2,
    )
    assert proposal.action == "delete"
    assert proposal.previous_name == "Even Older Name"
    assert proposal.regeneration_attempts == 2
