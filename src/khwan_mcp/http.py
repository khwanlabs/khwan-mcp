"""Streamable HTTP transport — the same six tools, served to many callers.

stdio stays the default and is not affected by anything here. This exists for one
reason: while a directory hosts our server, it holds both the user's credential
and, in the payload log, the memories themselves. Running the endpoint ourselves
is the only way off that, and it is why the transport is a custody decision
rather than a distribution one.

Two rules shape the whole module.

**We verify nothing.** The caller's OAuth token is passed through to the Khwan
API, which already verifies bearers against the IdP's JWKS. Verifying here too
would put the crypto and the key set in two places that must agree forever, and
the second one always drifts. This layer decides nothing about identity.

**The path asks, the token answers.** `/mcp/{core}/{user}` states which brain the
install wants — a request, never an entitlement. What the caller may actually
reach is decided by the API from the token. A slug someone does not own is
refused there, not here, and refused as 404 so the path cannot be used to learn
which cores exist.
"""

from __future__ import annotations

import json
import os
from contextlib import asynccontextmanager
from typing import Any, Callable, Dict, Optional, Tuple

from .server import mcp, request_credentials

# Where a client is told to look when it arrives without a token. RFC 9728: the
# 401 carries the URL of the protected-resource document, which names the
# authorization server. Without it a client has nowhere to start and simply
# fails, rather than discovering how to authenticate.
RESOURCE_METADATA_URL = "KHWAN_MCP_RESOURCE_METADATA"


def _resource_metadata_url() -> Optional[str]:
    explicit = (os.environ.get(RESOURCE_METADATA_URL) or "").strip()
    if explicit:
        return explicit
    resource = (os.environ.get("KHWAN_MCP_RESOURCE") or "").strip().rstrip("/")
    return f"{resource}/.well-known/oauth-protected-resource" if resource else None


def _bearer(headers: list) -> Optional[str]:
    for name, value in headers:
        if name.lower() != b"authorization":
            continue
        raw = value.decode("latin-1").strip()
        if raw[:7].lower() == "bearer ":
            return raw[7:].strip() or None
    return None


def _split_brain(path: str, root_path: str, inner: str) -> Tuple[Optional[str], Optional[str], str]:
    """Read the brain segments, and build the path the inner router will match.

    `root_path` is the part of the URL a Mount has claimed. Starlette does NOT
    remove it from `path` — it records it, and the child router matches
    ``path[len(root_path):]`` against its own routes, so the target has to
    include the prefix or it is stripped twice:

        mounted at /mcp, FastMCP serving /mcp
            in   path="/mcp/acme/web"  root_path="/mcp"
            out  path="/mcp/mcp"       → router sees "/mcp" → matches

        standalone
            in   path="/mcp/acme/web"  root_path=""
            out  path="/mcp"           → router sees "/mcp" → matches

    """
    rel = path[len(root_path):] if root_path and path.startswith(root_path) else path
    if rel == inner:
        rest = ""
    elif rel.startswith(inner + "/"):
        rest = rel[len(inner):]
    else:
        rest = rel
    rest = rest.strip("/")
    target = root_path + inner
    if not rest:
        return None, None, target
    parts = rest.split("/")
    if len(parts) > 2:
        # Deeper than we understand. Leave it intact for the inner app to
        # refuse — a URL we did not parse must not quietly become one we did.
        return None, None, path
    core = parts[0] or None
    user = parts[1] if len(parts) > 1 and parts[1] else None
    return core, user, target


async def _send_json(send: Callable, status: int, body: Dict[str, Any],
                     headers: Optional[list] = None) -> None:
    payload = json.dumps(body).encode()
    await send({
        "type": "http.response.start",
        "status": status,
        "headers": [(b"content-type", b"application/json"),
                    (b"content-length", str(len(payload)).encode())] + (headers or []),
    })
    await send({"type": "http.response.body", "body": payload})


def _allowed_hosts() -> Optional[list]:
    """Hosts this server may be addressed as, for DNS-rebinding protection.

    FastMCP ships that protection on, allowing only loopback — correct for a
    server started by a local client, and a 421 for every request the moment the
    thing is reachable at a real hostname. Turning the check off would be the
    quick fix and the wrong one: it is what stops a page on another origin from
    driving this endpoint through a victim's browser.

    So widen it rather than disable it, from the resource identifier we already
    configure — one source of truth for what this server calls itself.
    """
    explicit = [h.strip() for h in
                (os.environ.get("KHWAN_MCP_ALLOWED_HOSTS") or "").split(",") if h.strip()]
    if explicit:
        return explicit
    resource = (os.environ.get("KHWAN_MCP_RESOURCE") or "").strip()
    if not resource:
        return None
    host = resource.split("://", 1)[-1].split("/", 1)[0]
    return [host, f"{host}:*"] if host else None


def _apply_transport_security() -> None:
    hosts = _allowed_hosts()
    if not hosts:
        return
    settings = mcp.settings.transport_security
    for h in hosts:
        if h not in settings.allowed_hosts:
            settings.allowed_hosts.append(h)
        origin = f"https://{h}"
        if origin not in settings.allowed_origins:
            settings.allowed_origins.append(origin)


class BrainScopedMCP:
    """ASGI app: read the credential and the requested brain, then delegate.

    Wraps FastMCP's Streamable HTTP app. Every request runs inside
    ``request_credentials``, so the tools resolve a client belonging to THIS
    caller — never a process-wide one shared with whoever arrived first.
    """

    def __init__(self, app: Optional[Callable] = None, *, inner_path: str = "/mcp") -> None:
        if app is None:
            _apply_transport_security()
            app = mcp.streamable_http_app()
        self.app = app
        # The route FastMCP itself serves — not where this wrapper is mounted.
        # Those are different things; `root_path` carries the second one.
        self.inner_path = "/" + inner_path.strip("/")

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        token = _bearer(scope.get("headers") or [])
        if not token:
            # 401 with the pointer, not 403: the caller is not forbidden, it has
            # simply not authenticated yet, and this is where it learns how.
            metadata = _resource_metadata_url()
            challenge = 'Bearer'
            if metadata:
                challenge = f'Bearer resource_metadata="{metadata}"'
            await _send_json(
                send, 401,
                {"error": "unauthorized",
                 "error_description": "This endpoint requires an OAuth access token."},
                [(b"www-authenticate", challenge.encode())],
            )
            return

        core, user, path = _split_brain(
            scope["path"], scope.get("root_path") or "", self.inner_path)
        scope = {**scope, "path": path, "raw_path": path.encode()}

        # KHWAN_BASE_URL matters more here than on stdio. Mounted beside the
        # API, the default sends every tool call out to the public hostname and
        # back — a round trip over the internet to reach the process next door.
        with request_credentials(bearer_token=token, core=core, user=user,
                                 base_url=os.environ.get("KHWAN_BASE_URL") or None):
            await self.app(scope, receive, send)


def app(*, inner_path: str = "/mcp") -> BrainScopedMCP:
    """The ASGI app to mount, e.g. ``app.mount("/mcp", khwan_mcp.http.app())``."""
    return BrainScopedMCP(inner_path=inner_path)


@asynccontextmanager
async def session_lifespan():
    """Run FastMCP's session manager for as long as the host app is up.

    Starlette does not run the lifespan of a mounted sub-application. FastMCP's
    Streamable HTTP handler needs one — without it every authenticated request
    dies on `RuntimeError: Task group is not initialized`, which arrives as a
    500 and says nothing about mounting.

    So the host has to run this itself, alongside whatever lifespan it already
    has:

        prev = app.router.lifespan_context

        @asynccontextmanager
        async def lifespan(app):
            async with prev(app):
                async with session_lifespan():
                    yield

        app.router.lifespan_context = lifespan
    """
    async with mcp.session_manager.run():
        yield
