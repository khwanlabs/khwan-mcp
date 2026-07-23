# khwan-mcp

**Durable memory that survives the session.** An [MCP](https://modelcontextprotocol.io)
server that plugs [Khwan](https://khwan.ai) — a pure AI-memory layer — into
Claude Code, Claude Desktop, or any MCP client.

Khwan never runs a model. **The client is the model.** Its job is to persist and
distil what matters into a brain you can **recall in a later session or seed a
subagent with** — a compact, bounded set of facts instead of a replayed
transcript. One account can hold many isolated **cores** (brains), and — on paid
plans — an isolated sub-brain per end-user.

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
claude mcp add khwan \
  -e KHWAN_API_KEY=kwk_live_xxx \
  -e KHWAN_CORE=default \
  -- khwan-mcp
```

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

Add to `claude_desktop_config.json`:

```json
{
  "mcpServers": {
    "khwan": {
      "command": "khwan-mcp",
      "env": {
        "KHWAN_API_KEY": "kwk_live_xxx",
        "KHWAN_CORE": "default"
      }
    }
  }
}
```

## Configuration (environment)

| Var              | Required | Purpose                                                              |
| ---------------- | -------- | ------------------------------------------------------------------- |
| `KHWAN_API_KEY`  | yes      | Your key from the Khwan dashboard (`kwk_live_…`).                    |
| `KHWAN_CORE`     | no       | Select an isolated core/brain (default: the account's default core).|
| `KHWAN_USER`     | no       | Isolated sub-brain per end-user (paid); sets `X-Khwan-User`.         |
| `KHWAN_BASE_URL` | no       | Override the API base — e.g. `http://127.0.0.1:8010` for a local engine. |

## Tools

| Tool                              | When                                                              |
| --------------------------------- | ---------------------------------------------------------------- |
| `khwan_recall(query, limit=8)`    | **seed** a session/subagent — compact relevant facts + `seed_text`. |
| `khwan_remember(fact)`            | **persist** a durable fact/preference for future sessions.        |
| `khwan_prepare(input)`            | full loop, **before** answering — memory context + a `turn_token`. |
| `khwan_record(turn_token, answer)`| full loop, **after** answering — persists the turn so Khwan learns. |
| `khwan_memory(limit=20)`          | inspect what the brain currently remembers.                       |
| `khwan_cores()`                   | list the isolated cores on the account.                           |

`khwan_recall` / `khwan_remember` are the token-smart pair for a caching host;
`khwan_prepare` / `khwan_record` are the full loop for custom agents (pass the
exact `turn_token` from prepare back into record).

## Always-on memory (Claude Code hooks)

The tools above are called *when Claude decides to*. For **deterministic** memory
— no reliance on the model — use the hook preset in
[`examples/claude-code-hooks/`](examples/claude-code-hooks/): a `UserPromptSubmit`
hook injects memory on every prompt and a `Stop` hook records every answer.

> ⚠️ On a caching host this is the *thorough* option, not the *cheap* one — it
> adds per-turn tokens. Prefer it when recall reliability matters more than token
> cost (or on a non-caching client); otherwise use `khwan_recall` at session
> start.

## License

Proprietary — © Khwan Labs.
