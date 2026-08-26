"""One caller must never be served another caller's client.

Every test here fails against the code before this file existed, where `_kw()`
memoised a single client in a module global built from process environment. That
is correct on stdio — the process belongs to one person — and it is a
cross-tenant leak the moment the same process serves two.

Nothing here touches the network: constructing a Khwan client is offline, and
the assertions read the credentials off the object.
"""

from __future__ import annotations

import asyncio

import pytest

from khwan_mcp import server


@pytest.fixture(autouse=True)
def clean(monkeypatch):
    """No ambient environment, no carried-over cache."""
    for var in ("KHWAN_API_KEY", "KHWAN_CORE", "KHWAN_USER", "KHWAN_BASE_URL"):
        monkeypatch.delenv(var, raising=False)
    server._clients.clear()
    yield
    server._clients.clear()


def _key(client) -> str:
    return client._key


# ── the leak ──────────────────────────────────────────────────────────────────

def test_two_callers_never_share_a_client():
    with server.request_credentials("kwk_live_alice"):
        alice = server._kw()
    with server.request_credentials("kwk_live_bob"):
        bob = server._kw()

    assert alice is not bob
    assert _key(alice) == "kwk_live_alice"
    assert _key(bob) == "kwk_live_bob"


def test_the_first_caller_does_not_become_everyone():
    """The precise old failure: whoever called first owned the process."""
    with server.request_credentials("kwk_live_first"):
        server._kw()
    with server.request_credentials("kwk_live_second"):
        assert _key(server._kw()) == "kwk_live_second"


def test_core_and_user_are_part_of_identity():
    """Same key, different brain, must not be the same client."""
    with server.request_credentials("k", core="acme", user="web"):
        web = server._kw()
    with server.request_credentials("k", core="acme", user="api"):
        api = server._kw()

    assert web is not api
    assert (web.core, web.user_id) == ("acme", "web")
    assert (api.core, api.user_id) == ("acme", "api")


def test_concurrent_callers_do_not_see_each_other():
    """A ContextVar, not a global — interleaving must not cross the streams."""

    async def call(api_key: str, hold: float) -> str:
        with server.request_credentials(api_key):
            await asyncio.sleep(hold)          # yield mid-request, on purpose
            return _key(server._kw())

    async def main():
        return await asyncio.gather(
            call("kwk_live_a", 0.02),
            call("kwk_live_b", 0.01),
            call("kwk_live_c", 0.0),
        )

    assert asyncio.run(main()) == ["kwk_live_a", "kwk_live_b", "kwk_live_c"]


# ── what must not regress ─────────────────────────────────────────────────────

def test_stdio_still_reads_the_environment(monkeypatch):
    monkeypatch.setenv("KHWAN_API_KEY", "kwk_live_env")
    monkeypatch.setenv("KHWAN_CORE", "acme")
    monkeypatch.setenv("KHWAN_USER", "web")

    client = server._kw()
    assert (_key(client), client.core, client.user_id) == ("kwk_live_env", "acme", "web")
    assert server._kw() is client          # still one client per process there


def test_a_request_overrides_the_environment(monkeypatch):
    monkeypatch.setenv("KHWAN_API_KEY", "kwk_live_env")
    with server.request_credentials("kwk_live_request"):
        assert _key(server._kw()) == "kwk_live_request"
    assert _key(server._kw()) == "kwk_live_env"      # and hands it back after


def test_no_credentials_still_says_so_clearly():
    with pytest.raises(RuntimeError, match="KHWAN_API_KEY is not set"):
        server._kw()


def test_the_cache_is_bounded():
    """Otherwise it grows one entry per caller and holds their keys forever."""
    for i in range(server._CLIENT_CACHE_MAX + 10):
        with server.request_credentials(f"kwk_live_{i}"):
            server._kw()
    assert len(server._clients) == server._CLIENT_CACHE_MAX


def test_eviction_is_least_recently_used():
    with server.request_credentials("kwk_live_keep"):
        server._kw()
    for i in range(server._CLIENT_CACHE_MAX - 1):
        with server.request_credentials(f"kwk_live_filler_{i}"):
            server._kw()

    with server.request_credentials("kwk_live_keep"):      # touch it
        server._kw()
    with server.request_credentials("kwk_live_overflow"):  # push one out
        server._kw()

    assert any(dict(k).get("api_key") == "kwk_live_keep" for k in server._clients)


def test_the_context_resets_even_when_the_body_raises():
    monkeypatch_free = server._request_creds
    with pytest.raises(ValueError):
        with server.request_credentials("kwk_live_x"):
            raise ValueError("boom")
    assert monkeypatch_free.get() is None


def test_all_six_tools_still_register():
    names = [t.name for t in asyncio.run(server.mcp.list_tools())]
    assert names == [
        "khwan_prepare", "khwan_record", "khwan_recall",
        "khwan_remember", "khwan_memory", "khwan_cores",
    ]
