"""FastMCP entry point for the saved-search MCP server.

Run it:

    python -m vesta_saved_search.server          # streamable-http on :6001/mcp
    MCP_TRANSPORT=stdio python -m vesta_saved_search.server

Environment variables:

    MCP_TRANSPORT  "streamable-http" (default) or "stdio" or "sse"
    MCP_HOST       bind host for HTTP transports (default: 0.0.0.0)
    MCP_PORT       bind port for HTTP transports (default: 6001)
    LOG_LEVEL      DEBUG / INFO / WARNING / ERROR (default: INFO)

Why 6001 and not 6000: the property-search server owns 6000 and these two run side
by side. The Dockerfile's ``EXPOSE`` says 6001 for the same reason, and
``scripts/check_python_version.py``-style drift between the two would be a container
that starts and serves nothing, so keep them equal.
"""

from __future__ import annotations

import logging
import os
from typing import TYPE_CHECKING, Literal, cast

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

if TYPE_CHECKING:  # pragma: no cover - import-time only, for type checking
    from starlette.requests import Request

logger = logging.getLogger(__name__)

SERVER_NAME = "vesta-saved-search"

# HTTP transports need a real bind address; stdio ignores both of these.
DEFAULT_HOST = "0.0.0.0"  # noqa: S104 - see below
# S104 (hardcoded_bind_all_interfaces) is suppressed deliberately. This process runs
# in a container and must accept connections from the orchestrator in another
# container, so binding only to loopback would make it unreachable by design.
# Exposure is controlled by the network and the port mapping, not by this default —
# and the value is overridable with MCP_HOST for anyone running it differently.
DEFAULT_PORT = 6001

# Default to the transport this server actually gets deployed with, unlike the
# property-search server which defaults to stdio for `mcp dev` convenience. A stdio
# default here would mean `docker run` starts cleanly and then serves nothing on the
# port the Dockerfile exposes — a failure that looks like a network problem.
DEFAULT_TRANSPORT = "streamable-http"

_Transport = Literal["stdio", "sse", "streamable-http"]


def create_app() -> FastMCP:
    """Build a fresh :class:`FastMCP` app with the ``/health`` route registered.

    A factory rather than a module-level singleton, because the tests need their own
    instance to register a throwaway tool on. That matters more than it looks: N1's
    real deliverable is proof that the ``_meta`` channel works **over the actual
    protocol**, and the only honest way to test that is a real MCP client talking to a
    real app with a tool on it. A production singleton would force that test tool to
    ship in the wheel.
    """
    host = os.getenv("MCP_HOST", DEFAULT_HOST)
    port = _port_from_env()

    app = FastMCP(SERVER_NAME, host=host, port=port)

    # `type: ignore[untyped-decorator]` — `custom_route` comes from the MCP SDK, which
    # this repo exempts from import typing (see the mypy override for `mcp.*`), so mypy
    # sees an untyped decorator and would treat `health` as untyped too. Scoped to this
    # one error code on this one line rather than relaxing
    # `disallow_untyped_decorators` for the module, which would also hide the next
    # untyped decorator somebody adds.
    @app.custom_route("/health", methods=["GET"])  # type: ignore[untyped-decorator]
    async def health(_request: Request) -> JSONResponse:
        """Liveness only: 200 when the process is up and serving.

        Deliberately does NOT check the GuestSite API. Liveness answers "should this
        process be restarted", and an upstream outage is not a reason to restart —
        a health check that fails on someone else's outage turns their incident into
        a restart loop of ours. Readiness against upstreams belongs in the
        orchestrator, which already gates on MCP handshake health.
        """
        return JSONResponse({"status": "ok", "service": SERVER_NAME})

    return app


def _port_from_env() -> int:
    """Read ``MCP_PORT``, falling back to :data:`DEFAULT_PORT` if it is not a number.

    A malformed port logs a warning and falls back rather than raising. The
    alternative — crashing at startup on a typo — trades a server listening on a
    documented default for a container that will not boot, and the log line says
    exactly what happened.
    """
    raw = os.getenv("MCP_PORT")
    if raw is None:
        return DEFAULT_PORT
    try:
        return int(raw)
    except ValueError:
        logger.warning(
            "invalid_mcp_port value=%r falling_back_to=%d",
            raw,
            DEFAULT_PORT,
        )
        return DEFAULT_PORT


def main() -> None:
    """Configure logging and run the server on the configured transport."""
    logging.basicConfig(
        level=os.getenv("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )

    transport = os.getenv("MCP_TRANSPORT", DEFAULT_TRANSPORT)
    app = create_app()

    logger.info(
        "server_startup service=%s transport=%s host=%s port=%s",
        SERVER_NAME,
        transport,
        os.getenv("MCP_HOST", DEFAULT_HOST),
        _port_from_env(),
    )
    # MCP_TRANSPORT is documented above as one of the FastMCP transports; narrow the
    # env string for the type checker. An unknown value fails inside `run()` with the
    # SDK's own message, which names the valid transports — better than us
    # duplicating that list and letting it drift.
    app.run(transport=cast(_Transport, transport))


if __name__ == "__main__":  # pragma: no cover - process entry point
    main()
