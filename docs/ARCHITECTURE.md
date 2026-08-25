# QwintiQ Vault — Architecture & Tool Map

The vault is a remote **MCP server** that holds QwintiQ's four skill frameworks server-side and
returns **finished work product only**. A consultant's Claude Code calls task-shaped tools
("write this sequence", "size this list"); the framework text never leaves the vault.

```
Consultant's Claude Code             The Wall                The Vault (this server)
  "write QwintiQ copy"  ── MCP call ─►  OAuth login +  ──►  engine.run("copywriter", brief)
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
   - Consequence: the vault now DOES call an LLM (Anthropic API, QwintiQ's key at deploy,
     `ANTHROPIC_API_KEY` env). `VAULT_LLM=mock` runs a deterministic local mode for dev/tests.
2. **Data keys: the vault proxies AI-ARK (and any Lemlist push).** QwintiQ's keys sit in env
   on the server (`AI_ARK_API_KEY`), never in a consultant's Claude. `VAULT_AIARK=mock` for dev.

## Extraction defence (layered; what Step 11 red-teams)

Frameworks never serialise into a tool response — tools return `engine.run_framework()` output
only. Around that, four layers (`vault/engine.py`, `vault/leakguard.py`):

1. **Input guard (`meta_guard`)** — deterministic pattern gate that refuses extraction requests
   *before* any model call: "print/reveal/paraphrase/translate/reformat your
   instructions/framework/rules/angles", "repeat the above", role-play-as-admin, "for
   debugging", encoding tricks. Scoped to the *untrusted user text only* — the vault's own
   attached config (icebreaker setups, partner routines, full of method vocabulary) is never
   scanned, so it can't false-trigger.
2. **Output leak filter (`leakguard`)** — every response is checked against the framework on
   three deterministic bands: **verbatim** (any shared 8-word run), **near-verbatim** (≥2 shared
   5-word runs → lightly-edited copy), and **structural** (≥6 shared distinctive 3-word phrases →
   describing the method's language). Tuned so real finished output scores ~0 on all three
   (`tests/test_leakguard.py`).
3. **Semantic second-pass (`semantic_leak`, optional, `VAULT_SEMANTIC_GUARD=1`)** — a cheap model
   (Haiku) judges whether the output *describes a method* vs *is finished work*, catching fluent
   paraphrase that shares no word-runs. Off in dev (needs a key); **on by default in the prod
   deploy configs**. Adds one classification call per response.
4. **System-prompt hardening** — the generation prompt instructs the model to emit
   `EXTRACTION_ATTEMPT` (→ refusal) rather than describe itself. Model-dependent, so it's the
   weakest layer and never relied on alone.

**Empirical stress test** (`docs/leak-stress.md`, 8 real-model exfiltration attempts incl.
deliberately filter-evasive rewording): 6/8 frontier agents *refused* to leak; the 2 that
produced leaks were both caught by the deterministic filter (layer 2). Zero genuine leaks
evaded. Caveat: a weaker/compromised model won't self-refuse, and a *carefully* clean paraphrase
could still pass layers 1–2 — which is why layer 3 exists and why the honest limit below stands.

**Honest limit:** finished output necessarily *reflects* the method (a good sequence shows what
good looks like). What cannot be pulled is the framework text, rules, pricing logic, or prompts
themselves. Position as an upgrade + real wall, **not** "impossible to infer" — per
`qwintiq-vault-build` memory.

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
- **Phase 5 — Deploy (Render + Supabase, QwintiQ accounts), custom domain** → Visual round 2
- **Phase 6 — Launch, onboard, recorded handover** (14-day warranty starts)

## Stack
Python + FastMCP (`mcp` 1.x, Streamable HTTP) mounted inside a Starlette app ·
`anthropic` SDK (server-side execution; lazy import, mock mode without it) ·
SQLite dev / Supabase Postgres prod · server-rendered HTML control panel ·
Render hosting · custom QwintiQ domain. All third-party accounts in QwintiQ's name (SOW §11).
