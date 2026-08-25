# QwintiQ Vault

The **"QwintiQ Box"** from the Navreo IP-shield SOW — a remote **MCP server** that **hosts
QwintiQ's skills** and serves them on demand to a consultant's Claude Code, which runs them. The
skills are never files on a consultant's machine; access is gated behind a login and revocable in
one click. The MCP server auto-delivers the skills — consultants install and manage nothing.

See [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md) for the full tool map, what this does and
doesn't protect, and the phase plan.

## Status
- **Phase 1 (this scaffold):** vault skeleton + first hosted skill, `qwintiq_copywriter`. **No
  auth wall yet** — do not expose publicly until Phase 3 (anyone with the URL could pull the skill).

## Layout
```
server.py                 MCP server (ping + qwintiq_copywriter)
vault/library.py          serves a skill's instructions to the caller (never executes them)
vault/frameworks/*.md     the crown jewels — the skills themselves, held behind the wall
client-shell/             what a consultant sets up (just the connector — no skill files)
db/schema.sql             the Keyring (Phase 4)
docs/ARCHITECTURE.md      full tool map + open decisions + phases
```

## Run locally
```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python server.py              # Streamable HTTP on http://localhost:8000/mcp
```
No API keys needed to run — the vault serves instructions, it does not call an LLM.

## Connect Claude Code (local test)
```bash
claude mcp add --transport http qwintiq-local http://localhost:8000/mcp
```
Then in Claude Code: call `ping` (returns `qwintiq-vault: online`), or say "write QwintiQ copy" —
Claude calls `qwintiq_copywriter`, receives the instructions, and runs the skill.
