"""Khwan MCP server.

Exposes Khwan's pure memory loop — ``prepare`` → (your model) → ``record`` — as
MCP tools, so any MCP client (Claude Code, Claude Desktop, …) gains a
persistent, *learning* memory. Khwan never runs a model: **the connected client
IS the model.** In Claude Code that means Claude itself is the "your model" step
of the loop.

The loop, as MCP tools:

1. ``khwan_prepare(input)`` → memory-enriched context + a ``turn_token``.
2. The client (Claude) answers, grounded in that context.
3. ``khwan_record(turn_token, answer)`` → Khwan persists + learns; the next
   ``prepare`` is sharper.

Config is environment-driven (nothing secret on the command line):

    KHWAN_API_KEY   required — from your Khwan dashboard (``kwk_live_…``)
    KHWAN_CORE      optional — select an isolated core/brain (default: account default)
    KHWAN_USER      optional — isolated sub-brain per end-user (paid); sets X-Khwan-User
    KHWAN_BASE_URL  optional — override API base (default https://api.khwan.ai;
                    use http://127.0.0.1:8010 against a local engine)
"""

from __future__ import annotations

import os
from typing import Any, Dict, List, Optional

from khwan import Khwan, KhwanError, Turn
from mcp.server.fastmcp import FastMCP

INSTRUCTIONS = """\
Khwan is this session's long-term memory. It does NOT run a model — you do.

For any turn where remembering (or being remembered) matters, follow the loop:
  1. Call `khwan_prepare(input=<the user's message>)` BEFORE you answer.
     Ground your answer in the returned `context` and honor `allowed`/`reason`.
  2. Answer the user (you are the model).
  3. Call `khwan_record(turn_token=<from step 1>, answer=<your answer>)` AFTER
     you answer, so Khwan can persist and learn.

Always pass the exact `turn_token` you received from `khwan_prepare` back into
`khwan_record` — do not invent or reuse one. Skip the loop only for trivial
turns with nothing worth remembering.
"""

mcp = FastMCP("khwan", instructions=INSTRUCTIONS)


def _client() -> Khwan:
    api_key = os.environ.get("KHWAN_API_KEY")
    if not api_key:
        raise RuntimeError(
            "KHWAN_API_KEY is not set — get one from your Khwan dashboard."
        )
    kwargs: Dict[str, Any] = {"api_key": api_key}
    if os.environ.get("KHWAN_CORE"):
        kwargs["core"] = os.environ["KHWAN_CORE"]
    if os.environ.get("KHWAN_USER"):
        kwargs["user_id"] = os.environ["KHWAN_USER"]
    if os.environ.get("KHWAN_BASE_URL"):
        kwargs["base_url"] = os.environ["KHWAN_BASE_URL"]
    return Khwan(**kwargs)


# One client per process, built lazily on first tool call — so the module stays
# importable (tests, --help, tool discovery) without KHWAN_API_KEY set, but a
# real call surfaces a clear error.
_kw_cache: Optional[Khwan] = None


def _kw() -> Khwan:
    global _kw_cache
    if _kw_cache is None:
        _kw_cache = _client()
    return _kw_cache


@mcp.tool()
def khwan_prepare(input: str) -> Dict[str, Any]:
    """Pull the memory-enriched context for a turn BEFORE you answer.

    Khwan builds context from memory + the brain's constitution + a coherence
    gate. No model is called. Ground your reply in the returned ``context`` and
    respect ``allowed``/``reason``. Keep the returned ``turn_token`` and pass it
    to ``khwan_record`` after you answer.

    Args:
        input: The user's message / the turn you are about to answer.

    Returns:
        context:    ready-to-use messages (memory + constitution) to ground your reply.
        coherence:  optional float — how coherent this turn is with the brain (may be None).
        allowed:    whether Khwan's coherence gate permits answering.
        reason:     why, when not allowed (else None).
        turn_token: opaque token — pass it verbatim to khwan_record.
    """
    try:
        turn = _kw().prepare(input)
    except KhwanError as e:
        raise RuntimeError(f"khwan prepare failed ({e.status}): {e}") from e
    return {
        "context": turn.messages,
        "coherence": turn.coherence,
        "allowed": turn.allowed,
        "reason": turn.reason,
        "sources": turn.sources,
        "turn_token": turn.turn_token,
    }


@mcp.tool()
def khwan_record(turn_token: str, answer: str) -> Dict[str, Any]:
    """Hand your answer back to Khwan AFTER you reply, so it persists + learns.

    Args:
        turn_token: the exact token returned by the matching ``khwan_prepare``.
        answer:     the answer you gave the user for that turn.

    Returns:
        Khwan's record acknowledgement (persisted state / next-turn hints).
    """
    try:
        # The hosted client reads turn_token off a Turn; we only need the token.
        return _kw().record(Turn({"turn_token": turn_token}), answer)
    except KhwanError as e:
        raise RuntimeError(f"khwan record failed ({e.status}): {e}") from e


@mcp.tool()
def khwan_memory(limit: int = 20) -> Dict[str, Any]:
    """Inspect what this brain currently remembers (most recent first).

    Args:
        limit: max memory entries to return (default 20).
    """
    try:
        return _kw().memory(limit)
    except KhwanError as e:
        raise RuntimeError(f"khwan memory failed ({e.status}): {e}") from e


@mcp.tool()
def khwan_cores() -> List[Any]:
    """List the isolated cores (brains) available on this account.

    Each core is a fully isolated brain (own memory/identity/learning). Select
    one for the session via the ``KHWAN_CORE`` environment variable.
    """
    try:
        result = _kw().cores()
    except KhwanError as e:
        raise RuntimeError(f"khwan cores failed ({e.status}): {e}") from e
    # /cores may return a bare list or {"cores": [...]}; normalize to a list.
    if isinstance(result, dict):
        return result.get("cores", result)
    return result


def main() -> None:
    """Console entry point (``khwan-mcp``). Runs over stdio for local MCP clients."""
    mcp.run()


if __name__ == "__main__":
    main()
