# khwan-mcp

**Give Claude a memory that learns.** An [MCP](https://modelcontextprotocol.io)
server that plugs [Khwan](https://khwan.ai) — a pure AI-memory layer — into
Claude Code, Claude Desktop, or any MCP client.

Khwan never runs a model. **The client is the model.** In Claude Code that means
Claude itself is the "your model" step of Khwan's only loop:

```
khwan_prepare(input)  →  Claude answers (grounded in memory)  →  khwan_record(answer)
```

Each turn is persisted and distilled into lessons, so the next `prepare` is
sharper. One account can hold many isolated **cores** (brains), and — on paid
plans — an isolated sub-brain per end-user.

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

Then, in a session, ask Claude to remember something — it will call
`khwan_prepare` before answering and `khwan_record` after. The server's
instructions tell Claude to run the loop; you can reinforce it in your project's
`CLAUDE.md` ("always call `khwan_prepare` before answering, `khwan_record`
after").

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

| Tool                              | When                                                        |
| --------------------------------- | ----------------------------------------------------------- |
| `khwan_prepare(input)`            | **before** answering — returns memory context + a `turn_token`. |
| `khwan_record(turn_token, answer)`| **after** answering — persists the turn so Khwan learns.    |
| `khwan_memory(limit=20)`          | inspect what the brain currently remembers.                 |
| `khwan_cores()`                   | list the isolated cores on the account.                     |

Pass the exact `turn_token` from `khwan_prepare` back into `khwan_record` — it
ties the answer to the right turn.

## Always-on memory (Claude Code hooks)

The tools above are called *when Claude decides to*. For **deterministic**
memory on every turn — no reliance on the model — use the hook preset in
[`examples/claude-code-hooks/`](examples/claude-code-hooks/): a
`UserPromptSubmit` hook injects memory on every prompt and a `Stop` hook records
every answer. Run either approach, or both.

## License

Proprietary — © Khwan Labs.
