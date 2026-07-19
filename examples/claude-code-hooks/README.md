# Claude Code hooks — automatic Khwan memory

The MCP server (repo root) exposes Khwan as **tools** Claude *chooses* to call.
These hooks are the **deterministic** alternative: Khwan memory is pulled on
**every** prompt and every answer is recorded — no reliance on the model
deciding to call a tool.

You can run **either** approach, or both. Hooks give you always-on memory; the
MCP tools give Claude explicit, on-demand access (and work in Claude Desktop and
other MCP clients, where these Claude-Code-only hooks do not).

## How it works

| Hook event         | Script                    | Does                                                        |
| ------------------ | ------------------------- | ----------------------------------------------------------- |
| `UserPromptSubmit` | `khwan_prepare_hook.py`   | calls `prepare`, injects `<khwan-memory>` into the turn, stashes the `turn_token` |
| `Stop`             | `khwan_record_hook.py`    | reads Claude's last answer from the transcript, calls `record` against the stashed token |

The `turn_token` is stashed per session in your temp dir so the two hooks line
up on the same turn. Both hooks **fail open**: any error (missing key, network,
API error) is logged to stderr and never blocks your prompt.

## Setup

1. Install the `khwan` client so the hooks can import it:
   ```bash
   pip install khwan
   ```
2. Export your credentials in the shell that launches Claude Code:
   ```bash
   export KHWAN_API_KEY=kwk_live_xxx
   export KHWAN_CORE=default        # optional — pick an isolated brain
   # export KHWAN_USER=alice        # optional — isolated sub-brain (paid)
   # export KHWAN_BASE_URL=http://127.0.0.1:8010   # optional — local engine
   ```
3. Merge `settings.snippet.json` into your project's `.claude/settings.json`,
   replacing `/ABSOLUTE/PATH/TO/khwan-mcp` with your clone path.

That's it — next `claude` session, memory is injected on every prompt and
recorded on every answer.
