"""Unit tests for the app factory and the process entry point (step N1 / VA-396)."""

from __future__ import annotations

import logging
from typing import Any

import pytest

from vesta_saved_search import server

pytestmark = pytest.mark.unit


def test_create_app_returns_a_fresh_instance_each_call() -> None:
    """The factory must not memoise.

    Tests register throwaway tools on their own app; sharing one instance would leak
    a test-only tool into every later test's tool list.
    """
    assert server.create_app() is not server.create_app()


def _route_named(app: Any, path: str) -> Any:
    """Find a mounted route by path.

    Reads `path` with `getattr`, because Starlette's `BaseRoute` does not declare it —
    only the concrete `Route`/`Mount` subclasses do — and `routes` is typed as the base.
    """
    for route in app.streamable_http_app().routes:
        if getattr(route, "path", None) == path:
            return route
    return None


def test_health_route_is_registered() -> None:
    assert _route_named(server.create_app(), "/health") is not None


async def test_health_returns_ok() -> None:
    """Call the route function directly — it takes no meaningful request state."""
    route = _route_named(server.create_app(), "/health")
    response = await route.endpoint(None)
    assert response.status_code == 200
    assert response.body == b'{"status":"ok","service":"vesta-saved-search"}'


def test_port_defaults_to_6001_matching_the_dockerfile(monkeypatch: pytest.MonkeyPatch) -> None:
    """6001 is duplicated in the Dockerfile's EXPOSE; drift means a silent no-listen."""
    monkeypatch.delenv("MCP_PORT", raising=False)
    assert server._port_from_env() == 6001
    assert server.DEFAULT_PORT == 6001


def test_port_reads_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MCP_PORT", "7002")
    assert server._port_from_env() == 7002


def test_malformed_port_falls_back_and_warns(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A typo must not stop the process booting, but must not be silent either."""
    monkeypatch.setenv("MCP_PORT", "not-a-port")
    with caplog.at_level(logging.WARNING, logger=server.__name__):
        assert server._port_from_env() == server.DEFAULT_PORT
    assert "invalid_mcp_port" in caplog.text
    assert "not-a-port" in caplog.text


def test_default_transport_is_http_not_stdio() -> None:
    """Deliberate divergence from the property-search server, which defaults to stdio.

    A stdio default here would make `docker run` start cleanly and serve nothing on
    the port the Dockerfile exposes — a failure that reads as a network problem.
    """
    assert server.DEFAULT_TRANSPORT == "streamable-http"


def test_main_runs_the_app_on_the_configured_transport(monkeypatch: pytest.MonkeyPatch) -> None:
    """`main()` is the container's entry point, so assert it wires transport through."""
    monkeypatch.setenv("MCP_TRANSPORT", "stdio")
    recorded: dict[str, Any] = {}

    class _StubApp:
        def run(self, transport: str) -> None:
            recorded["transport"] = transport

    monkeypatch.setattr(server, "create_app", lambda: _StubApp())
    server.main()
    assert recorded == {"transport": "stdio"}


def test_main_defaults_transport_when_env_unset(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("MCP_TRANSPORT", raising=False)
    recorded: dict[str, Any] = {}

    class _StubApp:
        def run(self, transport: str) -> None:
            recorded["transport"] = transport

    monkeypatch.setattr(server, "create_app", lambda: _StubApp())
    server.main()
    assert recorded == {"transport": server.DEFAULT_TRANSPORT}
