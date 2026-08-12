"""Shared HTTP-transport error mapping for this server's outbound HTTP clients.

Both :class:`~vesta_saved_search.client.SavedSearchClient` (talking to
GuestSite) and :class:`~vesta_saved_search.es_query_client.EsQueryClient`
(talking to property-search's internal esQuery endpoint) need the exact same
mapping from httpx's transport-level exceptions to this server's typed
errors — a timeout or connection failure means the same thing ("couldn't do
that right now") no matter which upstream it was talking to. Factored out
once so the two clients cannot drift on this independently, the way they
had before this module existed.
"""

from __future__ import annotations

from collections.abc import Awaitable

import httpx

from vesta_saved_search.errors import SavedSearchUpstreamError


async def request_or_upstream_error(
    request: Awaitable[httpx.Response], *, description: str
) -> httpx.Response:
    """Await `request`, mapping transport-level failures to `SavedSearchUpstreamError`.

    Covers only "the request never got a response at all" — a timeout or a
    connection-level failure. Status-code handling on a real response stays
    with each caller, since the two upstreams' response shapes (and which
    codes mean what) differ meaningfully and are not safe to share.
    """
    try:
        return await request
    except httpx.TimeoutException as exc:
        raise SavedSearchUpstreamError(f"{description} timed out") from exc
    except httpx.RequestError as exc:
        raise SavedSearchUpstreamError(f"{description} failed: {exc}") from exc


__all__ = ["request_or_upstream_error"]
