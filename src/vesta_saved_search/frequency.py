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

#: Backward map, read side, keyed on the PAIR — never on `notify` or `scheduleId`
#: alone. `scheduleInterval` is write-only and is never returned by any read.
#:
#: Verified directly against dev, by creating one record per frequency and reading
#: it back:
#:
#: | Sent (scheduleInterval) | `notify` back | `scheduleId` back |
#: |---|---|---|
#: | 0 (never)     | False | None |
#: | 1 (daily)     | False | 3    |
#: | 2 (instantly) | True  | 1    |
#:
#: 🔴 `notify` is `False` for BOTH never and daily. A reader that used `notify`
#: alone to answer "is this on" would tell a user with a daily saved search that
#: their notifications are off — a plainly wrong statement about their own data.
#: `scheduleId` alone happens to be sufficient today, but these are opaque server
#: ids, and building on one field to carry a two-field concept is fragile. Map over
#: the pair, in this one place, so nothing else in the codebase is tempted to read
#: either field alone.
_NOTIFY_SCHEDULE_TO_FREQUENCY: Final[dict[tuple[bool | None, int | None], Frequency]] = {
    (False, None): "never",
    (False, 3): "daily",
    (True, 1): "instantly",
}


def frequency_to_schedule_interval(frequency: str) -> int:
    """Map a frequency name to the integer the API's `scheduleInterval` expects.

    Raises ``ValueError`` for anything not in {"never", "daily", "instantly"} —
    deliberately, and before any HTTP call is made. See the trap documented on
    :data:`_FREQUENCY_TO_SCHEDULE_INTERVAL`: the API answers a bad value with a
    silent 200, so refusing it here is the only place this mistake can be caught.
    """
    try:
        return _FREQUENCY_TO_SCHEDULE_INTERVAL[frequency]
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

    An unrecognised pair returns ``"unknown"`` rather than defaulting to
    ``"never"``. See the module docstring's open question about why Daily reads
    back as ``notify=False`` — if that is ever "fixed" upstream, `(False, 3)` will
    stop occurring and this function must not have quietly been treating unmapped
    pairs as "never" the whole time, which would misreport a schedule as off.
    """
    return _NOTIFY_SCHEDULE_TO_FREQUENCY.get((notify, schedule_id), "unknown")
