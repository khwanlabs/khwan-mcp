#!/usr/bin/env python3
"""Claude Code UserPromptSubmit hook — inject Khwan memory before Claude answers.

This is the *deterministic* alternative to the MCP tools: memory is pulled on
EVERY prompt without relying on the model to call a tool. Pair it with
``khwan_record_hook.py`` (a Stop hook) to close the loop.

Wire it up in ``.claude/settings.json`` (see ``settings.snippet.json``). Claude
Code passes the hook a JSON event on stdin; a UserPromptSubmit hook that prints
to stdout has that text added to the model's context for the turn.

Env: KHWAN_API_KEY (required), KHWAN_CORE / KHWAN_USER / KHWAN_BASE_URL (optional)
     — same as the MCP server.

The turn_token is stashed to a per-session file so the Stop hook can record the
answer against the same turn.
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from khwan import Khwan


def _stash_path(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "default"
    return Path(tempfile.gettempdir()) / f"khwan-turn-{safe}.txt"


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0  # nothing to do; never block the prompt

    user_input = event.get("prompt") or event.get("user_input") or ""
    session_id = event.get("session_id", "default")
    if not user_input.strip():
        return 0

    api_key = os.environ.get("KHWAN_API_KEY")
    if not api_key:
        # Fail open — a missing key must never block the user's prompt.
        print("[khwan] KHWAN_API_KEY not set; skipping memory injection.", file=sys.stderr)
        return 0

    kwargs = {"api_key": api_key}
    for env, arg in (("KHWAN_CORE", "core"), ("KHWAN_USER", "user_id"),
                     ("KHWAN_BASE_URL", "base_url")):
        if os.environ.get(env):
            kwargs[arg] = os.environ[env]

    try:
        turn = Khwan(**kwargs).prepare(user_input)
    except Exception as e:  # fail open on any error
        print(f"[khwan] prepare failed, continuing without memory: {e}", file=sys.stderr)
        return 0

    if turn.turn_token:
        try:
            _stash_path(session_id).write_text(turn.turn_token, encoding="utf-8")
        except OSError:
            pass

    # Surface the prepared context to Claude for this turn.
    lines = ["<khwan-memory>"]
    for msg in turn.messages:
        role = msg.get("role", "system")
        content = msg.get("content", "")
        if content:
            lines.append(f"[{role}] {content}")
    if turn.coherence is not None:
        lines.append(f"(coherence: {turn.coherence})")
    if not turn.allowed and turn.reason:
        lines.append(f"(coherence gate: {turn.reason})")
    lines.append("</khwan-memory>")
    print("\n".join(lines))
    return 0


if __name__ == "__main__":
    sys.exit(main())
