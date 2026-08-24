#!/usr/bin/env python3
"""Seed a Khwan core from Claude Code transcripts you have already produced.

Khwan only knows what it was told. If you have been working in Claude Code for
months, all of that is sitting in ``~/.claude/projects/<slug>/*.jsonl`` and the
brain has never seen a line of it. This walks those transcripts, distils each
turn into a durable fact, and replays them through ``prepare`` → ``record``.

No model is called — the distillation is deterministic (tool_use blocks, not
prose), and Khwan itself never runs a model.

    # look, change nothing (default)
    python3 backfill_claude_code.py --map cores.json

    # actually write, one project at a time
    python3 backfill_claude_code.py --map cores.json --project Khwan --commit

``cores.json`` maps a Claude Code project directory to the core to seed:

    {"-Users-you-Desktop-acme-web": "acme", "-Users-you-Desktop-side-project": "side"}

Cores are NOT auto-created — create each one in the dashboard first, or the API
answers 404 "unknown core".

Requires the Khwan client for ``--commit`` (``pip install khwan``); a dry run
needs nothing but the standard library.

Env: KHWAN_API_KEY (required), KHWAN_BASE_URL (optional).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import time
from pathlib import Path
from typing import Iterator, Optional

PROJECTS = Path.home() / ".claude" / "projects"

# Commands worth remembering — state changes, not lookups. A `grep` tells you
# nothing six months from now; a `git push` or an `alembic upgrade` does.
# Scratchpad and temp paths are scaffolding for one turn — never a durable fact.
# ~/.claude/projects/ is Claude Code's own store: its transcripts and its memory
# files. Recording "this turn edited Claude's memory" as a Khwan memory is a
# circular fact, and those paths embed the encoded home directory (and with it
# the username), which survives stripping $HOME from the front.
TRANSIENT = re.compile(r"^(/private)?/(tmp|var/folders)/|/scratchpad/|/node_modules/"
                       r"|(^|/)\.claude/(projects|todos|shell-snapshots)/")

SUBSTANTIVE = re.compile(
    r"\b(git (commit|push|merge|tag|revert|rebase)|gh (pr|release|issue)|"
    r"npm (publish|run build)|pip install|docker|railway|vercel|fly deploy|"
    r"alembic|psql|terraform|pytest|make )\b"
)


# ── distillation ──────────────────────────────────────────────────────────────

def turns(path: Path) -> Iterator[tuple[str, list]]:
    """Yield (user_text, events) per human turn, in transcript order."""
    cur_user: Optional[str] = None
    events: list = []
    try:
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except json.JSONDecodeError:
            continue
        msg = entry.get("message")
        if not isinstance(msg, dict) or not msg.get("role"):
            continue
        content = msg.get("content")
        blocks = content if isinstance(content, list) else [
            {"type": "text", "text": content or ""}]

        if msg["role"] == "user":
            # A tool_result comes back under role=user — not a new human turn.
            if any(isinstance(b, dict) and b.get("type") == "tool_result"
                   for b in blocks):
                continue
            text = " ".join(
                b.get("text", "") for b in blocks
                if isinstance(b, dict) and b.get("type") == "text"
            ).strip()
            # Skip hook/system injections (<khwan-memory>, <system-reminder>, …).
            if not text or text.startswith("<"):
                continue
            if cur_user and events:
                yield cur_user, events
            cur_user, events = text, []
            continue

        for b in blocks:
            if not isinstance(b, dict):
                continue
            if b.get("type") == "tool_use":
                inp = b.get("input") or {}
                name = b.get("name")
                if name in ("Edit", "Write", "NotebookEdit") and inp.get("file_path"):
                    events.append(("edit", inp["file_path"]))
                elif name == "Bash":
                    for cleaned in _commands(inp.get("command") or ""):
                        events.append(("ran", cleaned))
            elif b.get("type") == "text" and (b.get("text") or "").strip():
                events.append(("said", b["text"].strip()))
    if cur_user and events:
        yield cur_user, events


def _commands(block: str) -> list[str]:
    """The *kinds* of substantive action in a Bash block — `git push`, `npm run
    build`, `gh pr` — not the raw command lines.

    Storing raw shell was a losing game: quoting, pipes, heredocs and redirects
    to temp paths all had to be stripped, and a `|` inside a quoted regex broke
    every attempt. The exact flags are not what a future session needs to recall;
    that this turn pushed, built or opened a PR is. Specifics survive in OUTCOME.
    """
    out = []
    for m in SUBSTANTIVE.finditer(block):
        verb = " ".join(m.group(0).split())
        if verb not in out:
            out.append(verb)
    return out[:4]


def _short(path: str) -> str:
    """Drop the home prefix so a stored fact carries a path, not a machine."""
    home = str(Path.home())
    if path.startswith(home):
        path = path[len(home):].lstrip("/")
    return re.sub(r"-Users-[^-]+-", "-Users-", path)


def _short_text(text: str) -> str:
    """Same home-stripping, applied to prose — OUTCOME quotes absolute paths."""
    return text.replace(str(Path.home()) + "/", "~/").replace(str(Path.home()), "~")


def distil(user_text: str, events: list, repo_root: str = "") -> Optional[tuple[str, str]]:
    """(ask, outcome) for a turn that changed something — else None.

    A turn that only answered a question leaves nothing durable behind; storing
    it would just add a near-neighbour for future queries to trip over.
    """
    files = sorted({v for k, v in events if k == "edit"}
                   - {v for k, v in events if k == "edit" and TRANSIENT.search(v)})
    ran = [c for c in dict.fromkeys(v for k, v in events if k == "ran") if c]
    said = [v for k, v in events if k == "said"]
    if not files and not ran:
        return None

    files = [_short(f) for f in files]
    parts = []
    if files:
        parts.append("FILES: " + ", ".join(files[:8])
                     + (f" (+{len(files) - 8} more)" if len(files) > 8 else ""))
    if ran:
        parts.append("RAN: " + ", ".join(ran))
    if said:
        parts.append("OUTCOME: " + _short_text(said[-1])[:600])
    return user_text[:400], "\n".join(parts)


# ── replay ────────────────────────────────────────────────────────────────────

def ledger_path(core: str) -> Path:
    return Path.home() / ".khwan" / f"backfill-{core}.jsonl"


def already_done(core: str) -> set[str]:
    """Turn keys this core has already ingested — makes a re-run a no-op."""
    p = ledger_path(core)
    if not p.exists():
        return set()
    done = set()
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            done.add(json.loads(line)["key"])
        except (json.JSONDecodeError, KeyError):
            continue
    return done


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--map", required=True, help="JSON: {project_dir: core_slug}")
    ap.add_argument("--project",
                    help="only project dirs containing this (case-insensitive)")
    ap.add_argument("--commit", action="store_true",
                    help="actually write to Khwan (default: dry run)")
    # Each turn costs TWO operations (prepare + record), so turns/sec must stay
    # at half the plan's ops/sec or the server starts answering 429.
    ap.add_argument("--rate", type=float, default=1.0,
                    help="turns/sec; each turn = 2 ops. free=1, starter=5, pro=25")
    ap.add_argument("--limit", type=int,
                    help="stop after N turns IN TOTAL across every matched project")
    args = ap.parse_args()

    t_start = time.monotonic()
    mapping: dict[str, str] = json.loads(Path(args.map).read_text(encoding="utf-8"))
    if args.commit and not os.environ.get("KHWAN_API_KEY"):
        print("KHWAN_API_KEY is not set.", file=sys.stderr)
        return 2

    Khwan = None
    if args.commit:
        # Imported lazily: a dry run needs no client, and most runs are dry runs.
        try:
            from khwan import Khwan as _K
        except ModuleNotFoundError:
            print("This needs the Khwan client, which a dry run does not:\n"
                  "    pip install khwan", file=sys.stderr)
            return 2
        Khwan = _K

    grand = {"seen": 0, "durable": 0, "sent": 0, "skipped": 0, "refused": 0}
    total_sent = 0        # across all projects — what --limit counts

    for proj_dir, core in sorted(mapping.items()):
        # Case-insensitive: the directory name mirrors the path on disk, so
        # "acme" and "Acme" are the same project to anyone typing the flag.
        if args.project and args.project.lower() not in proj_dir.lower():
            continue
        if args.limit and total_sent >= args.limit:
            break
        proj = PROJECTS / proj_dir
        if not proj.is_dir():
            print(f"!! {proj_dir}: no such project directory", file=sys.stderr)
            continue

        # Oldest session first: Khwan ranks by similarity × confidence, and a
        # later turn correcting an earlier one must land AFTER it to win.
        sessions = sorted(proj.glob("*.jsonl"), key=lambda p: p.stat().st_mtime)
        repo_root = "/" + proj_dir.strip("-").replace("-", "/")
        done = already_done(core)
        kw = Khwan(api_key=os.environ["KHWAN_API_KEY"], core=core) if args.commit else None
        led = ledger_path(core)
        if args.commit:
            led.parent.mkdir(parents=True, exist_ok=True)

        n_sent = n_skip = n_ref = n_dur = n_seen = 0
        for sess in sessions:
            for i, (user_text, events) in enumerate(turns(sess)):
                n_seen += 1
                pair = distil(user_text, events, repo_root)
                if not pair:
                    continue
                n_dur += 1
                ask, outcome = pair
                key = f"{sess.stem}:{i}"
                if key in done:
                    n_skip += 1
                    continue
                if args.limit and total_sent >= args.limit:
                    break
                if not args.commit:
                    n_sent += 1
                    total_sent += 1
                    continue
                try:
                    turn = kw.prepare(ask)
                    if not turn.turn_token:
                        # Coherence gate declined this turn — nothing to record.
                        n_ref += 1
                        continue
                    kw.record(turn, outcome)
                except Exception as e:            # keep going; log and move on
                    print(f"   !! {key}: {e}", file=sys.stderr)
                    n_ref += 1
                    continue
                with led.open("a", encoding="utf-8") as f:
                    f.write(json.dumps({"key": key, "ask": ask[:120]},
                                       ensure_ascii=False) + "\n")
                n_sent += 1
                total_sent += 1
                # A turn costs seconds of round-trip, so a silent run looks hung.
                # Report each one, on a single rewritten line.
                el = time.monotonic() - t_start
                print(f"\r  {core}: sent {total_sent}"
                      + (f"/{args.limit}" if args.limit else "")
                      + f"  ({el / total_sent:.1f}s/turn, {el / 60:.1f} min elapsed)"
                      + " " * 8, end="", file=sys.stderr, flush=True)
                time.sleep(1.0 / args.rate)
            if args.limit and total_sent >= args.limit:
                break

        if args.commit and n_sent:
            print("\r" + " " * 78 + "\r", end="", file=sys.stderr)
        verb = "would send" if not args.commit else "sent"
        print(f"{core:12s} ← {proj_dir[:44]:44s} "
              f"turns {n_seen:5d}  durable {n_dur:5d}  {verb} {n_sent:5d}"
              f"  skip(done) {n_skip:4d}  refused {n_ref:3d}")
        for k, v in (("seen", n_seen), ("durable", n_dur), ("sent", n_sent),
                     ("skipped", n_skip), ("refused", n_ref)):
            grand[k] += v

    print("-" * 100)
    print(f"TOTAL  turns {grand['seen']}  durable {grand['durable']}  "
          f"{'would send' if not args.commit else 'sent'} {grand['sent']}  "
          f"skip {grand['skipped']}  refused {grand['refused']}")
    if not args.commit:
        print("\nDry run — nothing was written. Add --commit to send.")
        # Wall clock is set by the prepare+record round trip (~10 s/turn
        # observed against the hosted engine), not by --rate, whose sleep is a
        # second at most. Estimating from --rate alone understated a 25-minute
        # run as 45 seconds.
        lo, hi = grand["sent"] * 8 / 60, grand["sent"] * 14 / 60
        print(f"Cost if committed: {grand['sent'] * 2} operations "
              f"(prepare+record), roughly {lo:.0f}-{hi:.0f} min — the round trip "
              f"dominates, not --rate.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
