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


def _split_brain(path: str, inner: str) -> Tuple[Optional[str], Optional[str], str]:
    """Read the brain segments, from a mounted OR a standalone path.

    Starlette's `Mount` strips its own prefix before the inner app is called, so
    the same request arrives shaped two different ways depending on how this app
    was wired:

        mounted at /mcp   →  "/acme/web"
        standalone        →  "/mcp/acme/web"

    Both must work, and getting it wrong is silent: the segments are simply not
    seen, every install lands on the default brain, and nothing errors.

    Returns the core, the sub-brain, and the path to hand the inner app — which
    is always `inner`, because FastMCP serves at its own fixed route regardless
    of where this wrapper sits.
    """
    rest = path
    if rest == inner:
        rest = ""
    elif rest.startswith(inner + "/"):
        rest = rest[len(inner):]
    rest = rest.strip("/")
    if not rest:
        return None, None, inner
    parts = rest.split("/")
    if len(parts) > 2:
        # Deeper than we understand. Leave it intact for the inner app to
        # refuse — a URL we did not parse must not quietly become one we did.
        return None, None, path
    core = parts[0] or None
    user = parts[1] if len(parts) > 1 and parts[1] else None
    return core, user, inner


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


class BrainScopedMCP:
    """ASGI app: read the credential and the requested brain, then delegate.

    Wraps FastMCP's Streamable HTTP app. Every request runs inside
    ``request_credentials``, so the tools resolve a client belonging to THIS
    caller — never a process-wide one shared with whoever arrived first.
    """

    def __init__(self, app: Optional[Callable] = None, *, inner_path: str = "/mcp") -> None:
        self.app = app if app is not None else mcp.streamable_http_app()
        # The route FastMCP itself serves. Not where this wrapper is mounted —
        # those are different things, and conflating them is what broke the
        # first deploy.
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

        core, user, path = _split_brain(scope["path"], self.inner_path)
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
