"""MCP tools exposed by this server. First one: `list_saved_searches` (step N3).

Registering a tool is the job of :func:`register_tools`, called once from
:func:`vesta_saved_search.server.create_app`. Kept in its own module, separate
from `server.py`'s transport/health-route concerns, so a test can register these
tools on a bare `FastMCP` instance without going through the full app factory.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context, FastMCP

from vesta_saved_search.client import SavedSearchClient
from vesta_saved_search.errors import SavedSearchApiError
from vesta_saved_search.identity import bearer_token_from_meta
from vesta_saved_search.models import SavedSearchRecord

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


def register_tools(app: FastMCP, client: SavedSearchClient) -> None:
    """Register every tool this server exposes onto `app`.

    Takes the client as a parameter, rather than constructing one internally, so
    tests can register these tools against a client built on `httpx.MockTransport`
    without this module knowing tests exist — the same pattern
    `vesta_saved_search.server.create_app` uses for the whole app.
    """

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


__all__ = ["register_tools"]
