"""Typed, read-once environment configuration for this server.

One place, following the same pattern as the orchestrator repo's
`orchestrator/shared/config.py`: every env var this server reads gets one named
constant here, read at import time, rather than scattered `os.getenv` calls a
reader has to hunt down individually.
"""

from __future__ import annotations

import os

#: Base URL of the GuestSite SavedSearches API, with NO trailing path segment
#: (the client appends `/SavedSearches...` itself). Defaults to dev, matching this
#: repo's current dev-only testing constraint — production deploys must override
#: this explicitly, not rely on the default silently pointing at dev.
GUESTSITE_SAVED_SEARCH_BASE_URL: str = os.getenv(
    "GUESTSITE_SAVED_SEARCH_BASE_URL", "https://www.dev.themls.com/GuestSiteApi"
)

#: How long a `propose_saved_search` proposal survives before `save_search` /
#: `delete_saved_search` must answer `proposal_expired` (step N5 / VA-401).
#:
#: Short by design: the proposal store is the only server-side state this
#: feature introduces, and therefore the only place a cross-user leak can be
#: created (see `vesta_saved_search.proposals`). A short TTL bounds how long a
#: stale proposal exists to be attacked, and an expired proposal degrades to
#: the safe, recoverable "re-propose" path rather than any kind of failure.
PROPOSAL_TTL_SECONDS: float = float(os.getenv("SAVED_SEARCH_PROPOSAL_TTL_SECONDS", "300"))

#: Upper bound on a saved search's display name, enforced by
#: `propose_saved_search` before a name is ever sent upstream. GuestSite
#: itself does not document a limit; this exists so a generated name has a
#: concrete length to fail against rather than an open-ended one nothing ever
#: catches until a UI truncates it awkwardly.
SAVED_SEARCH_NAME_MAX_LENGTH: int = int(os.getenv("SAVED_SEARCH_NAME_MAX_LENGTH", "60"))

#: Base URL of the property-search MCP server's plain-HTTP side, WITHOUT the
#: `/internal/es-query` path (the client appends it) — step S4 / VA-403.
#: Server-to-server only: this server calls it as an HTTP client, never as an
#: MCP tool, and no LLM is ever on this path. Defaults to the local dev port
#: (6000) that server's own `docker-compose.local.yml` and `server.py` both
#: bind to — the two servers are meant to run side by side, one MCP host
#: apart (this one is 6001). Production deploys must override this to the
#: real internal service address; the default silently pointing at
#: localhost is deliberate for the same reason
#: `GUESTSITE_SAVED_SEARCH_BASE_URL` defaults to dev.
PROPERTY_SEARCH_INTERNAL_URL: str = os.getenv("PROPERTY_SEARCH_INTERNAL_URL", "http://localhost:6000")
