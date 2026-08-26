"""Mount it the way it is deployed, and drive real MCP traffic.

Three things only go wrong in the seam between this wrapper and the app around
it, which is why they need a test that assembles both:

  root_path   Starlette records a Mount's prefix and leaves `path` whole; the
              child router strips it again. Hand it a bare "/mcp" and "" is
              what gets matched.
  lifespan    Starlette does not run a mounted sub-application's lifespan, and
              FastMCP's session manager needs one.
  host        FastMCP's DNS-rebinding protection allows loopback only, so a
              real hostname has to be added to it.
"""

from __future__ import annotations

import os
from contextlib import asynccontextmanager

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi import FastAPI                       # noqa: E402
from fastapi.testclient import TestClient         # noqa: E402

INIT = {
    "jsonrpc": "2.0",
    "id": 1,
    "method": "initialize",
    "params": {
        "protocolVersion": "2024-11-05",
        "capabilities": {},
        "clientInfo": {"name": "probe", "version": "0"},
    },
}
ACCEPT = {"Accept": "application/json, text/event-stream"}


@pytest.fixture(scope="module")
def mounted():
    """A FastAPI app with the transport mounted exactly as khwan-api does it."""
    # Module-scoped, because FastMCP's session manager can be run only once per
    # process — a fresh app per test raises rather than starting a second one.
    mp = pytest.MonkeyPatch()
    mp.setenv("KHWAN_MCP_RESOURCE", "https://mcp.khwan.ai")
    mp.setenv("KHWAN_MCP_ALLOWED_HOSTS", "testserver,mcp.khwan.ai,mcp.khwan.ai:*")

    from khwan_mcp.http import app as mcp_app, session_lifespan

    api = FastAPI()
    previous = api.router.lifespan_context

    @asynccontextmanager
    async def lifespan(app):
        async with previous(app):
            async with session_lifespan():
                yield

    api.router.lifespan_context = lifespan
    api.mount("/mcp", mcp_app())
    with TestClient(api, raise_server_exceptions=False) as client:
        yield client
    mp.undo()


def test_an_authenticated_request_reaches_the_protocol(mounted):
    """The end of the chain: a real initialize, answered."""
    r = mounted.post("/mcp/", json=INIT,
                     headers={**ACCEPT, "Authorization": "Bearer fake.token"})
    assert r.status_code == 200
    assert '"protocolVersion"' in r.text
    assert '"jsonrpc":"2.0"' in r.text


def test_a_brain_in_the_path_still_reaches_the_protocol(mounted):
    """The 404: root_path made the router strip the prefix twice."""
    r = mounted.post("/mcp/acme/web", json=INIT,
                     headers={**ACCEPT, "Authorization": "Bearer fake.token"})
    assert r.status_code == 200
    assert '"protocolVersion"' in r.text


def test_the_guard_still_guards_when_mounted(mounted):
    r = mounted.post("/mcp/", json=INIT, headers=ACCEPT)
    assert r.status_code == 401
    assert "resource_metadata=" in r.headers["www-authenticate"]


def test_not_421(mounted):
    """DNS-rebinding protection stays ON — the hostname is allowed, not the check
    disabled. A 421 here would mean someone turned it off and it came back."""
    r = mounted.post("/mcp/", json=INIT,
                     headers={**ACCEPT, "Authorization": "Bearer t",
                              "Host": "mcp.khwan.ai"})
    assert r.status_code != 421


def test_an_unknown_host_is_still_refused(mounted):
    """The protection has to still protect, or widening it was just disabling it."""
    r = mounted.post("/mcp/", json=INIT,
                     headers={**ACCEPT, "Authorization": "Bearer t",
                              "Host": "evil.example.com"})
    assert r.status_code == 421


def test_the_host_allow_list_comes_from_the_resource(monkeypatch):
    """One source of truth for what this server calls itself."""
    monkeypatch.delenv("KHWAN_MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.setenv("KHWAN_MCP_RESOURCE", "https://mcp.khwan.ai")
    from khwan_mcp.http import _allowed_hosts
    assert _allowed_hosts() == ["mcp.khwan.ai", "mcp.khwan.ai:*"]


def test_no_resource_means_no_widening(monkeypatch):
    """Nothing configured, nothing loosened — loopback-only stays."""
    monkeypatch.delenv("KHWAN_MCP_ALLOWED_HOSTS", raising=False)
    monkeypatch.delenv("KHWAN_MCP_RESOURCE", raising=False)
    from khwan_mcp.http import _allowed_hosts
    assert _allowed_hosts() is None
