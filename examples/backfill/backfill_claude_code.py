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

``cores.json`` maps a Claude Code project directory to the brain to seed. A brain
is a core, optionally narrowed to one end-user sub-brain (``account::core::@user``,
which is a fully separate brain — same isolation as a core, without spending one):

    {
      "-Users-you-Desktop-acme-web": {"core": "acme", "user": "Web"},
      "-Users-you-Desktop-acme-api": {"core": "acme", "user": "Api"},
      "-Users-you-Desktop-side-project": "side"
    }

Cores are NOT auto-created — create each one in the dashboard first, or the API
answers 404 "unknown core". Sub-brains ARE created on first write, but they are a
paid-plan feature.

Requires the Khwan client for ``--commit`` — ``pip install 'khwan>=0.4.0'``, which
is the first version whose ``record()`` takes ``occurred_at``. An older client
refuses every turn one warning at a time; the run then exits non-zero rather than
looking like a finished import. A dry run needs nothing but the standard library.

Env: KHWAN_API_KEY (required), KHWAN_BASE_URL (optional).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from datetime import datetime
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

# A human rejecting, correcting or restating a preference. These turns change no
# file and run no command, so the gate in distil() used to drop every one of them
# — and they are the most durable thing anyone says. Measured over seven projects
# in one account: a single preference was restated TWELVE times and dropped twelve
# times, which is also why it had to be restated twelve times.
#
# The engine classifies these too (khwan_main._CORRECTION_MARKERS) — but only for
# turns that reach it, and this filter runs first. Kept as its own list because
# this script is stdlib-only by design and the engine is a private package.
#
# Two markers from the engine's list are deliberately NOT here, both false friends
# in Thai: "เปล่า" is also the tail of the question particle "…รึเปล่า" (= "…or not?"),
# and "แทน" sits inside "ตัวแทน" (= agent/representative), a common word in Thai
# commercial copy. Bare "not " is out for the same reason: it fires on build logs
# and on console reports pasted into a turn.
CORRECTING = re.compile(
    r"ไม่ใช่|ไม่ถูก|ไม่เอา|ไม่อยาก|ไม่ต้อง|ไม่ชอบ|ยังไม่|ผิด|แก้ใหม่|ที่จริง|จริงๆ|จริง ๆ|"
    r"อย่า|ห้าม|ทุกครั้ง|เสมอ|ต่อไปนี้|จากนี้|ขอเป็น|เอาแบบ|อยากได้|ชอบแบบ|"
    r"\bno,|that'?s not|that is not|\bwrong\b|incorrect|\bactually\b|i meant|"
    r"you'?re wrong|rather,|\bdon'?t\b|\bnever\b|\balways\b|instead|\bprefer\b|\bstop\b",
    re.IGNORECASE)


# ── distillation ──────────────────────────────────────────────────────────────

def turns(path: Path) -> Iterator[tuple[str, list, Optional[str]]]:
    """Yield (user_text, events, timestamp) per human turn, in transcript order.

    The timestamp is when the turn HAPPENED, off the transcript entry. Without it
    every packet is dated the minute the import ran, and months of work collapses
    into however long the import took — measured once at two months of history
    stored as twelve minutes. Retrieval then cannot tell a decision from June from
    one made this morning.
    """
    cur_user: Optional[str] = None
    cur_ts: Optional[str] = None
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
                yield cur_user, events, cur_ts
            cur_user, events, cur_ts = text, [], entry.get("timestamp")
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
        yield cur_user, events, cur_ts


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
    """(ask, outcome) for a turn worth remembering — else None.

    Two kinds qualify. A turn that CHANGED something (edited a file, ran a real
    command) leaves a record of work. A turn that CORRECTED something leaves a
    standing preference — and it edits nothing, which is why it used to be dropped.

    A turn that merely answered a question is still skipped: it leaves nothing
    durable and only adds a near-neighbour for future queries to trip over.
    """
    files = sorted({v for k, v in events if k == "edit"}
                   - {v for k, v in events if k == "edit" and TRANSIENT.search(v)})
    ran = [c for c in dict.fromkeys(v for k, v in events if k == "ran") if c]
    said = [v for k, v in events if k == "said"]
    if not files and not ran:
        # Nothing was changed — but being told "not that, this" is the highest-signal
        # thing a session contains, and it never touches a file. Require an answer
        # too, so a bare complaint with no reply attached is not stored as a fact.
        if not (said and CORRECTING.search(user_text)):
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

def _parse_ts(raw: Optional[str]) -> Optional[datetime]:
    """Transcript timestamp → datetime, or None when it is missing or unreadable.

    None is fine: the server dates the packet now, which is what happened before
    this existed. A malformed timestamp is not worth failing an import over.
    """
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except (TypeError, ValueError):
        return None


def brain_of(value) -> dict:
    """Normalise a cores.json value into {core, user, label}.

    A bare string stays valid — it is a core with no sub-brain.
    """
    if isinstance(value, str):
        return {"core": value, "user": None, "label": value}
    core = value["core"]
    user = value.get("user") or None
    return {"core": core, "user": user, "label": value.get("label") or user or core}


def ledger_path(brain: dict) -> Path:
    """One ledger per BRAIN. Keying it on the core alone would let a re-run skip
    turns "already sent" that in fact went to a different sub-brain."""
    name = brain["core"] + (f"__{brain['user']}" if brain["user"] else "")
    safe = "".join(c if c.isalnum() or c in "-_." else "_" for c in name)
    return Path.home() / ".khwan" / f"backfill-{safe}.jsonl"


def already_done(brain: dict) -> set[str]:
    """Turn keys this brain has already ingested — makes a re-run a no-op."""
    p = ledger_path(brain)
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

    for proj_dir, target in sorted(mapping.items()):
        brain = brain_of(target)
        core, label = brain["core"], brain["label"]
        # Case-insensitive: the directory name mirrors the path on disk, so its
        # casing is whatever the folder used, not what anyone types at the flag.
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
        done = already_done(brain)
        kw = None
        if args.commit:
            kw_args = {"api_key": os.environ["KHWAN_API_KEY"], "core": core}
            if brain["user"]:
                kw_args["user_id"] = brain["user"]     # → account::core::@user
            kw = Khwan(**kw_args)
        led = ledger_path(brain)
        if args.commit:
            led.parent.mkdir(parents=True, exist_ok=True)

        n_sent = n_skip = n_ref = n_dur = n_seen = 0
        for sess in sessions:
            for i, (user_text, events, occurred) in enumerate(turns(sess)):
                n_seen += 1
                pair = distil(user_text, events, repo_root)
                if not pair:
                    continue
                n_dur += 1
                ask, outcome = pair
                ask = f"[{label}] {ask}"
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
                    kw.record(turn, outcome, occurred_at=_parse_ts(occurred))
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
                print(f"\r  {label}: sent {total_sent}"
                      + (f"/{args.limit}" if args.limit else "")
                      + f"  ({el / total_sent:.1f}s/turn, {el / 60:.1f} min elapsed)"
                      + " " * 8, end="", file=sys.stderr, flush=True)
                time.sleep(1.0 / args.rate)
            if args.limit and total_sent >= args.limit:
                break

        if args.commit and n_sent:
            print("\r" + " " * 78 + "\r", end="", file=sys.stderr)
        verb = "would send" if not args.commit else "sent"
        shown = core + (f"::@{brain['user']}" if brain["user"] else "")
        print(f"{shown[:24]:24s} ← {proj_dir[:38]:38s} "
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

    # A commit that wrote NOTHING is an outage, not a quiet success. Every turn is
    # wrapped in a try/except so one bad row cannot end the run — which also means
    # a fault affecting EVERY row (an expired token, a client too old for the
    # server, a core that does not exist) is reported one harmless "!!" at a time
    # and then exits 0. That happened: a 0.2.0 client against a script passing
    # `occurred_at` refused all 266 turns, printed a per-turn warning nobody reads
    # at that volume, and finished looking like a completed import. Say it once,
    # loudly, and exit non-zero so a caller finds out.
    if grand["refused"] and not grand["sent"]:
        print(f"\n!! NOTHING WAS WRITTEN — all {grand['refused']} turns were refused.\n"
              f"   A fault that hits every turn is not a bad row. Check, in order:\n"
              f"     - is the installed `khwan` client new enough for this script?\n"
              f"     - does the core in --map exist? (cores are never auto-created)\n"
              f"     - is KHWAN_API_KEY valid for that account?",
              file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
