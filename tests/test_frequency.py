"""Unit tests for the frequency mapping, both directions (step N2 / VA-397)."""

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
        (False, None, "never"),
        (False, 3, "daily"),
        (True, 1, "instantly"),
    ],
)
def test_backward_map_matches_verified_dev_pairs(
    notify: bool, schedule_id: int | None, expected: str
) -> None:
    """These three pairs are recorded straight from dev, not invented.

    See tests/fixtures/guestsite_dev/create_daily.json (False, 3) and
    create_instantly.json (True, 1).
    """
    assert schedule_pair_to_frequency(notify, schedule_id) == expected


def test_notify_false_does_not_mean_never() -> None:
    """🔴 The single most important row in the backward map.

    `notify` is False for BOTH never and daily. Reading it alone would tell a user
    with a daily saved search that their notifications are off — a plainly wrong
    statement about their own data.
    """
    # The two assertions below are the real check: they pin never and daily to two
    # distinct literal strings. A version that collapsed them to the same value
    # would fail here directly, so there is no separate `!=` to add on top.
    assert schedule_pair_to_frequency(False, None) == "never"
    assert schedule_pair_to_frequency(False, 3) == "daily"


@pytest.mark.parametrize(
    ("notify", "schedule_id"),
    [
        (True, None),  # notify=True but no schedule at all -- inconsistent
        (True, 3),  # instantly's notify with daily's schedule id
        (False, 1),  # never/daily's notify with instantly's schedule id
        (None, None),  # a record type this API has never been observed to return
        (False, 99),  # an id from neither documented pair
    ],
)
def test_unrecognised_pair_is_unknown_never_never(notify: bool | None, schedule_id: int | None) -> None:
    """🔴 An unmapped pair must answer "unknown", never silently "never".

    If the API's Daily/notify=False quirk is ever fixed upstream, (False, 3) stops
    occurring and (True, 3) starts. A backward map that defaulted unmapped pairs to
    "never" would then silently misreport every daily search as off, right at the
    moment the underlying bug it was compensating for went away.
    """
    assert schedule_pair_to_frequency(notify, schedule_id) == "unknown"
