# khwan-mcp

**Durable memory that survives the session.** An [MCP](https://modelcontextprotocol.io)
server that plugs [Khwan](https://khwan.ai) — a pure AI-memory layer — into
Claude Code, Claude Desktop, or any MCP client.

Khwan never runs a model. **The client is the model.** Its job is to persist and
distil what matters into a brain you can **recall in a later session or seed a
subagent with** — a compact, bounded set of facts instead of a replayed
transcript. One account can hold many isolated **cores** (brains), and — on paid
plans — an isolated sub-brain per end-user.

<!-- The MCP Registry verifies ownership of a PyPI package by finding this
     name in the package README, which is what PyPI shows as the description.
     It must match `name` in server.json, and it only reaches PyPI on the next
     release — so do not rename one without re-releasing. -->
<!-- mcp-name: ai.khwan/khwan-mcp -->

## How it saves tokens (and where it doesn't)

Be honest about the mechanism — an MCP **adds** to a host's context, it cannot
**replace** the transcript the host already sends. So:

- **Within one hot session, it does not save tokens.** Claude Code caches its
  growing history (cache reads ≈ 0.1×), so re-injecting memory every turn only
  adds. Don't do that here.
- **Across sessions and subagents, it does.** A cache dies in minutes; a session
  ends. Khwan persists distilled facts so the *next* run recalls them cheaply —
  no cold-replay of an old transcript, and facts that already scrolled out of
  context are retrievable again.

The token-smart pattern: **seed once, remember durable facts** (below), rather
than running the full loop on every turn of a caching host. The full
`prepare → record` loop still shines in a **custom agent on a non-caching host**,
where replacing history with distilled memory bounds per-turn cost directly.

## Install

```bash
pip install khwan-mcp          # or: uvx khwan-mcp
```

## Connect to Claude Code

```bash
claude mcp add khwan --scope project \
  -e KHWAN_CORE=default \
  -- khwan-mcp
```

`--scope project` writes `.mcp.json` into the repo, so the setting travels with
the project. Note what is **not** in that command: the key.

### Keeping the key out of the repo

`claude mcp add -e KHWAN_API_KEY=…` writes the literal value into `.mcp.json` —
a file whose whole point is being committed. Two ways to avoid that, and the
second is the one that works everywhere:

**Shell environment.** Leave `KHWAN_API_KEY` out of the config entirely and
export it in the shell that launches `claude`. The server inherits it.

```bash
export KHWAN_API_KEY=kwk_live_xxx
```

**A launcher (works in the desktop app too).** A desktop app is started from a
dock or menu, not a login shell, so it inherits none of your shell exports and
the approach above silently yields no key. Read it from a file instead:

```bash
mkdir -p ~/.khwan && chmod 700 ~/.khwan
printf 'KHWAN_API_KEY=kwk_live_xxx\n' > ~/.khwan/env && chmod 600 ~/.khwan/env

cat > ~/.khwan/khwan-mcp <<'SH'
#!/bin/sh
set -a
[ -f "$HOME/.khwan/env" ] && . "$HOME/.khwan/env"
set +a
exec khwan-mcp "$@"
SH
chmod 700 ~/.khwan/khwan-mcp
```

Then point the config at the launcher and keep only non-secret settings inline:

```bash
claude mcp add khwan --scope project \
  -e KHWAN_CORE=acme -e KHWAN_USER=Web \
  -- ~/.khwan/khwan-mcp
```

`.mcp.json` is now safe to commit, and every new repo costs two lines instead of
a pasted key. Anyone else on the team writes their own `~/.khwan/env`.

## One brain per project

Memory is only useful if the right project's memory comes back. Two axes, and
both give **complete** isolation:

| | selected by | free | paid |
|---|---|---|---|
| **core** | `KHWAN_CORE` | 1 — `default` only | 5 (starter) → 25 (pro) |
| **sub-brain** | `KHWAN_USER` | **3** | unlimited |

A sub-brain is a full separate brain, not a filter: `account::@web` shares
nothing with `account::@api`. So the two axes multiply, and **a free account
already holds four isolated brains** — the core on its own, plus three
sub-brains:

```
account              default core, no KHWAN_USER      brain 1
account::@web        KHWAN_USER=web                   brain 2
account::@api        KHWAN_USER=api                   brain 3
account::@docs       KHWAN_USER=docs                  brain 4
```

Which means one-brain-per-project works on the free plan, for up to four
projects — **and it needs no `KHWAN_CORE` at all**:

```bash
# in ~/code/web
claude mcp add khwan --scope project -e KHWAN_USER=web -- ~/.khwan/khwan-mcp
# in ~/code/api
claude mcp add khwan --scope project -e KHWAN_USER=api -- ~/.khwan/khwan-mcp
```

Named cores are the paid axis. Reach for one when four brains stop being
enough, or when you want them grouped per client rather than per repository:

```bash
# in ~/code/acme-web
claude mcp add khwan --scope project -e KHWAN_CORE=acme -e KHWAN_USER=Web -- ~/.khwan/khwan-mcp
# in ~/code/acme-api
claude mcp add khwan --scope project -e KHWAN_CORE=acme -e KHWAN_USER=Api -- ~/.khwan/khwan-mcp
```

Two things to know before you point `KHWAN_CORE` anywhere. **A core must exist
first** — an unknown slug answers `404`, not "created it for you" — and they are
created in the dashboard. **On the free plan there is nothing to point at**: the
cap of one is spent on `default`, so creating a named core answers `402`. Leave
`KHWAN_CORE` unset there and use `KHWAN_USER`. Sub-brains, by contrast, are
created on first write.

### Recommended pattern (token-smart)

On a caching host like Claude Code, prefer **seed + remember** over the per-turn
loop:

1. **Seed** at the start of a session or subagent:
   > "Call `khwan_recall(query="<the task>")` and use the returned `seed_text` as
   > context."
2. **Remember** durable facts as they emerge:
   > "That's a standing decision — call `khwan_remember(fact="…")`."

Reinforce it in your project's `CLAUDE.md`, e.g.:

```md
- At the start of a task, call `khwan_recall` to seed relevant memory.
- When a durable decision/preference/fact emerges, call `khwan_remember`.
- Don't call prepare/record every turn — it adds tokens without saving them here.
```

**Seeding a subagent** is where the win is clearest — hand it a bounded brief
instead of the whole transcript:

> "Recall deploy memory with `khwan_recall(query="deploy runbook")`, then spawn a
> subagent whose brief is that `seed_text` plus the task."

## Connect to Claude Desktop

Claude Desktop and Claude Code keep **separate** MCP configuration — a server
added to one is invisible to the other, and `claude mcp add` does not touch this
file. Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "khwan": {
      "command": "/Users/you/.khwan/khwan-mcp",
      "env": {
        "KHWAN_CORE": "acme",
        "KHWAN_USER": "Web"
      }
    }
  }
}
```

Use an absolute path: a desktop app does not get your shell's `PATH` either, so
a bare `khwan-mcp` may not resolve. One core is selected for the whole app —
there is no per-project switch here, so choose a broad one.

## Configuration (environment)

| Var              | Required | Purpose                                                              |
| ---------------- | -------- | ------------------------------------------------------------------- |
| `KHWAN_API_KEY`  | yes      | Your key from the Khwan dashboard (`kwk_live_…`).                    |
| `KHWAN_CORE`     | no       | Select a named core. Paid plans only — free has just `default`.     |
| `KHWAN_USER`     | no       | A separate brain inside the core — 3 on free, unlimited on paid.    |
| `KHWAN_BASE_URL` | no       | Override the API base — e.g. `http://127.0.0.1:8010` for a local engine. |

## Tools

| Tool                              | When                                                              |
| --------------------------------- | ---------------------------------------------------------------- |
| `khwan_recall(query, limit=3)`    | **seed** a session/subagent — synthesised `lessons` + up to 3 relevant facts, as `seed_text`. |
| `khwan_remember(fact)`            | **persist** a durable fact/preference for future sessions.        |
| `khwan_prepare(input)`            | full loop, **before** answering — memory context + a `turn_token`. |
| `khwan_record(turn_token, answer)`| full loop, **after** answering — persists the turn so Khwan learns. |
| `khwan_memory(limit=20)`          | inspect what the brain currently remembers.                       |
| `khwan_cores()`                   | list the isolated cores on the account.                           |

`khwan_recall` / `khwan_remember` are the token-smart pair for a caching host;
`khwan_prepare` / `khwan_record` are the full loop for custom agents (pass the
exact `turn_token` from prepare back into record).

### What comes back, and what an empty answer means

`khwan_recall` returns at most **three** facts — that ceiling is the server's,
so `limit` can lower it but not raise it — plus any `lessons` synthesis has
distilled from many past turns. Lessons lead the `seed_text`: a rule earned over
months outranks a single turn that happens to sit nearby in the index.

Retrieval applies a relevance floor, so **an empty `facts` is an answer**: the
brain has nothing close to this question. Read it as "not known here" rather than
as a failure, and do not fill the gap by leaning on whichever fact was nearest.

The floor is deliberately loose, because a memory wrongly dropped is invisible
while a memory wrongly kept is not. Expect a returned fact to be *plausibly*
related, not certainly relevant — read it before relying on it.

## Seeding a brain from work you have already done

A new brain knows nothing, so its first weeks of recall are thin — while the
answers are often already sitting in the host's own transcripts, unread.
[`examples/backfill/`](examples/backfill/) replays Claude Code transcripts into a
brain: deterministic, no model calls, dry-run by default.

```bash
python3 examples/backfill/backfill_claude_code.py --map cores.json
```

## Always-on memory (Claude Code hooks)

The tools above are called *when Claude decides to*. For **deterministic** memory
— no reliance on the model — use the hook preset in
[`examples/claude-code-hooks/`](examples/claude-code-hooks/): a `UserPromptSubmit`
hook injects memory on every prompt and a `Stop` hook records every answer.

> ⚠️ On a caching host this is the *thorough* option, not the *cheap* one — it
> adds per-turn tokens. Prefer it when recall reliability matters more than token
> cost (or on a non-caching client); otherwise use `khwan_recall` at session
> start.

## Source

[github.com/khwanlabs/khwan-mcp](https://github.com/khwanlabs/khwan-mcp) — this
server runs on your machine, with your key, reading what you type. Read it before
you install it.

## License

MIT — © Khwan Labs. See [LICENSE](LICENSE).
