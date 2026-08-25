"""The vault's MCP tools — the ONLY surface a consultant's Claude sees.

Every tool takes a brief and returns finished work. Framework text never appears in a
response (engine.run_framework guards + filters). The AI-ARK credit gate is enforced
HERE, server-side: no export happens unless the typed confirmation phrase carries the
exact row count being pulled.
"""
from __future__ import annotations

import csv
import functools
import io
import json
import logging
import os
import re

from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings

from auth.context import current_consultant
from db import dal
from vault import aiark, lemlist
from vault.aiark import DataUnavailable
from vault.engine import REFUSAL, meta_guard, run_framework

_log = logging.getLogger("qwintiq.vault")

# Curtain guard: NOTHING about the vault's internals may reach a consultant through an error.
# Without this, an unhandled exception is surfaced verbatim by the MCP layer ("Error executing
# tool …: Client error '401' for url 'https://api.ai-ark.com/…'") — leaking the data provider,
# endpoints, the model behind the skills, DB errors, etc. Every tool is wrapped so the real cause
# is logged server-side (admin only) and the consultant sees a generic, internals-free message.
_SAFE_ERROR = ("Something didn't go through on the QwintiQ side just now. Please try again in a "
               "moment — if it keeps happening, let your QwintiQ admin know. Nothing to fix on your end.")
_DATA_ERROR = ("The data lookup is temporarily unavailable. Please try again shortly — if it "
               "persists, your QwintiQ admin needs to check the vault's data connection.")


def _safe(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except DataUnavailable:
            _log.warning("data lookup failed in %s", fn.__name__)
            return _DATA_ERROR
        except Exception:
            _log.exception("tool %s failed", fn.__name__)  # full detail stays in the server log
            return _SAFE_ERROR
    return wrapper

# FastMCP auto-enables DNS-rebinding protection (Host allow-list = localhost only) whenever its
# host is 127.0.0.1 — which silently 421s every request once deployed on a real hostname behind a
# proxy. That protection guards browser/localhost servers against malicious web pages; it does NOT
# apply here, because every /mcp call already passes our OAuth wall (a bearer token checked per
# request in auth.oauth.AuthGate). So we disable the Host check by default and let it run behind a
# proxy on any hostname. Set VAULT_ALLOWED_HOSTS="host1,host2:*" to re-enable it explicitly.
_allowed = os.environ.get("VAULT_ALLOWED_HOSTS", "").strip()
if _allowed:
    _security = TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=[h.strip() for h in _allowed.split(",") if h.strip()],
        allowed_origins=[h.strip() for h in _allowed.split(",") if h.strip()],
    )
else:
    _security = TransportSecuritySettings(enable_dns_rebinding_protection=False)

mcp = FastMCP("qwintiq-vault", transport_security=_security)

_CONFIRM_RE = re.compile(r"i\s+confirm\s+to\s+export\s+this\s+and\s+use\s+([\d,]+)\s+amount\s+of\s+credits",
                         re.IGNORECASE)


def _cid() -> str | None:
    c = current_consultant()
    return c["id"] if c else None


@mcp.tool()
@_safe
def ping() -> str:
    """Health check. Confirms the QwintiQ vault is reachable and responding."""
    return "qwintiq-vault: online"


@mcp.tool()
@_safe
def qwintiq_copywriter(problem: str, outcome: str, risk_reversal: str, service: str,
                       proof: str = "", icebreaker_note: str = "") -> str:
    """Write QwintiQ outreach copy — the finished email + LinkedIn sequences, Lemlist-ready.

    Collect the brief from the user first (the problem the prospect has, the outcome on
    offer, the risk reversal / guarantee, what the service is, optional proof), then call
    this once. It returns the two finished sequences; you present them as-is.
    """
    task = json.dumps({"problem": problem, "outcome": outcome, "risk_reversal": risk_reversal,
                       "service": service, "proof": proof, "icebreaker_note": icebreaker_note})
    out = run_framework("copywriter", task, _cid(), "qwintiq_copywriter")
    # The {{icebreaker}} merge slot is a hard contract (filled per lead at upload). If the
    # model wrote an opener instead, retry once with a corrective reminder appended.
    from vault.engine import REFUSAL
    if "{{icebreaker}}" not in out and out != REFUSAL:
        task_retry = json.dumps({"problem": problem, "outcome": outcome,
                                 "risk_reversal": risk_reversal, "service": service,
                                 "proof": proof, "icebreaker_note": icebreaker_note,
                                 "REMINDER": "Message 1 of BOTH sequences must open with the "
                                             "literal line {{icebreaker}} — the verbatim merge "
                                             "variable, never a written-out opener."})
        retry = run_framework("copywriter", task_retry, _cid(), "qwintiq_copywriter")
        if "{{icebreaker}}" in retry:
            return retry
    return out


@mcp.tool()
@_safe
def qwintiq_icebreaker(prospects: list[dict], setup_name: str = "Qwintiq partnership icebreakers") -> str:
    """Write personalised opening lines for partnership outreach prospects.

    For each prospect pass: name, company, and page_text (paste the visible text of their
    website / LinkedIn page — gather it with your own browsing, the vault does the rest).
    Returns one finished icebreaker per prospect; lines with no verified hook come back
    flagged honestly. Nothing is ever invented.
    """
    setup = dal.state_get(_cid(), "icebreaker_setup", setup_name)
    task = json.dumps({"prospects": prospects, "setup": setup["config"] if setup else None})
    # Guard only the user-supplied prospects; the setup config is trusted vault data.
    return run_framework("icebreaker", task, _cid(), "qwintiq_icebreaker",
                         guard_text=json.dumps(prospects))


@mcp.tool()
@_safe
def qwintiq_list_count(what_you_sell: str, industry: str, country: str,
                       size_min: int | None = None, size_max: int | None = None,
                       roles: str = "", keywords: list[str] | None = None,
                       exclude_keywords: list[str] | None = None,
                       seniorities: list[str] | None = None,
                       departments: list[str] | None = None,
                       titles: list[str] | None = None,
                       exclude_titles: list[str] | None = None) -> str:
    """Size a market: how many companies match the brief, and how many decision-makers
    inside them. Cheap (about a credit per count) and safe — this never exports rows.

    Collect the brief in plain English (what they sell, the vertical, country, size band,
    the decision-maker roles, exclusions), play it back for a yes, then call this. Report
    the numbers plainly. Exporting actual rows is a separate, gated step — quote the user
    the confirmation sentence this returns and wait for them to type it themselves.
    """
    for txt in (what_you_sell, industry, roles):
        if meta_guard(txt or ""):
            dal.log_extraction(_cid(), "qwintiq_list_count", "meta_guard", txt)
            return REFUSAL
    filters = {"industry": industry, "country": country, "size_min": size_min,
               "size_max": size_max, "keywords": keywords or [],
               "exclude_keywords": exclude_keywords or [], "seniorities": seniorities or [],
               "departments": departments or [], "titles": titles or [],
               "exclude_titles": exclude_titles or []}
    companies = aiark.count_companies(filters)
    people = aiark.count_people(filters)
    mock_note = " (dev mock numbers)" if companies.get("mock") else ""
    return json.dumps({
        "companies_matching": companies["total"],
        "decision_makers_matching": people["total"],
        "company_sample": companies["sample"],
        "person_sample": people["sample"],
        "note": f"Counts only — nothing exported, roughly 2 credits used{mock_note}.",
        "to_export": ("Ask the user to choose a scope (all, or a capped batch), then have "
                      "them type EXACTLY: 'I confirm to export this and use X amount of "
                      "credits' where X is the row count. Pass their typed sentence to "
                      "qwintiq_list_export. Never type it for them."),
        "filters_used": filters,
    })


@mcp.tool()
@_safe
def qwintiq_list_export(kind: str, filters: dict, max_rows: int, confirmation_phrase: str) -> str:
    """Export the confirmed list (kind: 'companies' or 'decision_makers') as CSV text.

    HARD GATE: this only runs if confirmation_phrase is the sentence the USER typed —
    'I confirm to export this and use X amount of credits' — and X equals max_rows.
    The vault validates it server-side and refuses otherwise. One confirmation = one export.
    """
    m = _CONFIRM_RE.search(confirmation_phrase or "")
    if not m:
        return ("EXPORT REFUSED: the confirmation sentence is missing or not in the agreed "
                "form. Show the user the exact sentence with the real number and wait for "
                "them to type it themselves.")
    confirmed = int(m.group(1).replace(",", ""))
    if confirmed != max_rows:
        return (f"EXPORT REFUSED: the user confirmed {confirmed} credits but the export asks "
                f"for {max_rows} rows. Re-quote the correct number and re-confirm.")
    rows = aiark.export_rows(filters, "companies" if kind == "companies" else "people", max_rows)
    seen, deduped = set(), []
    for r in rows:
        key = (r.get("website"), r.get("full_name"))
        if key not in seen:
            seen.add(key)
            deduped.append(r)
    cols = (["company_name", "website", "country", "employee_count", "industry", "linkedin"]
            if kind == "companies"
            else ["full_name", "title", "company_name", "website", "country", "linkedin"])
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(deduped)
    receipt = (f"Exported {len(deduped)} {kind.replace('_', ' ')} · used about {len(rows)} "
               f"credits · confirmed at {confirmed} credits.")
    return json.dumps({"csv": buf.getvalue(), "rows": len(deduped),
                       "credits_estimate": len(rows), "receipt": receipt,
                       "note": "Save this CSV for the user, then show them the 'receipt' line "
                               "verbatim as a plain-English record of what was pulled and spent."})


@mcp.tool()
@_safe
def qwintiq_partner_signals(routine_name: str = "", candidate_companies: list[dict] | None = None,
                            confirmation_phrase: str = "") -> str:
    """Run QwintiQ's daily partner/PR signal routine.

    Call with no arguments to see the saved routines. Call with routine_name to run one:
    pass candidate_companies (name + website + what happened, from your own free web
    search using the routine's search terms) and the vault qualifies them, counts the
    right decision-makers, and — only after the user types the confirmation sentence with
    the real number (Supervised mode) — pulls the people. Autopilot routines carry their
    own daily credit cap and skip the daily stop, per the user's standing permission.
    """
    cid = _cid()
    if not routine_name:
        routines = dal.state_list(cid, "partner_routine")
        return json.dumps({"routines": [r["name"] for r in routines],
                           "note": "Ask the user which routine to run today."})
    row = dal.state_get(cid, "partner_routine", routine_name)
    if not row:
        return json.dumps({"error": f"No routine named '{routine_name}'.",
                           "routines": [r["name"] for r in dal.state_list(cid, "partner_routine")]})
    cfg = row["config"]
    task = json.dumps({"routine": cfg, "candidates": candidate_companies or [],
                       "confirmation_phrase": confirmation_phrase})
    m = _CONFIRM_RE.search(confirmation_phrase or "")
    autopilot = (cfg.get("run_mode") == "autopilot")
    if candidate_companies and not m and not autopilot:
        dm = cfg.get("decision_makers", {})
        hints = dm.get("ai_ark_dials_hint", {}) or {}
        first = next(iter(hints.values()), {})
        est = aiark.count_people({"titles": dm.get("target_roles", []),
                                  "seniorities": first.get("seniority", []),
                                  "departments": first.get("department", [])})
        n = min(est["total"], dm.get("max_per_company", 3) * len(candidate_companies))
        return json.dumps({
            "qualified_note": "Candidates received. Free qualify + count done; the paid pull is gated.",
            "estimated_people": n,
            "gate": ("Supervised routine: show the user this sentence to type EXACTLY — "
                     f"'I confirm to export this and use {n} amount of credits' — then call "
                     "again with their typed sentence as confirmation_phrase."),
        })
    # Guard only the user-supplied candidates/phrase; the routine config is trusted vault data.
    return run_framework("partner_signals", task, cid, "qwintiq_partner_signals",
                         guard_text=json.dumps({"candidates": candidate_companies or [],
                                                "confirmation_phrase": confirmation_phrase}))


# ---------- Lemlist: the vault adds finished leads to a campaign, server-side ----------

@mcp.tool()
@_safe
def qwintiq_lemlist_campaigns() -> str:
    """List the Lemlist campaigns finished leads can be added to (name + id).

    Call this when the user wants to load people into Lemlist but hasn't named a campaign,
    or to confirm the exact campaign name before an upload. Returns the campaigns on the
    account; present the names and ask which one.
    """
    campaigns = lemlist.list_campaigns()
    return json.dumps({
        "campaigns": campaigns,
        "note": ("Ask the user which campaign these people should go into, then call "
                 "qwintiq_lemlist_upload with that campaign name (or id) and the leads."),
    })


@mcp.tool()
@_safe
def qwintiq_lemlist_upload(campaign: str, leads: list[dict]) -> str:
    """Add finished leads straight into a Lemlist campaign, on the user's behalf.

    Pass the campaign (its name or its cam_… id) and the leads. Each lead needs an "email";
    optional fields are firstName, lastName, companyName, jobTitle, phone, linkedinUrl,
    companyDomain, icebreaker, plus any custom variables your campaign uses. The vault does
    the upload itself — the Lemlist key and mechanics never leave the vault — and returns a
    plain-English receipt. Re-running is safe: leads already in the campaign are de-duplicated.
    """
    target = lemlist.resolve_campaign(campaign)
    if not target:
        available = lemlist.list_campaigns()
        return json.dumps({
            "error": f"No Lemlist campaign matches '{campaign}'.",
            "campaigns": available,
            "note": "Show the user these campaign names and ask them to pick the exact one.",
        })
    result = lemlist.upload_leads(target["id"], leads or [])
    bits = [f"Added {result['added']} lead(s) to the Lemlist campaign “{target['name']}”."]
    if result["skipped_no_email"]:
        bits.append(f"{result['skipped_no_email']} row(s) had no valid email and were skipped.")
    if result["failed"]:
        bits.append(f"{result['failed']} lead(s) couldn't be added and can be retried.")
    if result["mock"]:
        bits.append("(Demo mode — no Lemlist key is set, so nothing was really uploaded.)")
    return json.dumps({
        "campaign": target,
        "added": result["added"],
        "skipped_no_email": result["skipped_no_email"],
        "failed": result["failed"],
        "receipt": " ".join(bits),
        "note": "Show the user the 'receipt' line as a plain record of what was loaded.",
    })


# ---------- Enrichment: add contact details to a known list, server-side ----------

@mcp.tool()
@_safe
def qwintiq_enrich(people: list[dict], include_phone: bool = True) -> str:
    """Find the missing contact details (work email, and mobile when include_phone) for people
    you already have.

    Use this when the user has a list of people — names, companies, or LinkedIn URLs — but is
    missing their emails/phones, and wants them filled in before outreach. Identify each person
    by a "linkedin" URL, or a "full_name" plus a "company_domain" (or "company_name"). Any other
    fields you pass are kept as-is on the row. The vault does the lookup itself — the data key and
    provider never leave the vault — and returns the same list with "email"/"phone" added and an
    "enriched" flag per row. People it can't resolve come back with those blank, not as an error.
    """
    rows = aiark.enrich(people or [], want_phone=include_phone)
    found = sum(1 for r in rows if r.get("enriched"))
    mock = bool(rows and rows[0].get("mock"))
    receipt = f"Found contact details for {found} of {len(rows)} people."
    if mock:
        receipt += " (Demo mode — no data key is set, so these are placeholder details.)"
    return json.dumps({
        "people": rows,
        "found": found,
        "total": len(rows),
        "receipt": receipt,
        "note": "Show the 'receipt' line, then the enriched people. Blank email/phone = not found.",
    })


# ---------- Vault-side state: setups + routines live here, never on a consultant's disk ----

@mcp.tool()
@_safe
def qwintiq_setup_list() -> str:
    """List the user's saved icebreaker setups (plus the shared QwintiQ default)."""
    return json.dumps([{"name": s["name"], "shared": s["consultant_id"] is None,
                        "updated_at": s["updated_at"]}
                       for s in dal.state_list(_cid(), "icebreaker_setup")])


@mcp.tool()
@_safe
def qwintiq_setup_save(name: str, config: dict) -> str:
    """Save/update an icebreaker setup (angles, order, recency, off-limits, backups) in the
    vault under the user's account. Edit in plain words with the user, then save here —
    never write a local file."""
    dal.state_save(_cid(), "icebreaker_setup", name, config)
    return f"Setup '{name}' saved to the vault."


@mcp.tool()
@_safe
def qwintiq_routine_list() -> str:
    """List the user's saved partner-signal routines (plus shared QwintiQ defaults)."""
    return json.dumps([{"name": s["name"], "shared": s["consultant_id"] is None,
                        "updated_at": s["updated_at"]}
                       for s in dal.state_list(_cid(), "partner_routine")])


@mcp.tool()
@_safe
def qwintiq_routine_save(name: str, config: dict) -> str:
    """Save/update a partner-signal routine (signals, company rule, roles, icebreaker style,
    campaign, run mode + credit cap) in the vault under the user's account. An autopilot
    routine MUST carry a daily_credit_cap — refused otherwise."""
    # Canonicalise the run-mode key: clients plausibly send "mode" for "run_mode".
    mode = str(config.get("run_mode") or config.get("mode") or "").strip().lower()
    if mode:
        config = {**config, "run_mode": mode}
        config.pop("mode", None)
    if mode == "autopilot" and not config.get("daily_credit_cap"):
        return "REFUSED: an autopilot routine needs a daily_credit_cap. Ask the user for one."
    dal.state_save(_cid(), "partner_routine", name, config)
    return f"Routine '{name}' saved to the vault."
