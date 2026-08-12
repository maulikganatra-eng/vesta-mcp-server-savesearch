"""Typed errors for the GuestSite SavedSearches API client (step N2 / VA-397).

One rule drives this whole module: **an upstream error must surface as an error,
never as a success status with no write behind it.** A bare `requests`/`httpx`
exception, or a 200-with-null-body treated as success, would let a tool answer
"saved!" when nothing was saved. Every failure the client can observe becomes one
of these, so a tool can turn it into an honest "couldn't do that right now"
without inspecting HTTP internals itself.
"""

from __future__ import annotations


class SavedSearchApiError(Exception):
    """Base class for every error this client can raise."""


class SavedSearchNameExistsError(SavedSearchApiError):
    """The upstream rejected a create because the name is already taken.

    Verified: creating with an existing name returns HTTP 400 with the body
    ``{"status": "exists", "savedSearch": None}``, and writes nothing. This is a
    free backstop — it keys on name only, so even a wrong client-side collision
    check cannot produce an actual duplicate name upstream.
    """


class SavedSearchInvalidRequestError(SavedSearchApiError):
    """The upstream rejected the request for a reason other than a name collision.

    Covers the plain-string 400 bodies — e.g. ``"EsQuery cannot be null"`` — which
    is a completely different shape from the `{"status": "exists", ...}` body above
    and must not be parsed as if it were one.
    """


class SavedSearchUpstreamError(SavedSearchApiError):
    """A 5xx, a timeout, or any other failure that is not the caller's fault.

    Deliberately one bucket rather than several: every one of these must become
    the same "couldn't do that right now" to a tool, and there is no case here
    where the caller could have done anything differently.
    """


class SavedSearchUnexpectedResponseError(SavedSearchApiError):
    """The upstream returned 200, but the body did not have the shape we rely on.

    This is its own class, separate from :class:`SavedSearchUpstreamError`,
    because a 200 with a shape we cannot parse is a contract change worth
    surfacing distinctly from a plain outage — treating it identically to a 5xx
    would silence exactly the signal that a mapping this file depends on drifted.
    """
