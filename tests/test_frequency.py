"""Unit tests for the frequency mapping, both directions (step N2 / VA-397, GS-8057)."""

from __future__ import annotations

import pytest

from vesta_saved_search.frequency import (
    frequency_to_schedule_interval,
    schedule_pair_to_frequency,
)

pytestmark = pytest.mark.unit


@pytest.mark.parametrize(
    ("frequency", "expected"),
    [("never", 0), ("daily", 1), ("instantly", 2)],
)
def test_forward_map_is_correct(frequency: str, expected: int) -> None:
    assert frequency_to_schedule_interval(frequency) == expected


@pytest.mark.parametrize("bad_value", ["weekly", "", "3", "0", " ", "dailyish"])
def test_forward_map_rejects_anything_out_of_range(bad_value: str) -> None:
    """🔴 Refuse before any HTTP call — the API accepts out-of-range values with a
    silent 200 and stores them as "never". This is the only place that mistake can
    be caught, so it must raise, not clamp or default.
    """
    with pytest.raises(ValueError, match="unknown notification frequency"):
        frequency_to_schedule_interval(bad_value)


@pytest.mark.parametrize(
    ("frequency", "expected"),
    [("Never", 0), ("DAILY", 1), ("Instantly", 2), ("  daily  ", 1)],
)
def test_forward_map_is_case_and_whitespace_insensitive(frequency: str, expected: int) -> None:
    """🔴 Discovered running a real end-to-end scenario: the model naturally
    sends "Daily" (matching the confirmation template's own display casing),
    and a strict lookup rejected it as invalid, costing a wasted retry round
    trip even though the intent was completely unambiguous."""
    assert frequency_to_schedule_interval(frequency) == expected


@pytest.mark.parametrize(
    ("notify", "schedule_id", "expected"),
    [
        # Pre-GS-8670 pairs (verified against dev, August 2026).
        (False, None, "never"),
        (False, 3, "daily"),
        (True, 1, "instantly"),
        # Post-GS-8670 pairs actually RECORDED against QA, 2026-09-10 (see
        # tests/fixtures/guestsite_qa/), after Ignacio Fernandez's API-side fix.
        # Only Daily's `notify` changed.
        (True, 3, "daily"),
        # Synthesized, NOT recorded from any live response -- no fixture has ever
        # shown `notify=True` with `scheduleId=None`. Included because the fix's
        # whole point is that `notify` must not change the answer for a given
        # `scheduleId`, and `None` needs the same guarantee as every other id.
        (True, None, "never"),
    ],
)
def test_backward_map_is_keyed_on_schedule_id_not_notify(
    notify: bool, schedule_id: int | None, expected: str
) -> None:
    """GS-8693/GS-8694: the API changed which `notify` value accompanies a Daily
    schedule (False -> True) without changing `scheduleId`. A map keyed on the
    (notify, scheduleId) *pair* broke the instant that happened -- a Daily search
    started reading back as "unknown" because (True, 3) wasn't a recognised pair.

    `scheduleId` alone has been stable across the fix and uniquely identifies the
    frequency in every fixture recorded from either API version, so the map now
    keys on it alone and treats `notify` as informational, not load-bearing.
    """
    assert schedule_pair_to_frequency(notify, schedule_id) == expected


def test_notify_value_does_not_change_the_answer_for_a_given_schedule_id() -> None:
    """🔴 The regression this fix targets, pinned directly: for the SAME
    scheduleId, flipping `notify` must never flip the reported frequency. This is
    exactly the axis GS-8693/GS-8694 broke on when the API's `notify` value for
    Daily changed out from under a (notify, scheduleId)-pair-keyed map.
    """
    assert schedule_pair_to_frequency(False, 3) == schedule_pair_to_frequency(True, 3) == "daily"
    assert schedule_pair_to_frequency(False, None) == schedule_pair_to_frequency(True, None) == "never"
    assert schedule_pair_to_frequency(False, 1) == schedule_pair_to_frequency(True, 1) == "instantly"


@pytest.mark.parametrize(
    ("notify", "schedule_id"),
    [
        (False, 99),  # an id from no documented frequency
        (True, 2),  # an id from no documented frequency
        (None, 7),  # an id from no documented frequency
    ],
)
def test_unrecognised_schedule_id_is_unknown_never_never(
    notify: bool | None, schedule_id: int | None
) -> None:
    """🔴 An unmapped, non-null scheduleId must answer "unknown", never silently
    "never".

    A schedule id the API introduces later (or returns from an environment this
    codebase hasn't verified) must surface as "we don't know", not quietly report
    a schedule as off. `schedule_id is None` is excluded here deliberately: that
    value has been observed, consistently, to mean "no schedule attached" across
    both the pre- and post-GS-8670 API — see
    `test_backward_map_is_keyed_on_schedule_id_not_notify`.
    """
    assert schedule_pair_to_frequency(notify, schedule_id) == "unknown"
