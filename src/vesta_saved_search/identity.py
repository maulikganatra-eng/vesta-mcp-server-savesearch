"""Reading the end user's GuestSite bearer token off the MCP ``_meta`` channel.

This is the whole of the server's identity story, and it is deliberately tiny.
Everything downstream — whose saved searches get listed, whose account a write
lands in — derives from the one function below.

Two properties of this module are load-bearing and are argued for at the
extraction point in :func:`bearer_token_from_meta`: ``_meta`` is not
authenticated, and nothing user-specific may be cached.
"""

from __future__ import annotations

from typing import Any

#: The ``_meta`` key the orchestrator puts the end user's GuestSite bearer token on.
#:
#: 🔴 This string is a CONTRACT with the orchestrator — step O3 (VA-386), written in
#: ``capability_executor.py``'s ``_meta`` build in the backend repo. Both sides
#: hard-code it and neither may change it alone. A rename on one side does not fail
#: any build; it silently produces a server that thinks every caller is anonymous.
#:
#: ⚠️ For an anonymous caller the key is **absent entirely** — not present-and-null.
#: The orchestrator applies a drop-falsy filter when building ``_meta``, so there is
#: no key at all rather than a ``None`` value. The ``sign_in_required`` branch keys
#: off absence, and :func:`bearer_token_from_meta` maps both shapes to ``None`` so
#: that a future change on either side cannot turn an anonymous caller into a
#: signed-in one holding a null token.
#:
#: (``noqa: S105`` — bandit sees a name ending in "token" bound to a string literal and
#: assumes a hardcoded credential. This is the *key name* the credential travels under,
#: not the credential. It is not secret, and it has to be identical on both sides.)
META_TOKEN_KEY = "guestsite_bearer_token"  # noqa: S105


def bearer_token_from_meta(ctx: Any) -> str | None:
    """Return the caller's GuestSite bearer token, or ``None`` if there is not one.

    ``None`` means "treat this caller as anonymous". It is never an error: an
    anonymous visitor reaching this server is an ordinary, expected case that the
    tools answer with ``sign_in_required``.

    ⚠️ ``_meta`` IS NOT AUTHENTICATED. This server takes the orchestrator's word
    for who the caller is. There is no signature, no verification, and nothing here
    can tell a real orchestrator from anything else that can reach the port. That is
    acceptable only because both processes sit on a private network and only the
    orchestrator can reach this one. **It is not a security boundary**, and it must
    not be treated as one — if this server ever becomes reachable from outside that
    network, this function is the thing that has to change, not the callers.

    🔴 AND, FOR THE SAME REASON — because this is the point where a request acquires
    an identity — NOTHING USER-SPECIFIC MAY BE CACHED ON MODULE OR SESSION STATE
    ANYWHERE IN THIS SERVER. Not the token, not the identity it resolves to, not
    list results, not saved searches, not "just the last response to save a round
    trip".

    MCP sessions are **pooled and shared across users**: the orchestrator's
    ``pool.py`` round-robins ``MCP_POOL_SIZE`` sessions per URL with no user
    dimension at all, so two consecutive requests on one session are routinely two
    different people. The property-search server's ``mls_client.py`` caches on
    ``(mode, payload)`` with no user dimension, and that is perfectly safe *there* —
    the MLS API is unauthenticated and returns the same ``permission: guest`` data
    to everyone. Copying that pattern into this server would be a cross-tenant leak
    of other people's saved searches, and it would look like a performance win in
    review.

    Args:
        ctx: The FastMCP ``Context`` for the current tool call. Typed loosely
            because the SDK's ``Context`` is generic over its session and lifespan
            types, and this function needs exactly one attribute path off it —
            pinning the full generic here would buy no safety and would couple every
            caller to those parameters.

    Returns:
        The token exactly as the orchestrator sent it, or ``None`` when the caller is
        anonymous, when ``_meta`` is absent, or when the value is blank.
    """
    # Read defensively rather than optimistically. `request_context` raises — it
    # does not return None — when there is no active request, which is the normal
    # state when a tool function is called directly (as a unit test might) instead
    # of through the protocol. An anonymous caller and a missing request context
    # both mean "no identity", so both answer None.
    #
    # Broad `except Exception` is deliberate: every failure mode here has the same
    # correct answer, and the alternative is enumerating the SDK's internal error
    # types and re-breaking on the next SDK release. Narrow it only if a specific
    # exception ever needs to mean something OTHER than "anonymous".
    try:
        meta = ctx.request_context.meta
    except Exception:
        return None

    if meta is None:
        return None

    # `model_extra` holds the keys the MCP spec does not define. `_meta`'s declared
    # field is `progressToken`; everything the orchestrator adds — including our
    # token — arrives here because `RequestParams.Meta` is configured `extra="allow"`.
    # An older or stricter SDK that dropped unknown keys would surface as
    # `model_extra` being empty, not as an exception, hence the explicit check.
    extra = getattr(meta, "model_extra", None)
    if not extra:
        return None

    token = extra.get(META_TOKEN_KEY)

    # A non-string is a caller bug, not an identity. Refusing it here keeps the
    # return type honest for every caller instead of letting, say, an int reach the
    # Authorization header and fail somewhere far away with a confusing message.
    if not isinstance(token, str):
        return None

    # Blank means absent. The orchestrator's drop-falsy filter should already prevent
    # an empty string arriving, so this is defence against a change on that side; it
    # keeps `if token:` and `if token is None:` agreeing for every caller.
    if not token.strip():
        return None

    # Returned VERBATIM — not stripped, not normalised. The token has to reach the
    # upstream API byte-identical, and a server that quietly "cleans up" a credential
    # is a server that can turn a valid token into a 401 nobody can explain.
    return token
