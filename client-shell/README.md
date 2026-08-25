# The QwintiQ Shell (what a consultant sets up)

The consultant installs **nothing but the connector**. There are no skill files on their
machine — the vault exposes QwintiQ's capabilities as MCP tools that return **finished
work only**. The frameworks themselves never leave the server.

## Setup (per consultant, once)

1. Install Claude Code and sign in.
2. Add the QwintiQ connector:
   ```bash
   claude mcp add --transport http qwintiq https://vault.qwintiq.example/mcp
   ```
   (or copy `.mcp.json.example` into the project as `.mcp.json` with the real URL)
3. The first use opens the **QwintiQ sign-in screen** in the browser. They sign in with
   the email + password from their admin. That's the whole setup.

From then on they work in Claude Code as normal — "write QwintiQ copy", "size this
market", "run today's partner signals" — and Claude calls the matching vault tool. The
session lasts about as long as a normal Google sign-in. Revoke their key in the control
panel and the very next call fails, mid-session.

There is deliberately nothing else in this folder: no prompts, no frameworks, no rules.
That's the point.
