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
