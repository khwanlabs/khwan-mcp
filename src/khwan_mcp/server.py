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
Khwan is DURABLE, cross-session memory for this account. It does NOT run a model
— you do. Use it to carry knowledge ACROSS sessions, not to re-inject memory on
every turn (a caching host like Claude Code already makes within-session history
cheap, so per-turn injection here adds tokens without saving them).

Recommended usage:

- SEED (read): At the start of a session — or when you spawn a subagent, or need
  a fact that has scrolled out of context — call `khwan_recall(query=<task/topic>)`
  ONCE to pull a COMPACT set of relevant remembered facts, and ground your work
  in them. This is the token-smart entry point: a fresh, bounded context instead
  of replaying a whole transcript.
- REMEMBER (write): When a durable fact, preference, or decision emerges that
  should outlive this session's context window, call `khwan_remember(fact=<it>)`.

- FULL LOOP (for custom agents / non-caching hosts): `khwan_prepare(input)` → you
  answer grounded in the returned context → `khwan_record(turn_token, answer)`.
  This bounds per-turn cost by replacing history with distilled memory — a real
  win when the host does NOT cache. On a caching host, prefer recall + remember.
  Always pass back the exact `turn_token` from `khwan_prepare`; never invent one.
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
def khwan_recall(query: str, limit: int = 8) -> Dict[str, Any]:
    """SEED a session/subagent with a COMPACT, bounded set of relevant memories.

    The token-smart entry point for a caching host (Claude Code, Claude Desktop):
    call it ONCE at the start of a session or subagent — or when you need a fact
    that has scrolled out of context — NOT on every turn. It returns only the
    relevant facts (not Khwan's full prepared prompt), so you seed a fresh,
    bounded context instead of replaying a transcript. No model is called.

    Args:
        query: the task or topic to recall memory for.
        limit: max facts to return (default 8).

    Returns:
        facts:     [{you_said, khwan_knows}] — the relevant remembered exchanges.
        count:     how many facts were returned.
        seed_text: a ready-to-drop-in memory block for a subagent's brief ("" if none).
    """
    try:
        turn = _kw().prepare(query)
    except KhwanError as e:
        raise RuntimeError(f"khwan recall failed ({e.status}): {e}") from e
    facts: List[Dict[str, Any]] = []
    for s in (turn.sources or [])[:limit]:
        if isinstance(s, dict) and s.get("response"):
            facts.append({"you_said": s.get("input"), "khwan_knows": s.get("response")})
    seed_lines = []
    for f in facts:
        you, khwan = (f["you_said"] or "").strip(), (f["khwan_knows"] or "").strip()
        # A fact stored via khwan_remember has you == khwan → show it once, not "X → X".
        seed_lines.append(f"- {khwan}" if not you or you == khwan
                          else f"- {you} → {khwan}")
    seed_text = ("Relevant memory (recalled from Khwan):\n" + "\n".join(seed_lines)
                 if seed_lines else "")
    return {"query": query, "facts": facts, "count": len(facts), "seed_text": seed_text}


@mcp.tool()
def khwan_remember(fact: str) -> Dict[str, Any]:
    """Persist a durable fact/preference so FUTURE sessions can recall it.

    A convenience over the prepare→record loop for the common "just remember this"
    case: it stores ``fact`` in the brain (no model call) so it outlives this
    session's context window and is available to the next ``khwan_recall``. Use it
    when a durable preference, decision, or fact emerges.

    Args:
        fact: the durable fact/preference to store.

    Returns:
        stored: whether the fact was persisted; reason when not.
    """
    kw = _kw()
    try:
        turn = kw.prepare(fact)
        if not turn.turn_token:
            return {"stored": False, "reason": turn.reason or "brain gate declined the turn"}
        kw.record(turn, fact)
    except KhwanError as e:
        raise RuntimeError(f"khwan remember failed ({e.status}): {e}") from e
    return {"stored": True, "contradicted_memory": bool(turn._d.get("contradiction"))
            if hasattr(turn, "_d") else None}


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
