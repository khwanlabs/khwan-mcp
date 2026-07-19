#!/usr/bin/env python3
"""Claude Code Stop hook — record Claude's answer back to Khwan so it learns.

Pairs with ``khwan_prepare_hook.py``. On Stop, Claude Code passes the session's
transcript path; we read the last assistant message and record it against the
turn_token the prepare hook stashed. Fails open on any error.

Env: KHWAN_API_KEY (required), KHWAN_CORE / KHWAN_USER / KHWAN_BASE_URL (optional).
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

from khwan import Khwan, Turn


def _stash_path(session_id: str) -> Path:
    safe = "".join(c for c in session_id if c.isalnum() or c in "-_") or "default"
    return Path(tempfile.gettempdir()) / f"khwan-turn-{safe}.txt"


def _last_assistant_text(transcript_path: str) -> str:
    """Read the final assistant turn's text from a JSONL transcript."""
    path = Path(transcript_path)
    if not path.exists():
        return ""
    answer = ""
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            msg = entry.get("message", entry)
            if msg.get("role") != "assistant":
                continue
            content = msg.get("content", "")
            if isinstance(content, str):
                answer = content
            elif isinstance(content, list):
                answer = "".join(
                    block.get("text", "")
                    for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
    except OSError:
        return ""
    return answer.strip()


def main() -> int:
    try:
        event = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0

    session_id = event.get("session_id", "default")
    stash = _stash_path(session_id)
    if not stash.exists():
        return 0  # no prepared turn for this session; nothing to record

    try:
        turn_token = stash.read_text(encoding="utf-8").strip()
    except OSError:
        return 0
    if not turn_token:
        return 0

    answer = _last_assistant_text(event.get("transcript_path", ""))
    if not answer:
        return 0

    api_key = os.environ.get("KHWAN_API_KEY")
    if not api_key:
        return 0

    kwargs = {"api_key": api_key}
    for env, arg in (("KHWAN_CORE", "core"), ("KHWAN_USER", "user_id"),
                     ("KHWAN_BASE_URL", "base_url")):
        if os.environ.get(env):
            kwargs[arg] = os.environ[env]

    try:
        Khwan(**kwargs).record(Turn({"turn_token": turn_token}), answer)
    except Exception as e:  # fail open
        print(f"[khwan] record failed: {e}", file=sys.stderr)
        return 0

    # Consume the token so a later Stop (e.g. a subsequent prompt) doesn't
    # re-record against a spent turn.
    try:
        stash.unlink()
    except OSError:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
