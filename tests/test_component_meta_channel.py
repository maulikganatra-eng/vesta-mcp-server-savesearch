"""🔴 The test N1 exists for: the token crosses ``_meta`` over the real protocol.

Level 2b. A real :class:`~mcp.ClientSession` talks to a real :class:`FastMCP` app
in-process, with one throwaway tool that reports whether it saw a token.

**Why this cannot be a unit test.** ``tests/test_identity.py`` calls the reader with a
hand-built fake context, which proves the reader's branching and nothing else. The
``_meta`` channel itself is made of things that only exist at the protocol layer: the
client serialising ``meta=`` into ``CallToolRequestParams.meta``, the SDK accepting
unknown keys there because ``RequestParams.Meta`` is configured ``extra="allow"``, and
the server surfacing them on ``model_extra``. Any of those could change under us and
every unit test would still pass while the feature was dead.

The tool is defined **inside** each test rather than shipped in ``src/`` — a
production tool that exists only to echo credentials back would be a bad thing to
have in the wheel, and :func:`~vesta_saved_search.server.create_app` is a factory
precisely so this is possible.
"""

from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import Context
from mcp.shared.memory import create_connected_server_and_client_session
import pytest

from vesta_saved_search.identity import META_TOKEN_KEY, bearer_token_from_meta
from vesta_saved_search.server import create_app

pytestmark = pytest.mark.component

# Obviously fake, and shaped like a JWT so nothing is tempted to "fix" it later.
# Never put a real bearer value in a test or a fixture.
#
# The trailing segments of this and OTHER_FAKE_TOKEN are deliberately different: the
# assertions compare the last four characters, and a first draft ended both with
# ".signature", so the two were indistinguishable and the bleed test could not have
# failed. There is a guard asserting they differ, so that cannot silently return.
FAKE_TOKEN = "eyJhbGciOiJIUzI1NiJ9.not-a-real-token.user-a-sig-1111"
OTHER_FAKE_TOKEN = "eyJhbGciOiJIUzI1NiJ9.a-different-fake-token.user-b-sig-2222"

# The bleed tests compare four-character tails, so two fakes with equal tails would
# make them pass unconditionally. Assert it once, at import, rather than in each test.
assert FAKE_TOKEN[-4:] != OTHER_FAKE_TOKEN[-4:], "the two fake tokens must be distinguishable"

TOOL_NAME = "echo_token_presence"


def _app_with_echo_tool() -> Any:
    """An app carrying one tool that reports what it saw on ``_meta``.

    It returns a *fingerprint* of the token (whether one arrived, its length, its last
    four characters) rather than the token itself. Even in a test, a tool that echoes a
    whole credential is a pattern worth not establishing — and the fingerprint is
    enough to prove the value survived the round trip intact.
    """
    app = create_app()

    @app.tool(name=TOOL_NAME)
    async def echo_token_presence(ctx: Context) -> dict[str, Any]:  # type: ignore[type-arg]
        token = bearer_token_from_meta(ctx)
        return {
            "saw_token": token is not None,
            "length": len(token) if token else 0,
            "tail": token[-4:] if token else None,
        }

    return app


def _payload(result: Any) -> dict[str, Any]:
    """Pull the tool's dict out of a ``CallToolResult``, whatever the SDK wrapped it in.

    Read ``structuredContent`` when present and fall back to parsing the text block.
    Asserting on only one of the two would make this test fail on an SDK change that
    did not actually break the ``_meta`` channel, which is the single thing it is here
    to check.
    """
    structured = getattr(result, "structuredContent", None)
    if isinstance(structured, dict):
        return structured
    import json

    return dict(json.loads(result.content[0].text))


async def test_token_on_meta_reaches_the_tool() -> None:
    """A token put on ``_meta`` by the client is readable inside the tool."""
    async with create_connected_server_and_client_session(_app_with_echo_tool()) as client:
        result = await client.call_tool(TOOL_NAME, {}, meta={META_TOKEN_KEY: FAKE_TOKEN})

    payload = _payload(result)
    assert payload["saw_token"] is True
    # Length and tail together prove the value arrived intact, not merely that some
    # truthy thing was present.
    assert payload["length"] == len(FAKE_TOKEN)
    assert payload["tail"] == FAKE_TOKEN[-4:]


async def test_no_meta_at_all_reads_as_anonymous() -> None:
    """The ordinary anonymous case: no ``meta=`` on the call at all.

    This is what an anonymous visitor's request looks like once the orchestrator's
    drop-falsy filter has removed the absent token, and it must be a clean "no
    identity" rather than an error.
    """
    async with create_connected_server_and_client_session(_app_with_echo_tool()) as client:
        result = await client.call_tool(TOOL_NAME, {})

    payload = _payload(result)
    assert payload["saw_token"] is False
    assert payload["tail"] is None


async def test_meta_present_without_our_key_reads_as_anonymous() -> None:
    """``_meta`` carrying only unrelated keys is still anonymous.

    ``progressToken`` is a real MCP field the orchestrator may well set on its own.
    Its presence must not be mistaken for an identity.
    """
    async with create_connected_server_and_client_session(_app_with_echo_tool()) as client:
        result = await client.call_tool(TOOL_NAME, {}, meta={"progressToken": "p-1"})

    payload = _payload(result)
    assert payload["saw_token"] is False


async def test_a_second_call_without_a_token_does_not_inherit_the_first_one() -> None:
    """🔴 The cross-user leak, reduced to its smallest reproducible form.

    Two calls on **one session**: the first signed in, the second anonymous. The
    second must see nothing.

    This is exactly the shape of the real failure, because the orchestrator's
    ``pool.py`` round-robins a fixed set of MCP sessions across all users with no user
    dimension — so two consecutive calls on one session are routinely two different
    people. If anything in this server ever caches identity on module or session
    state, this test is what fails, and it fails here rather than in production as one
    user reading another's saved searches.
    """
    async with create_connected_server_and_client_session(_app_with_echo_tool()) as client:
        signed_in = _payload(await client.call_tool(TOOL_NAME, {}, meta={META_TOKEN_KEY: FAKE_TOKEN}))
        anonymous = _payload(await client.call_tool(TOOL_NAME, {}))

    assert signed_in["saw_token"] is True
    assert anonymous["saw_token"] is False, (
        "the anonymous call inherited the previous caller's identity — "
        "something is caching user-specific state across requests"
    )


async def test_two_different_tokens_on_one_session_do_not_bleed() -> None:
    """The same leak, both directions: user B must not be served user A's identity."""
    async with create_connected_server_and_client_session(_app_with_echo_tool()) as client:
        first = _payload(await client.call_tool(TOOL_NAME, {}, meta={META_TOKEN_KEY: FAKE_TOKEN}))
        second = _payload(await client.call_tool(TOOL_NAME, {}, meta={META_TOKEN_KEY: OTHER_FAKE_TOKEN}))

    assert first["tail"] == FAKE_TOKEN[-4:]
    assert second["tail"] == OTHER_FAKE_TOKEN[-4:], (
        "the second caller was served the first caller's identity"
    )
