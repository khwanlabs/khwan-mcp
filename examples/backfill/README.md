# Backfill — seed a core from transcripts you already have

Khwan starts empty. If you have been working in Claude Code for months, that
history is sitting in `~/.claude/projects/<slug>/*.jsonl` and the brain has
never seen any of it — so the first weeks of `khwan_recall` return thin,
loosely-related results.

`backfill_claude_code.py` replays that history into a core. It calls no model:
each turn is distilled deterministically from its `tool_use` blocks.

## What it keeps

Two kinds of turn are kept.

**A turn that changed something** — edited a file, or ran a substantive command
(`git commit`, `alembic`, `railway`, `pytest`, …). This is the record of work.

**A turn that corrected something** — the user rejecting what was just done, or
stating how they want it done ("not that", "ไม่เอา", "always ask me first"). These
edit no file and run no command, so an earlier version of this script dropped
every one of them. That was the wrong call: a correction is the most durable thing
a session contains, and it is the one thing a rules file cannot capture, because a
rules file only ever holds what someone remembered to write down. Measured over
seven projects in one account, a single preference was restated **twelve times**
and dropped twelve times — which is also why it had to be restated twelve times.

Pure question-and-answer turns are still dropped: they leave nothing durable and
add a near-neighbour for a future query to trip over. That cost is lower than it
used to be — retrieval now has a relevance floor (`KHWAN_RETRIEVAL_MIN_SIMILARITY`,
0.20) so an unrelated packet returns nothing rather than the nearest thing in the
room — but a packet that says nothing is still not worth an operation.

Each kept turn becomes an input/response pair:

```
ask:      can we skip the login screen in local dev?
outcome:  FILES: components/AuthGate.tsx
          RAN: docker compose up -d db
          OUTCOME: the API already supports a dev-auth mode, fail-closed behind
                   its own env flag — only the frontend gate was missing …
```

## Use

```bash
# 0. the client, for --commit (a dry run needs nothing).
#    0.4+ to keep each turn's real timestamp; older still works, undated.
pip install "khwan>=0.4"

# 1. create every target core in the dashboard first (they are not auto-created)
# 2. map your project directories to those cores
cp cores.example.json cores.json && $EDITOR cores.json

# 3. look, change nothing
python3 backfill_claude_code.py --map cores.json

# 4. try one project, a slice at a time
export KHWAN_API_KEY=kwk_live_xxx
python3 backfill_claude_code.py --map cores.json --project Khwan --limit 20 --commit

# 5. the rest, paced to your plan (turns/sec; each turn costs 2 operations)
python3 backfill_claude_code.py --map cores.json --commit --rate 25
```

`--rate` must stay at half your plan's operations/sec: free 1, starter 5, pro 25.

## Notes

- **Oldest first.** Retrieval ranks by `similarity × confidence`, so a later turn
  that corrects an earlier one has to land after it. Sessions are ordered by
  mtime for exactly this reason.
- **Each packet keeps the time it happened.** The transcript timestamp is sent as
  `occurred_at`, so two months of history is stored as two months rather than as
  the twelve minutes the import took. Without it, ranking cannot tell a decision
  from June from one made this morning — which is the case a memory most needs to
  get right. Needs `khwan>=0.4` and an engine that accepts `occurred_at`; older
  ones ignore it and date the packet now, as before.
- **This trains the brain, it does not just load it.** Every `record` moves
  SimSelf axes and coherence, the same as a live turn.
- **Resumable.** Every successful turn is appended to
  `~/.khwan/backfill-<core>.jsonl`; re-running skips what is already in.
- **Dry run is the default.** Nothing is sent without `--commit`.
- **One brain per project, without spending a core.** A mapping value can be
  `{"core": "acme", "user": "Web"}`, which seeds `account::acme::@Web` — a fully
  separate brain, same isolation as a core. Nightly synthesis covers it: the
  worker sweeps `SELECT DISTINCT user_id FROM field_packets`, which is every
  brain key, sub-brains included.
- **The label is prefixed onto the stored ask** (`[Web] add the awards
  section…`). Retrieval embeds the ask ALONE — nothing in `FILES:`/`OUTCOME:`
  reaches the vector — so without the prefix, two sites asking "redo the icon"
  are indistinguishable to a search, and asking in one repo surfaces the other's
  work.
