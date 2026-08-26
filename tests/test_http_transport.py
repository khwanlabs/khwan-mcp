"""The HTTP transport decides two things and must decide nothing else.

It reads the caller's bearer token and the brain the URL asks for, then delegates.
It does not verify the token — the API does that, against the IdP's JWKS — and it
does not decide what the caller may reach. Everything here pins that boundary.

A stub inner app stands in for FastMCP so the tests exercise the wrapper rather
than the MCP session machinery.
"""

from __future__ import annotations

import asyncio

import pytest

from khwan_mcp import server
from khwan_mcp.http import BrainScopedMCP, _split_brain

JWT = "eyJhbGciOiJSUzI1NiJ9.stub.stub"


class Spy:
    """Records the scope it was called with, and the credentials in force."""

    def __init__(self, delay: float = 0.0):
        self.scope = None
        self.creds = None
        self.calls = 0
        self.delay = delay

    async def __call__(self, scope, receive, send):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        self.scope = scope
        self.creds = server._request_creds.get()


def _scope(path="/mcp", headers=None, type_="http"):
    return {"type": type_, "path": path, "headers": headers or []}


def _auth(token=JWT):
    return [(b"authorization", f"Bearer {token}".encode())]


async def _call(app, scope):
    sent = []

    async def send(msg):
        sent.append(msg)

    await app(scope, None, send)
    return sent


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    monkeypatch.delenv("KHWAN_MCP_RESOURCE_METADATA", raising=False)
    monkeypatch.setenv("KHWAN_MCP_RESOURCE", "https://mcp.khwan.ai")
    server._clients.clear()
    yield
    server._clients.clear()


# ── no token ──────────────────────────────────────────────────────────────────

def test_no_token_is_401_and_says_where_to_authenticate():
    spy = Spy()
    sent = asyncio.run(_call(BrainScopedMCP(spy), _scope()))

    start = sent[0]
    assert start["status"] == 401
    header = dict(start["headers"])[b"www-authenticate"].decode()
    assert 'resource_metadata="https://mcp.khwan.ai/.well-known/oauth-protected-resource"' in header
    assert spy.calls == 0          # never reaches the tools


def test_401_not_403():
    """Not forbidden — unauthenticated. 403 tells a client to give up."""
    sent = asyncio.run(_call(BrainScopedMCP(Spy()), _scope()))
    assert sent[0]["status"] == 401


def test_a_non_bearer_authorization_is_still_no_token():
    spy = Spy()
    scope = _scope(headers=[(b"authorization", b"Basic aGk6dGhlcmU=")])
    sent = asyncio.run(_call(BrainScopedMCP(spy), scope))
    assert sent[0]["status"] == 401 and spy.calls == 0


def test_the_challenge_degrades_rather_than_lying(monkeypatch):
    """No resource configured: still challenge, just without a pointer."""
    monkeypatch.delenv("KHWAN_MCP_RESOURCE", raising=False)
    sent = asyncio.run(_call(BrainScopedMCP(Spy()), _scope()))
    assert dict(sent[0]["headers"])[b"www-authenticate"] == b"Bearer"


# ── the token is passed through, not inspected ────────────────────────────────

def test_the_callers_token_becomes_the_credential():
    spy = Spy()
    asyncio.run(_call(BrainScopedMCP(spy), _scope(headers=_auth())))
    assert spy.creds == {"bearer_token": JWT}


def test_no_api_key_is_ever_synthesised():
    """The point of the whole design: our key is not in this path."""
    spy = Spy()
    asyncio.run(_call(BrainScopedMCP(spy), _scope(headers=_auth())))
    assert "api_key" not in spy.creds


def test_a_garbage_token_is_still_forwarded():
    """Verification belongs to the API. Guessing here would be a second opinion."""
    spy = Spy()
    asyncio.run(_call(BrainScopedMCP(spy), _scope(headers=_auth("not-a-jwt"))))
    assert spy.creds == {"bearer_token": "not-a-jwt"}


def test_the_context_is_released_after_the_request():
    asyncio.run(_call(BrainScopedMCP(Spy()), _scope(headers=_auth())))
    assert server._request_creds.get() is None


# ── the path asks for a brain ─────────────────────────────────────────────────

@pytest.mark.parametrize("path,core,user", [
    ("/mcp", None, None),
    ("/mcp/", None, None),
    ("/mcp/acme", "acme", None),
    ("/mcp/acme/web", "acme", "web"),
])
def test_brain_is_read_from_the_path(path, core, user):
    spy = Spy()
    asyncio.run(_call(BrainScopedMCP(spy), _scope(path, _auth())))
    assert spy.creds.get("core") == core
    # `user_id`, not `user` — that is the SDK's kwarg, and pinning the name here
    # is the point: a rename upstream should break a test, not a live request.
    assert spy.creds.get("user_id") == user
    assert spy.scope["path"] == "/mcp"      # rewritten for the inner app


def test_a_deeper_path_is_not_silently_accepted():
    """A URL we did not understand must not quietly become one we did."""
    spy = Spy()
    asyncio.run(_call(BrainScopedMCP(spy), _scope("/mcp/a/b/c", _auth())))
    assert spy.scope["path"] == "/mcp/a/b/c"     # left for the inner app to reject
    assert spy.creds.get("core") is None


def test_split_is_pure():
    assert _split_brain("/mcp/acme/web", "/mcp") == ("acme", "web", "/mcp")
    assert _split_brain("/elsewhere", "/mcp") == (None, None, "/elsewhere")


# ── isolation under load ──────────────────────────────────────────────────────

def test_concurrent_callers_do_not_cross():
    """The failure this whole design exists to prevent."""
    slow, fast = Spy(delay=0.02), Spy(delay=0.0)

    async def main():
        await asyncio.gather(
            _call(BrainScopedMCP(slow), _scope("/mcp/alice", _auth("token_alice"))),
            _call(BrainScopedMCP(fast), _scope("/mcp/bob", _auth("token_bob"))),
        )

    asyncio.run(main())
    assert slow.creds == {"bearer_token": "token_alice", "core": "alice"}
    assert fast.creds == {"bearer_token": "token_bob", "core": "bob"}


# ── non-HTTP traffic ──────────────────────────────────────────────────────────

def test_lifespan_passes_straight_through():
    spy = Spy()
    asyncio.run(_call(BrainScopedMCP(spy), _scope(type_="lifespan")))
    assert spy.calls == 1 and spy.creds is None
