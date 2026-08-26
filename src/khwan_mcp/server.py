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

import contextvars
import os

import anyio
from collections import OrderedDict
from contextlib import contextmanager
from typing import Any, Dict, Iterator, List, Optional, Tuple

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
- REMEMBER (write): call `khwan_remember(fact=<standing rule>)` — and do NOT wait
  for a fact to feel important enough. The trigger is mechanical, not a judgement
  call. Call it in the SAME turn, before you carry out the fix, whenever the user:
    * rejects or corrects what you just did ("not that", "wrong", "ไม่ใช่", "ไม่เอา")
    * states a preference about HOW to work ("always X", "never Y", "ask me first")
    * tells you something you already did once — a repeat means the first one was lost
    * decides something, with a reason, that a future session would otherwise re-litigate
  Store the RULE, not the sentence: "deploys go to staging first, never straight to
  production" — not "no, not like that". It has to make sense to a session that
  never saw this one.
  This is the one thing a rules file (CLAUDE.md, .cursorrules, AGENTS.md) cannot do
  for the user: it only ever holds what someone remembered to write down. Being
  corrected once should be enough, and that only works if you write it down here.

- FULL LOOP (for custom agents / non-caching hosts): `khwan_prepare(input)` → you
  answer grounded in the returned context → `khwan_record(turn_token, answer)`.
  This bounds per-turn cost by replacing history with distilled memory — a real
  win when the host does NOT cache. On a caching host, prefer recall + remember.
  Always pass back the exact `turn_token` from `khwan_prepare`; never invent one.
"""

mcp = FastMCP("khwan", instructions=INSTRUCTIONS)


# Whose credentials the CURRENT call runs under.
#
# On stdio the answer is the environment, because the process belongs to one
# person: that is the whole shape of `uvx khwan-mcp` and it is why reading
# os.environ once was correct. A shared HTTP server breaks that assumption and
# nothing in the old code noticed — one cached client, built from process
# environment on the first call, served every caller after it. That is a
# cross-tenant leak by construction rather than by bug, so the context is set
# per request by the transport and falls back to the environment when unset.
_request_creds: contextvars.ContextVar[Optional[Dict[str, Any]]] = contextvars.ContextVar(
    "khwan_request_creds", default=None
)


@contextmanager
def request_credentials(api_key: Optional[str] = None, *,
                        bearer_token: Optional[str] = None,
                        core: Optional[str] = None,
                        user: Optional[str] = None,
                        base_url: Optional[str] = None) -> Iterator[None]:
    """Run a block as one specific caller — for a transport serving many.

    A ContextVar, not a global: each request in an asyncio server gets its own
    copy, so concurrent callers cannot see each other's credentials even while
    they interleave. Resets on exit, including when the body raises.

    Exactly one credential. An `api_key` is the account's own secret; a
    `bearer_token` is an OAuth token belonging to the caller, which is what an
    HTTP transport receives and must not exchange for anything of its own.
    """
    if bool(api_key) == bool(bearer_token):
        raise ValueError("pass exactly one of api_key or bearer_token")
    creds: Dict[str, Any] = (
        {"bearer_token": bearer_token} if bearer_token else {"api_key": api_key}
    )
    if core:
        creds["core"] = core
    if user:
        creds["user_id"] = user
    if base_url:
        creds["base_url"] = base_url
    token = _request_creds.set(creds)
    try:
        yield
    finally:
        _request_creds.reset(token)


def _creds() -> Dict[str, Any]:
    """The credentials for this call: the request's, else the environment's."""
    override = _request_creds.get()
    if override is not None:
        return override
    api_key = os.environ.get("KHWAN_API_KEY")
    if not api_key:
        raise RuntimeError(
            "KHWAN_API_KEY is not set — get one from your Khwan dashboard."
        )
    creds: Dict[str, Any] = {"api_key": api_key}
    if os.environ.get("KHWAN_CORE"):
        creds["core"] = os.environ["KHWAN_CORE"]
    if os.environ.get("KHWAN_USER"):
        creds["user_id"] = os.environ["KHWAN_USER"]
    if os.environ.get("KHWAN_BASE_URL"):
        creds["base_url"] = os.environ["KHWAN_BASE_URL"]
    return creds


# Clients are cached BY CREDENTIAL, never process-wide. Bounded and LRU because
# the cache would otherwise grow one entry per caller and hold their keys for
# the life of the process; on stdio it never exceeds one entry.
_CLIENT_CACHE_MAX = 32
_clients: "OrderedDict[Tuple[Tuple[str, Any], ...], Khwan]" = OrderedDict()


def _kw() -> Khwan:
    creds = _creds()
    key = tuple(sorted(creds.items()))
    hit = _clients.get(key)
    if hit is not None:
        _clients.move_to_end(key)
        return hit
    client = Khwan(**creds)
    _clients[key] = client
    _clients.move_to_end(key)
    while len(_clients) > _CLIENT_CACHE_MAX:
        _clients.popitem(last=False)
    return client


def _lessons(turn: Turn) -> List[str]:
    """Synthesised rules for this turn.

    Read off the raw Turn dict: the hosted client has no `lessons` property yet,
    and an older engine simply omits the key.
    """
    raw = turn.raw() if hasattr(turn, "raw") else {}
    return [str(x) for x in (raw.get("lessons") or []) if str(x).strip()]


UPGRADE_URL = "https://app.khwan.ai"


def _fail(op: str, e: KhwanError) -> RuntimeError:
    """Turn a Khwan API error into something the MODEL can act on.

    A tool error is read by a model, not by a person, so `failed (402)` tells it
    nothing about what to do next — and every plausible guess is a wrong one:
    retrying a paywall until the turn dies, or dropping memory silently on a blip
    that a wait would have cleared. Name the KIND of failure and the next move.

    Note what is not here: a retry delay. The server sends `Retry-After` on a 429,
    but KhwanError carries only `.status` and a message, so the number never
    reaches this layer. Better to say "wait" than to invent a figure.
    """
    detail = str(e).strip().rstrip(".")
    if e.status == 0:
        # Never reached the API at all — a transport or configuration problem,
        # not an auth or plan one. Worth naming, because `failed (0)` next to a
        # requests traceback reads like the memory service rejected something.
        return RuntimeError(
            f"khwan {op} failed: could not reach the Khwan API at all — {detail}. "
            f"Nothing was sent, so this is not about the credential or the plan. "
            f"KHWAN_BASE_URL may point somewhere wrong, or the API is "
            f"unreachable from here. Tell the user; retrying will not help until "
            f"that changes."
        )
    if e.status == 402:
        return RuntimeError(
            f"khwan {op} failed (402): {detail}. This is a PLAN LIMIT, not a "
            f"transient error — retrying will not clear it. Tell the user which "
            f"limit they reached and that a larger plan at {UPGRADE_URL} lifts "
            f"it, then carry on without this call."
        )
    if e.status == 429:
        return RuntimeError(
            f"khwan {op} failed (429): {detail}. Wait before any retry, and if "
            f"you cannot wait, continue WITHOUT memory rather than "
            f"retrying in a loop. Repeated 429s mean the plan's burst is too "
            f"small for this workload — a larger plan at {UPGRADE_URL} raises it."
        )
    if e.status in (401, 403):
        return RuntimeError(
            f"khwan {op} failed ({e.status}): {detail}. The credential is missing "
            f"or rejected — this server reads KHWAN_API_KEY from its environment. "
            f"Do not retry; tell the user, whose key is at {UPGRADE_URL}."
        )
    if e.status == 404:
        return RuntimeError(
            f"khwan {op} failed (404): {detail}. Usually KHWAN_CORE names a core "
            f"that does not exist: cores are created in the dashboard, and the "
            f"free plan has only `default`. Do not retry; tell the user, who can "
            f"add cores at {UPGRADE_URL}."
        )
    return RuntimeError(f"khwan {op} failed ({e.status}): {detail}.")


async def _off_loop(fn, *args):
    """Run a blocking call in a worker thread instead of on the event loop.

    FastMCP invokes a sync tool function DIRECTLY — `return fn(**args)`, no
    thread — so a tool that does blocking I/O holds the loop for the whole
    request. On stdio that only ever delayed the one person the process belongs
    to. On a shared HTTP server it stops everything: other callers, the health
    check, and the very API this call is waiting on when it is the process next
    door.

    Every tool is therefore `async def` and does its work here.
    """
    return await anyio.to_thread.run_sync(fn, *args)


@mcp.tool()
async def khwan_prepare(input: str) -> Dict[str, Any]:
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
        turn = await _off_loop(lambda: _kw().prepare(input))
    except KhwanError as e:
        raise _fail("prepare", e) from e
    return {
        "context": turn.messages,
        "lessons": _lessons(turn),
        "coherence": turn.coherence,
        "allowed": turn.allowed,
        "reason": turn.reason,
        "sources": turn.sources,
        "turn_token": turn.turn_token,
    }


@mcp.tool()
async def khwan_record(turn_token: str, answer: str) -> Dict[str, Any]:
    """Hand your answer back to Khwan AFTER you reply, so it persists + learns.

    Args:
        turn_token: the exact token returned by the matching ``khwan_prepare``.
        answer:     the answer you gave the user for that turn.

    Returns:
        Khwan's record acknowledgement (persisted state / next-turn hints).
    """
    try:
        # The hosted client reads turn_token off a Turn; we only need the token.
        return await _off_loop(lambda: _kw().record(Turn({"turn_token": turn_token}), answer))
    except KhwanError as e:
        raise _fail("record", e) from e


@mcp.tool()
async def khwan_recall(query: str, limit: int = 3) -> Dict[str, Any]:
    """SEED a session/subagent with a COMPACT, bounded set of relevant memories.

    The token-smart entry point for a caching host (Claude Code, Claude Desktop):
    call it ONCE at the start of a session or subagent — or when you need a fact
    that has scrolled out of context — NOT on every turn. It returns only the
    relevant facts (not Khwan's full prepared prompt), so you seed a fresh,
    bounded context instead of replaying a transcript. No model is called.

    Two limits are worth knowing, because neither is this tool's to set:

    - **Three facts is the ceiling.** The server ranks a wider candidate pool and
      keeps its top three, so `limit` can only narrow that further, never widen
      it. Asking for more returns three.
    - **A relevance floor applies, so an EMPTY `facts` is an answer.** It means
      the brain has nothing close to this question — read it as "not known here",
      not as a failure. Do not retry with a reworded query hoping for more, and
      do not fill the gap with whichever fact happened to be nearest.

    Lessons — what synthesis distilled from many turns — come back alongside the
    raw exchanges and LEAD the seed text: a rule earned over months outranks any
    single turn that happens to sit nearby in the index.

    Args:
        query: the task or topic to recall memory for. Phrase it as the work you
            are about to do, not as a keyword — it is matched on meaning.
        limit: cap on facts returned, 1-3. The server's own ceiling is 3, so this
            can only lower it. Leave it alone unless you want fewer than three.

    Returns:
        lessons:   rules synthesis distilled from many past turns.
        facts:     [{you_said, khwan_knows}] — the relevant remembered exchanges.
        count:     how many facts were returned.
        seed_text: a ready-to-drop-in memory block for a subagent's brief ("" if none).
    """
    try:
        turn = await _off_loop(lambda: _kw().prepare(query))
    except KhwanError as e:
        raise _fail("recall", e) from e
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
    lessons = _lessons(turn)
    blocks = []
    if lessons:
        # First, and labelled: these are standing rules, not one recalled turn.
        blocks.append("What Khwan has learned (applies generally):\n"
                      + "\n".join(f"- {l}" for l in lessons))
    if seed_lines:
        blocks.append("Relevant memory (recalled from Khwan):\n" + "\n".join(seed_lines))
    return {"query": query, "lessons": lessons, "facts": facts,
            "count": len(facts), "seed_text": "\n\n".join(blocks)}


@mcp.tool()
async def khwan_remember(fact: str) -> Dict[str, Any]:
    """Persist a durable fact/preference so FUTURE sessions can recall it.

    A convenience over the prepare→record loop for the common "just remember this"
    case: it stores ``fact`` in the brain (no model call) so it outlives this
    session's context window and is available to the next ``khwan_recall``.

    **Reach for this the moment you are corrected.** A user rejecting your work, or
    telling you how they want it done, is the most durable thing a session produces
    and the easiest to lose — you fix the thing, the session ends, and the next one
    makes the same mistake. If the user is telling you something for the second
    time, the first time should have been stored here.

    Write the standing RULE, not the utterance. "Deploys go to staging first, never
    straight to production" survives into a session that never saw the conversation;
    "no, not like that" does not.

    Args:
        fact: the durable rule/preference to store, phrased to stand alone.

    Returns:
        stored: whether the fact was persisted; reason when not.
    """
    kw = _kw()
    try:
        turn = await _off_loop(lambda: kw.prepare(fact))
        if not turn.turn_token:
            return {"stored": False, "reason": turn.reason or "brain gate declined the turn"}
        # Two calls, two hops off the loop. Keeping them separate matters: the
        # gate above decides whether the second one happens at all.
        await _off_loop(lambda: kw.record(turn, fact))
    except KhwanError as e:
        raise _fail("remember", e) from e
    return {"stored": True, "contradicted_memory": bool(turn._d.get("contradiction"))
            if hasattr(turn, "_d") else None}


@mcp.tool()
async def khwan_memory(limit: int = 20) -> Dict[str, Any]:
    """Inspect what this brain currently remembers, newest first.

    A DEBUGGING window on the brain, not a way to seed a session. It returns
    recent entries in time order and ignores what you are working on, so it
    answers "is anything in here / did that write land" — not "what is relevant
    to this task". For the latter use ``khwan_recall``, which ranks by meaning
    and returns a bounded set. No model is called.

    Reach for it when a recall came back empty and you want to know whether the
    brain is empty or merely has nothing close, when confirming a
    ``khwan_remember`` persisted, or when the user asks what Khwan knows.

    Args:
        limit: max entries to return, newest first (default 20).

    Returns:
        The brain's recent memory entries, in the order they were written.
    """
    try:
        return await _off_loop(lambda: _kw().memory(limit))
    except KhwanError as e:
        raise _fail("memory", e) from e


@mcp.tool()
async def khwan_cores() -> List[Any]:
    """List the isolated cores (brains) available on this account.

    Each core is a fully isolated brain (own memory/identity/learning). Select
    one for the session via the ``KHWAN_CORE`` environment variable.
    """
    try:
        result = await _off_loop(lambda: _kw().cores())
    except KhwanError as e:
        raise _fail("cores", e) from e
    # /cores may return a bare list or {"cores": [...]}; normalize to a list.
    if isinstance(result, dict):
        return result.get("cores", result)
    return result


def main() -> None:
    """Console entry point (``khwan-mcp``). Runs over stdio for local MCP clients."""
    mcp.run()


if __name__ == "__main__":
    main()
