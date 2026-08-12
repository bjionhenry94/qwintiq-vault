"""The vault's MCP tools — the ONLY surface a consultant's Claude sees.

Every tool takes a brief and returns finished work. Framework text never appears in a
response (engine.run_framework guards + filters). The AI-ARK credit gate is enforced
HERE, server-side: no export happens unless the typed confirmation phrase carries the
exact row count being pulled.
"""
from __future__ import annotations

import csv
import io
import json
import re

from mcp.server.fastmcp import FastMCP

from auth.context import current_consultant
from db import dal
from vault import aiark
from vault.engine import REFUSAL, meta_guard, run_framework

mcp = FastMCP("qwintiq-vault")

_CONFIRM_RE = re.compile(r"i\s+confirm\s+to\s+export\s+this\s+and\s+use\s+([\d,]+)\s+amount\s+of\s+credits",
                         re.IGNORECASE)


def _cid() -> str | None:
    c = current_consultant()
    return c["id"] if c else None


@mcp.tool()
def ping() -> str:
    """Health check. Confirms the Qwintiq vault is reachable and responding."""
    return "qwintiq-vault: online"


@mcp.tool()
def qwintiq_copywriter(problem: str, outcome: str, risk_reversal: str, service: str,
                       proof: str = "", icebreaker_note: str = "") -> str:
    """Write Qwintiq outreach copy — the finished email + LinkedIn sequences, Lemlist-ready.

    Collect the brief from the user first (the problem the prospect has, the outcome on
    offer, the risk reversal / guarantee, what the service is, optional proof), then call
    this once. It returns the two finished sequences; you present them as-is.
    """
    task = json.dumps({"problem": problem, "outcome": outcome, "risk_reversal": risk_reversal,
                       "service": service, "proof": proof, "icebreaker_note": icebreaker_note})
    return run_framework("copywriter", task, _cid(), "qwintiq_copywriter")


@mcp.tool()
def qwintiq_icebreaker(prospects: list[dict], setup_name: str = "Qwintiq partnership icebreakers") -> str:
    """Write personalised opening lines for partnership outreach prospects.

    For each prospect pass: name, company, and page_text (paste the visible text of their
    website / LinkedIn page — gather it with your own browsing, the vault does the rest).
    Returns one finished icebreaker per prospect; lines with no verified hook come back
    flagged honestly. Nothing is ever invented.
    """
    setup = dal.state_get(_cid(), "icebreaker_setup", setup_name)
    task = json.dumps({"prospects": prospects, "setup": setup["config"] if setup else None})
    return run_framework("icebreaker", task, _cid(), "qwintiq_icebreaker")


@mcp.tool()
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
def qwintiq_partner_signals(routine_name: str = "", candidate_companies: list[dict] | None = None,
                            confirmation_phrase: str = "") -> str:
    """Run Qwintiq's daily partner/PR signal routine.

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
    return run_framework("partner_signals", task, cid, "qwintiq_partner_signals")


# ---------- Vault-side state: setups + routines live here, never on a consultant's disk ----

@mcp.tool()
def qwintiq_setup_list() -> str:
    """List the user's saved icebreaker setups (plus the shared Qwintiq default)."""
    return json.dumps([{"name": s["name"], "shared": s["consultant_id"] is None,
                        "updated_at": s["updated_at"]}
                       for s in dal.state_list(_cid(), "icebreaker_setup")])


@mcp.tool()
def qwintiq_setup_save(name: str, config: dict) -> str:
    """Save/update an icebreaker setup (angles, order, recency, off-limits, backups) in the
    vault under the user's account. Edit in plain words with the user, then save here —
    never write a local file."""
    dal.state_save(_cid(), "icebreaker_setup", name, config)
    return f"Setup '{name}' saved to the vault."


@mcp.tool()
def qwintiq_routine_list() -> str:
    """List the user's saved partner-signal routines (plus shared Qwintiq defaults)."""
    return json.dumps([{"name": s["name"], "shared": s["consultant_id"] is None,
                        "updated_at": s["updated_at"]}
                       for s in dal.state_list(_cid(), "partner_routine")])


@mcp.tool()
def qwintiq_routine_save(name: str, config: dict) -> str:
    """Save/update a partner-signal routine (signals, company rule, roles, icebreaker style,
    campaign, run mode + credit cap) in the vault under the user's account. An autopilot
    routine MUST carry a daily_credit_cap — refused otherwise."""
    if config.get("run_mode") == "autopilot" and not config.get("daily_credit_cap"):
        return "REFUSED: an autopilot routine needs a daily_credit_cap. Ask the user for one."
    dal.state_save(_cid(), "partner_routine", name, config)
    return f"Routine '{name}' saved to the vault."
