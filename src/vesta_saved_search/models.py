"""The record shape this client hands back, decoded once and correctly.

One dataclass, built in exactly one place (:func:`SavedSearchRecord.from_api`), so
none of the five traps documented on :mod:`vesta_saved_search.client` can be
half-applied by a caller who forgot one of them.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from typing import Any

from vesta_saved_search.errors import SavedSearchUnexpectedResponseError
from vesta_saved_search.frequency import Frequency, schedule_pair_to_frequency


@dataclass(frozen=True, slots=True)
class SavedSearchRecord:
    """One saved search, with every trap in the raw API record already resolved.

    Every field here is the DECODED value. `search_filters` is a dict, never the
    JSON string the wire sends; `notification_frequency` is the mapped word, never
    the raw `(notify, scheduleId)` pair. A caller of this client should never need
    to look at the raw API record.
    """

    saved_search_id: int
    name: str
    search_mode: str
    search_filters: dict[str, Any]
    search_url: str
    notification_frequency: Frequency
    new_listings_count: int
    created_at: str | None
    last_update: str | None

    @classmethod
    def from_api(cls, raw: dict[str, Any]) -> SavedSearchRecord:
        """Build a record from one element of the API's list/create/update response.

        Resolves three of the five documented traps by construction:

        * **`searchFilters` is a string on read.** Parsed here, once, so every
          caller gets a dict. Sent as an object, comes back as
          `"{\\"area\\":\\"11\\",...}"` — verified directly against dev.
        * **`consumerId` is injected inside stored `searchFilters`.** Left in the
          decoded dict rather than stripped — verified that echoing it back on
          write is harmless — but callers computing a fingerprint (step N4) must
          drop it themselves; stripping it here would hide from N4 the exact key
          it has to know to exclude.
        * **The `(notify, scheduleId)` pair, never either field alone**, via
          :func:`schedule_pair_to_frequency`.

        Raises :class:`SavedSearchUnexpectedResponseError` if `searchFilters` is present
        but is not parseable JSON — a malformed record should be visible as an
        error, not silently produce an empty dict that then makes every filter
        look absent. The same is true of every other required field below: a
        record missing `savedSearchId`, `searchName`, `searchType` or `searchUrl`
        raises the same typed error rather than a bare `KeyError`, which is not a
        `SavedSearchApiError` and would otherwise escape the tool's
        `except SavedSearchApiError` as a raw crash instead of a clean error
        envelope.
        """
        raw_filters = raw.get("searchFilters")
        search_filters: dict[str, Any]
        if raw_filters is None:
            search_filters = {}
        elif isinstance(raw_filters, dict):
            # Not observed from the real API, but accepted defensively -- a caller
            # constructing a record from an already-decoded dict (e.g. a test
            # fixture written by hand) should not be forced through a re-encode.
            search_filters = raw_filters
        elif isinstance(raw_filters, str):
            try:
                parsed = json.loads(raw_filters)
            except json.JSONDecodeError as exc:
                raise SavedSearchUnexpectedResponseError(
                    f"searchFilters for savedSearchId={raw.get('savedSearchId')!r} "
                    f"was not valid JSON: {raw_filters!r}"
                ) from exc
            if not isinstance(parsed, dict):
                raise SavedSearchUnexpectedResponseError(
                    f"searchFilters for savedSearchId={raw.get('savedSearchId')!r} "
                    f"decoded to a {type(parsed).__name__}, expected an object"
                )
            search_filters = parsed
        else:
            raise SavedSearchUnexpectedResponseError(
                f"searchFilters for savedSearchId={raw.get('savedSearchId')!r} "
                f"had an unexpected type: {type(raw_filters).__name__}"
            )

        try:
            saved_search_id = raw["savedSearchId"]
            name = raw["searchName"]
            search_mode = raw["searchType"]
            search_url = raw["searchUrl"]
        except KeyError as exc:
            # A bare KeyError is not a SavedSearchApiError, so it would escape
            # tools.py's `except SavedSearchApiError` as a raw crash instead of the
            # clean {"status": "error", ...} envelope every other failure path in
            # this client produces. Re-raised as the same typed error the
            # searchFilters decoding above already uses, naming which key was
            # missing rather than swallowing it into a generic message.
            raise SavedSearchUnexpectedResponseError(
                f"record is missing required key {exc.args[0]!r}: {raw!r}"
            ) from exc

        return cls(
            saved_search_id=saved_search_id,
            name=name,
            search_mode=search_mode,
            search_filters=search_filters,
            search_url=search_url,
            notification_frequency=schedule_pair_to_frequency(raw.get("notify"), raw.get("scheduleId")),
            new_listings_count=raw.get("newListingsSinceCertainDate") or 0,
            created_at=raw.get("createdDate"),
            last_update=raw.get("lastUpdate"),
        )
