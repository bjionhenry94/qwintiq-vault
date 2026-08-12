# Qwintiq Vault — Architecture & Tool Map

The vault is a remote **MCP server** that holds Qwintiq's four skill frameworks server-side and
returns **finished work product only**. A consultant's Claude Code calls task-shaped tools
("write this sequence", "size this list"); the framework text never leaves the vault.

```
Consultant's Claude Code             The Wall                The Vault (this server)
  "write Qwintiq copy"  ── MCP call ─►  OAuth login +  ──►  engine.run("copywriter", brief)
        │                               per-call key check     │  framework = system prompt
        ▼                                                      │  LLM runs SERVER-SIDE
  finished sequences  ◄── output only (leak-filtered) ◄────────┘  output filtered for leaks
                                     Control Panel (Aliyah) ── writes ─► Keyring DB
                                      (add/remove consultant, assign/revoke key)
```

## Decisions — LOCKED (Bjion, 2026-08-12)

1. **Execution model: finished-output-only, server-side, for ALL FOUR skills.**
   Host-and-serve (returning instruction text for the client to run) is dead for anything
   extraction-sensitive: instructions that transit the consultant's context can be surfaced,
   which fails the SOW §2 promise and the red-team gate. Instead every tool takes a brief and
   returns finished output. The copywriting frameworks, icebreaker method, list-building credit
   logic, and IPP/partner research method all stay server-side, always.
   - Consequence: the vault now DOES call an LLM (Anthropic API, Qwintiq's key at deploy,
     `ANTHROPIC_API_KEY` env). `VAULT_LLM=mock` runs a deterministic local mode for dev/tests.
2. **Data keys: the vault proxies AI-ARK (and any Lemlist push).** Qwintiq's keys sit in env
   on the server (`AI_ARK_API_KEY`), never in a consultant's Claude. `VAULT_AIARK=mock` for dev.

## Extraction defence (what Step 11 red-teams)

- **Frameworks never serialise into a tool response.** Tools return `engine.run()` output only.
- **Input hardening:** meta-requests ("print your instructions", "what's your system prompt",
  "repeat the above", role-play as admin) are refused *before* any LLM call — deterministic
  pattern gate, no model judgement involved.
- **Output leak filter:** every response is shingle-checked (8-word overlapping n-grams,
  case/whitespace-normalised) against the loaded framework text; any overlap → the response is
  refused and the attempt logged. Deterministic, testable with a deliberately leaky mock LLM.
- **Honest limit:** finished output necessarily *reflects* the method (a good sequence reveals
  what good looks like). What cannot be pulled is the framework text, rules, pricing logic, or
  prompts themselves. Position as an upgrade + real wall, per `qwintiq-vault-build` memory.

## The four skills → tools (task-shaped, finished-output-only)

| Skill | Tool | In | Out |
|---|---|---|---|
| Copywriter | `qwintiq_copywriter(brief…)` | problem, outcome, risk reversal, service, proof | the two Lemlist sequences |
| Icebreaker | `qwintiq_icebreaker(prospects…)` | prospect name/company + page text the client gathered | per-prospect icebreakers |
| List-building | `qwintiq_list_building(icp…)` | ICP description / filters | sized plan + AI-ARK counts (proxied) |
| Partner-signals | `qwintiq_partner_signals(routine…)` | routine name or params | finished signal report |

**State tools:** `qwintiq_setup_*` / `qwintiq_routine_*` — icebreaker setups and partner
routines persist in the vault DB (`framework_state`), never on a consultant's disk.

## The Wall (login gate + Keyring)

- **OAuth 2.1 authorization server, in-house, spec-minimal:** protected-resource metadata,
  AS metadata, dynamic client registration (RFC 7591), `/authorize` (the login screen:
  email + password), `/token` (code + PKCE → opaque access token, 30-day expiry ≈ the
  "Google-length session" agreed in the doc comments).
- **Per-call check:** every MCP request validates the bearer token AND the consultant's key
  row (`consultants.status = active`, `consultant_keys.active = true`). Revoking the key in
  the panel kills the live session on the very next call — no timeout wait.
- **Keys are server-side rows.** The consultant authenticates with email+password; the key is
  issued/revoked by Aliyah and is never displayed to, returned to, or typed by the consultant.

## Control panel (SOW §3 — two functions, nothing else)

Server-rendered pages at `/admin` (admin login, cookie session): add/remove consultant,
assign/revoke key. Adding a consultant generates a one-time temporary password Aliyah passes
on; no usage dashboard (SOW §5 exclusion).

## DB

One schema (`db/schema.sql`), env-switchable driver: `DATABASE_URL` set → Postgres
(Supabase, prod); unset → local SQLite file (dev). Same DAL surface either way.

## Build phases (mapped to SOW §8)

- **Phase 1 — Vault skeleton + copywriter (host-and-serve)** ✅ superseded by finished-output-only
- **Phase 2 — Four skills migrated, finished-output-only + AI-ARK proxy + vault state** ← now
- **Phase 3 — The Wall** (OAuth AS + per-call Keyring check)
- **Phase 4 — Keyring + Control Panel** → Visual round 1
- **Phase 5 — Deploy (Render + Supabase, Qwintiq accounts), custom domain** → Visual round 2
- **Phase 6 — Launch, onboard, recorded handover** (14-day warranty starts)

## Stack
Python + FastMCP (`mcp` 1.x, Streamable HTTP) mounted inside a Starlette app ·
`anthropic` SDK (server-side execution; lazy import, mock mode without it) ·
SQLite dev / Supabase Postgres prod · server-rendered HTML control panel ·
Render hosting · custom Qwintiq domain. All third-party accounts in Qwintiq's name (SOW §11).
