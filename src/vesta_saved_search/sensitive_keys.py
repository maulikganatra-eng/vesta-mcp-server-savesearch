"""A copy of the orchestrator's `_SENSITIVE_KEY_SUBSTRINGS`, kept in sync by hand.

⚠️ This is a MANUAL COPY of a constant defined in the orchestrator repo
(`orchestrator/application/capability_executor.py`), not an import — this server
and the orchestrator are separate deployments with no shared package between them.

Why it matters here: `capability_executor.py`'s `_strip_sensitive` deletes any
response key whose lowercased name contains one of these substrings, **silently**,
before the SSE payload reaches the client. A field on this server's tool output
named e.g. `authorization` or `bearerToken` would vanish with no error anywhere —
the tool would look like it worked and the field would just be gone.

`test_sensitive_keys.py` loops over this exact tuple against every field name this
server's tools emit, so a substring added on the orchestrator side without a
matching update here is caught by that test, not discovered in production.
"""

from __future__ import annotations

#: Verbatim copy of `capability_executor.py`'s `_SENSITIVE_KEY_SUBSTRINGS`, as of
#: VA-390 (O5). If the orchestrator side changes this tuple, this one must be
#: updated to match — there is no automated check across the two repos for this
#: constant (unlike the `remember`/envelope shape, which has a shared-fixture sync
#: script; a short list of string literals does not currently have an equivalent).
SENSITIVE_KEY_SUBSTRINGS: tuple[str, ...] = (
    "access_token",
    "id_token",
    "auth_token",
    "refresh_token",
    "bearer",
    "secret",
    "password",
    "api_key",
    "apikey",
    "credential",
    "authorization",
    "auth_",
)


def contains_sensitive_substring(key: str) -> bool:
    """Match the orchestrator's exact check: a case-insensitive substring test."""
    lowered = key.lower()
    return any(substring in lowered for substring in SENSITIVE_KEY_SUBSTRINGS)
