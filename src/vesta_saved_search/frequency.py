"""The notification-frequency mapping, both directions (step N2 / VA-397).

This is its own module, separate from the client, because the two directions have
different failure modes and both are easy to get backwards under time pressure:

* **Forward (write)**: only three integers mean anything, and the service will not
  tell you if you send a fourth. Silence, not an error, is the failure to guard
  against — so this side validates and raises *before* any HTTP call is made.
* **Backward (read)**: the service decomposes one written value into a PAIR of
  fields on read, and reading either one alone lies about the other. This side
  exists to make reading a single field structurally impossible.
"""

from __future__ import annotations

from typing import Final, Literal

Frequency = Literal["never", "daily", "instantly", "unknown"]

#: Forward map, write side. These are the ONLY three values the service understands.
#:
#: 🔴 Verified directly against dev: `scheduleInterval` values outside {0, 1, 2} are
#: accepted with HTTP 200 and silently stored exactly like `0` — `notify: false`,
#: `scheduleId: null`. No error, no warning, no rejected field. The failure this
#: produces: a user confirms "notify me daily", is told the search was saved, and
#: never receives anything. Nothing in the response distinguishes that success from
#: this failure, so a *user* finds it, not us.
#:
#: This map is unreachable from a bad value today because
#: :func:`frequency_to_schedule_interval` raises before any HTTP call for anything
#: not in this dict. It becomes reachable the moment somebody adds a fourth
#: frequency, or maps a value through an integer that came from somewhere else —
#: which is exactly why the raise exists rather than a comment asking people to
#: remember.
_FREQUENCY_TO_SCHEDULE_INTERVAL: Final[dict[str, int]] = {
    "never": 0,
    "daily": 1,
    "instantly": 2,
}

#: Backward map, read side. `scheduleInterval` is write-only and is never returned
#: by any read; `scheduleId` is the durable identifier of *which* schedule is
#: attached, and is authoritative on its own for identifying never/daily/instantly.
#: `notify` is accepted alongside it but NOT required to match a specific value —
#: see the GS-8670 history below for why.
#:
#: Verified directly against dev in August 2026, by creating one record per
#: frequency and reading it back:
#:
#: | Sent (scheduleInterval) | `notify` back | `scheduleId` back |
#: |---|---|---|
#: | 0 (never)     | False | None |
#: | 1 (daily)     | False | 3    |
#: | 2 (instantly) | True  | 1    |
#:
#: 🔴 GS-8670 (fixed on the GuestSite API/UI side, closed 2026-09-07): re-verified
#: directly against QA on 2026-09-10, the SAME `scheduleInterval` values now read
#: back as:
#:
#: | Sent (scheduleInterval) | `notify` back | `scheduleId` back |
#: |---|---|---|
#: | 0 (never)     | False | None |
#: | 1 (daily)     | **True**  | 3    |
#: | 2 (instantly) | True  | 1    |
#:
#: Only the Daily row's `notify` flipped from False to True — exactly the fragile
#: spot this module's docstring warned about before the fix ever happened. Any
#: environment could be running either API version depending on deploy timing, so
#: this function must accept BOTH the pre-fix and post-fix `notify` value for
#: Daily. `scheduleId` is the field that stayed stable across the fix and is what
#: this map keys on; `notify` is otherwise ignored on read (it is never `false` for
#: an active schedule anymore, but was for the pre-fix Daily case, so it cannot be
#: asserted equal to any particular value without reintroducing GS-8693/GS-8694).
_SCHEDULE_ID_TO_FREQUENCY: Final[dict[int | None, Frequency]] = {
    None: "never",
    3: "daily",
    1: "instantly",
}


def frequency_to_schedule_interval(frequency: str) -> int:
    """Map a frequency name to the integer the API's `scheduleInterval` expects.

    Raises ``ValueError`` for anything not in {"never", "daily", "instantly"} —
    deliberately, and before any HTTP call is made. See the trap documented on
    :data:`_FREQUENCY_TO_SCHEDULE_INTERVAL`: the API answers a bad value with a
    silent 200, so refusing it here is the only place this mistake can be caught.

    🔴 Matched case-insensitively, discovered running a real end-to-end
    scenario: the model naturally sends `"Daily"` (matching the confirmation
    template's own display casing, "Never|Daily|Instantly"), and a strict
    lookup rejected it as `invalid` even though the intent was completely
    unambiguous — costing a wasted round trip while the model retried with
    lowercase. This is the single validating chokepoint every write path
    (propose, save, update) already goes through, so normalising here once
    means every caller benefits without having to remember to do it first.
    """
    try:
        return _FREQUENCY_TO_SCHEDULE_INTERVAL[frequency.strip().casefold()]
    except KeyError:
        allowed = ", ".join(sorted(_FREQUENCY_TO_SCHEDULE_INTERVAL))
        raise ValueError(
            f"unknown notification frequency {frequency!r}; must be one of: {allowed}. "
            "Refusing rather than sending it: the API accepts out-of-range "
            "scheduleInterval values with HTTP 200 and silently treats them as "
            "'never', with no error to catch here later."
        ) from None


def schedule_pair_to_frequency(notify: bool | None, schedule_id: int | None) -> Frequency:
    """Map the `(notify, scheduleId)` pair read back from the API to a frequency.

    Keeps the `(notify, scheduleId)` signature everywhere this is called from
    (`models.py`'s `from_api`) for a stable call site, but keys the actual
    decision on `scheduleId` alone — see GS-8670 in the module docstring for why
    `notify` cannot be trusted to hold a fixed value for a given frequency across
    API versions. `notify` is accepted but intentionally unused: it is documented
    input, not a silently-dropped parameter.

    An unrecognised `scheduleId` returns ``"unknown"`` rather than defaulting to
    ``"never"`` — a schedule id the API introduces later (or one from an
    environment this codebase hasn't verified) must surface as "we don't know",
    never quietly report as "off".
    """
    del notify  # documented as read-side input; not used in the decision — see docstring
    return _SCHEDULE_ID_TO_FREQUENCY.get(schedule_id, "unknown")
