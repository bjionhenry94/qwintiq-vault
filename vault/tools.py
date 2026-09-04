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
import secrets

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


def _safe_async(fn):
    """Async twin of _safe for tools that must await (e.g. enrichment polls AI-ARK without
    blocking the event loop — mcp 1.x runs sync tools on the loop, so a blocking tool hangs the
    whole vault). The wrapper stays a coroutine function so FastMCP awaits it."""
    @functools.wraps(fn)
    async def wrapper(*args, **kwargs):
        try:
            return await fn(*args, **kwargs)
        except DataUnavailable:
            _log.warning("data lookup failed in %s", fn.__name__)
            return _DATA_ERROR
        except Exception:
            _log.exception("tool %s failed", fn.__name__)
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
        "resolved": {"industry": companies.get("resolved_industry"),
                     "location": companies.get("resolved_location")},
        "note": f"Counts only — nothing exported, roughly 2 credits used{mock_note}.",
        "to_export": ("Ask the user to choose a scope (all, or a capped batch), then have "
                      "them type EXACTLY: 'I confirm to export this and use X amount of "
                      "credits' where X is the row count. Pass their typed sentence to "
                      "qwintiq_list_export. Never type it for them."),
        "filters_used": filters,
    })


# The ONLY filter keys the market export honours (see aiark._company_args / _people_args). Anything
# else used to be dropped silently — so a curated shortlist passed as `company_domains` pulled a
# generic worldwide market instead and charged 50 credits for unusable rows (the live bug). Now any
# key outside this set refuses BEFORE the credit gate and BEFORE any AI-Ark call.
_EXPORT_FILTER_KEYS = {"industry", "country", "size_min", "size_max", "keywords", "exclude_keywords",
                       "seniorities", "departments", "titles", "exclude_titles"}
# Keys that mean "these specific companies/people" — the one thing a market export can never do.
_TARGETING_KEYS = {"company_domains", "domains", "websites", "companies", "company_names",
                   "company_name", "company_domain", "company", "linkedin", "linkedin_urls",
                   "people", "names", "full_names"}


def _refuse_unsupported_filters(filters: dict, kind: str) -> str:
    """Return a refusal string if `filters` asks for something the market export can't honour,
    else ''. Refusing here is what stops a mis-targeted request from spending anything."""
    keys = set((filters or {}).keys())
    targeting = sorted(keys & _TARGETING_KEYS)
    unknown = sorted(keys - _EXPORT_FILTER_KEYS - _TARGETING_KEYS)
    if not targeting and not unknown:
        return ""
    what = "decision-makers" if kind != "companies" else "companies"
    parts = ["EXPORT REFUSED (nothing was pulled or charged)."]
    if targeting:
        parts.append(
            f"This tool exports a MARKET by brief (industry, country, size, roles); it cannot target "
            f"specific companies, so it does not accept {', '.join(targeting)}. To get the {what} AT a "
            f"specific list of companies, call qwintiq_company_people with those companies (or confirm "
            f"a partner-signal routine, which pulls per company) — then qwintiq_enrich for their "
            f"emails by LinkedIn URL.")
    if unknown:
        parts.append(f"Unrecognised filter key(s): {', '.join(unknown)}. Supported keys are: "
                     f"{', '.join(sorted(_EXPORT_FILTER_KEYS))}.")
    parts.append("Tell the user plainly which tool fits, then continue with that one.")
    return " ".join(parts)


@mcp.tool()
@_safe
def qwintiq_list_export(kind: str, filters: dict, max_rows: int, confirmation_phrase: str) -> str:
    """Export a MARKET by brief (kind: 'companies' or 'decision_makers') as CSV text.

    This pulls a market described by filters — industry, country, size_min/size_max, keywords,
    exclude_keywords, and for decision_makers also seniorities, departments, titles,
    exclude_titles. It CANNOT target specific companies: it does not accept company_domains,
    company names, websites or LinkedIn URLs and will refuse — before any spend — if you pass
    them. To get the decision-makers AT a specific list of companies use qwintiq_company_people
    (or a partner-signal routine's confirm step), then qwintiq_enrich for their emails.

    HARD GATE: this only runs if confirmation_phrase is the sentence the USER typed —
    'I confirm to export this and use X amount of credits' — and X equals max_rows.
    The vault validates it server-side and refuses otherwise. One confirmation = one export.
    """
    refusal = _refuse_unsupported_filters(filters, kind)
    if refusal:
        return refusal
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


# A supervised gate is a TWO-call handshake, so the count + the exact shortlist it was quoted for
# MUST survive between the calls — otherwise the client has to re-send the candidate list perfectly
# (and if it doesn't, the run fires on nothing) and the count is recomputed and can drift. The
# snapshot is held server-side under a one-time token in the DB (dal.gate_put / gate_take), NOT in
# process memory. An in-memory dict silently "expired" on the very next call once deployed, because
# the count call and the confirm call are separate HTTP requests with no guarantee of hitting the
# same live process; the DB row is durable across workers, instances and restarts, and single-use.
# A supervised confirm happens in minutes, not hours. Env-overridable so a test can prove real
# time-based expiry without waiting 30 minutes.
_GATE_TTL = int(os.environ.get("SIGNAL_GATE_TTL_S", str(30 * 60)))


def _run_partner_pull(cfg: dict, candidates: list[dict], confirmation_phrase: str, cid,
                      cap_n: int | None = None) -> str:
    """The paid Phase-D pull. Actually fetch the decision-makers at each candidate company from
    AI-Ark (scoped by the company's domain, filtered to the routine's roles, capped per company),
    tie each person to their company, de-duplicate, and return the finished people plus a plain
    receipt. Never exceeds the confirmed number (Supervised) or the daily cap (Autopilot).

    This is real data work, NOT an LLM prompt. The model can't reach AI-Ark, so running the
    framework through it returned conversational filler and pulled nobody — the live bug this
    replaces (it passed only because the mock LLM faked a 'RUN REPORT')."""
    dm = cfg.get("decision_makers", {}) or {}
    max_per = int(dm.get("max_per_company", 3) or 3)
    hints = dm.get("ai_ark_dials_hint", {}) or {}
    role_sets = [{"seniorities": g.get("seniority", []) or [],
                  "departments": g.get("department", []) or [],
                  "titles": g.get("title", []) or []}
                 for g in hints.values() if isinstance(g, dict)]
    if not role_sets:
        role_sets = [{"seniorities": [], "departments": [], "titles": dm.get("target_roles", []) or []}]
    candidates = candidates or []
    total_cap = int(cap_n) if cap_n else max_per * max(1, len(candidates))
    people: list[dict] = []
    seen: set = set()
    errors = 0
    for co in candidates:
        if len(people) >= total_cap:
            break
        try:
            rows = aiark.pull_decision_makers(co, role_sets, min(max_per, total_cap - len(people)))
        except DataUnavailable:
            errors += 1  # one company's lookup blipped; keep going, don't fail the whole batch
            continue
        for r in rows:
            key = (r.get("linkedin") or "", (r.get("full_name") or "").strip().lower())
            if key == ("", ""):
                key = (r.get("full_name", ""), r.get("company_name", ""))
            if key in seen:
                continue
            seen.add(key)
            people.append(r)
            if len(people) >= total_cap:
                break
    if not people and errors:
        raise DataUnavailable  # every company failed — the honest data-outage message, never "0 found"
    cols = ["full_name", "title", "company_name", "website", "country", "linkedin"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(people)
    campaign = (cfg.get("lemlist", {}) or {}).get("campaign_name")
    receipt = (f"Pulled {len(people)} decision-maker(s) across "
               f"{len(candidates)} compan{'y' if len(candidates) == 1 else 'ies'}, "
               f"about {len(people)} credits.")
    return json.dumps({
        "decision_makers": people,
        "rows": len(people),
        "csv": buf.getvalue(),
        "credits_estimate": len(people),
        "receipt": receipt,
        "next": ("Show the user the receipt line. These rows have NO emails yet. Next, get their "
                 "emails: call qwintiq_enrich with these people, passing each person's 'linkedin' "
                 "URL (the best identifier), and ask the user to type the enrich confirmation "
                 "sentence; if they only want people with an email, pass only_with_email=true. "
                 "Then write openers with qwintiq_icebreaker, and load them with "
                 "qwintiq_lemlist_upload into "
                 + (f"the '{campaign}' campaign" if campaign else "the routine's campaign")
                 + ". Never message a real prospect during a test — use a draft or paused "
                 "campaign, or a dummy lead."),
    })


@mcp.tool()
@_safe
def qwintiq_company_people(companies: list[dict], confirmation_phrase: str,
                           max_per_company: int = 2, roles: list[str] | None = None,
                           seniorities: list[str] | None = None) -> str:
    """Find the decision-makers AT a specific list of companies (the per-company pull).

    Use this when the user already has the companies — a shortlist from a signal, a target
    account list, a set of domains — and wants the people inside them. Pass each company as
    {"name": ..., "website": "acme.com"} (the website/domain is what ties the search to the right
    company). Optional: roles (titles, e.g. ["Founder", "CEO"]), seniorities (e.g. ["founder",
    "c_suite"]), and max_per_company (default 2). Returns the people with name, title, company,
    website, country and LinkedIn URL — then call qwintiq_enrich with their LinkedIn URLs to get
    emails. This is NOT qwintiq_list_export (which pulls a whole market by brief and cannot target
    named companies).

    HARD GATE: about one credit per person, so it only runs if confirmation_phrase is the
    sentence the USER typed — 'I confirm to export this and use X amount of credits' — where X
    equals max_per_company × number of companies. Quote them the sentence with the real number
    and wait for them to type it; never type it for them.
    """
    companies = [c for c in (companies or []) if isinstance(c, dict)]
    if not companies:
        return "COMPANY PULL REFUSED: no companies were given. Pass a list of {name, website}."
    untethered = [c.get("name") or "?" for c in companies
                  if not (c.get("website") or c.get("domain") or c.get("name") or c.get("company_name"))]
    if untethered:
        return ("COMPANY PULL REFUSED (nothing charged): every company needs a website/domain or at "
                "least a name so the search is tied to the right company. Missing on: "
                + ", ".join(untethered))
    max_per = max(1, int(max_per_company or 1))
    n = max_per * len(companies)
    m = _CONFIRM_RE.search(confirmation_phrase or "")
    if not m:
        return (f"COMPANY PULL REFUSED: this spends about {n} credits ({max_per} per company × "
                f"{len(companies)} companies), so it needs the user's typed go-ahead. Show them "
                f"EXACTLY: 'I confirm to export this and use {n} amount of credits' and wait for "
                f"them to type it themselves.")
    confirmed = int(m.group(1).replace(",", ""))
    if confirmed != n:
        return (f"COMPANY PULL REFUSED: the user confirmed {confirmed} but this pull is {n} "
                f"({max_per} per company × {len(companies)} companies). Re-quote {n} and re-confirm.")
    role_sets = [{"seniorities": seniorities or [], "departments": [], "titles": roles or []}]
    people: list[dict] = []
    seen: set = set()
    errors = 0
    per_company: dict[str, int] = {}
    for co in companies:
        try:
            rows = aiark.pull_decision_makers(co, role_sets, max_per)
        except DataUnavailable:
            errors += 1
            continue
        label = co.get("name") or co.get("website") or "?"
        per_company[label] = len(rows)
        for r in rows:
            key = (r.get("linkedin") or "", (r.get("full_name") or "").strip().lower())
            if key == ("", ""):
                key = (r.get("full_name", ""), r.get("company_name", ""))
            if key in seen:
                continue
            seen.add(key)
            people.append(r)
    if not people and errors:
        raise DataUnavailable  # every company failed — honest outage message, never "0 found"
    cols = ["full_name", "title", "company_name", "website", "country", "linkedin"]
    buf = io.StringIO()
    w = csv.DictWriter(buf, fieldnames=cols, extrasaction="ignore")
    w.writeheader()
    w.writerows(people)
    empty = [k for k, v in per_company.items() if v == 0]
    receipt = (f"Found {len(people)} decision-maker(s) across {len(companies)} "
               f"compan{'y' if len(companies) == 1 else 'ies'} — about {len(people)} credits "
               f"(confirmed at {confirmed}).")
    if empty:
        receipt += f" No matching people at: {', '.join(empty)}."
    if errors:
        receipt += f" {errors} compan{'y' if errors == 1 else 'ies'} couldn't be looked up and can be retried."
    return json.dumps({
        "people": people,
        "rows": len(people),
        "per_company": per_company,
        "csv": buf.getvalue(),
        "credits_estimate": len(people),
        "receipt": receipt,
        "next": ("Show the user the receipt line. To get their emails, call qwintiq_enrich with these "
                 "people — pass each person's 'linkedin' URL (best match rate) — and ask the user to "
                 "type the enrich confirmation sentence."),
    })


@mcp.tool()
@_safe
def qwintiq_partner_signals(routine_name: str = "", candidate_companies: list[dict] | None = None,
                            confirmation_phrase: str = "", gate_token: str = "") -> str:
    """Run QwintiQ's daily partner/PR signal routine.

    Call with no arguments to see the saved routines. Call with routine_name + candidate_companies
    (name + website + what happened, from your own free web search using the routine's search terms)
    and the vault qualifies them, counts the right decision-makers ONCE, and returns a gate_token
    plus the sentence to show the user.

    To confirm (Supervised): call again with the user's typed confirmation_phrase AND that
    gate_token — you do NOT re-send candidate_companies; the vault uses the exact shortlist and
    count it already quoted, so the number can't drift and nothing runs on an empty list. Autopilot
    routines carry their own daily credit cap and skip the daily stop, per the user's standing
    permission.
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
    m = _CONFIRM_RE.search(confirmation_phrase or "")
    autopilot = (cfg.get("run_mode") == "autopilot")

    # Confirmation via the token from the gate call: the server already holds the shortlist + count.
    if gate_token and m:
        snap = dal.gate_take(gate_token)  # durable + single-use; None if unknown or expired
        if not snap:
            return json.dumps({"error": "That confirmation has expired. Re-run the routine to get a "
                               "fresh count and a new confirmation sentence."})
        if int(m.group(1).replace(",", "")) != snap["n"]:
            # Wrong number: re-issue a fresh token carrying the same snapshot so they can retype
            # without losing the shortlist (the old token was already consumed above).
            retoken = secrets.token_urlsafe(16)
            dal.gate_put(retoken, snap, _GATE_TTL)
            return json.dumps({"gate": f"The number must match. Show the user EXACTLY: 'I confirm to "
                               f"export this and use {snap['n']} amount of credits'.",
                               "estimated_people": snap["n"], "gate_token": retoken})
        return _run_partner_pull(cfg, snap["candidates"], confirmation_phrase, cid, cap_n=snap["n"])

    # A phrase with no shortlist and no token = the shortlist was lost between calls. Never run on
    # an empty list; send them back to the gate.
    if m and not candidate_companies and not autopilot:
        return json.dumps({"error": "Missing the shortlist. Re-run the routine with the "
                           "candidate_companies to see the count, then confirm with the gate_token "
                           "it returns (no need to paste the list again)."})

    # New gate call: qualify + count ONCE, snapshot the shortlist + count under a one-time token.
    if candidate_companies and not m and not autopilot:
        dm = cfg.get("decision_makers", {})
        hints = dm.get("ai_ark_dials_hint", {}) or {}
        first = next(iter(hints.values()), {})
        est = aiark.count_people({"titles": dm.get("target_roles", []),
                                  "seniorities": first.get("seniority", []),
                                  "departments": first.get("department", [])})
        n = max(1, int(min(est["total"], int(dm.get("max_per_company", 3) or 3) * len(candidate_companies))))
        token = secrets.token_urlsafe(16)
        dal.gate_put(token, {"candidates": candidate_companies, "n": n}, _GATE_TTL)
        return json.dumps({
            "qualified_note": "Candidates received. Free qualify + count done; the paid pull is gated.",
            "estimated_people": n,
            "gate_token": token,
            "gate": ("Supervised routine: show the user this sentence to type EXACTLY — "
                     f"'I confirm to export this and use {n} amount of credits' — then call again "
                     f"with their typed sentence as confirmation_phrase AND gate_token='{token}'. "
                     "Do NOT re-send candidate_companies."),
        })

    # Autopilot: standing permission, pull up to the daily cap (no typed phrase needed). Legacy
    # one-shot (candidates + phrase together) still works, capped at the number they confirmed.
    if autopilot:
        cap_n = int(cfg.get("daily_credit_cap") or 0)
        if cap_n <= 0:
            return json.dumps({"error": "This routine is on autopilot but has no daily credit cap "
                               "saved, so it can't pull on its own. Set a cap first."})
        return _run_partner_pull(cfg, candidate_companies or [], confirmation_phrase, cid, cap_n=cap_n)
    cap_n = int(m.group(1).replace(",", "")) if m else None
    return _run_partner_pull(cfg, candidate_companies or [], confirmation_phrase, cid, cap_n=cap_n)


# ---------- Lemlist: the vault adds finished leads to a campaign, server-side ----------

@mcp.tool()
@_safe
def qwintiq_lemlist_campaigns() -> str:
    """List the Lemlist campaigns finished leads can be added to (name + id).

    Call this when the user wants to load people into Lemlist but hasn't named a campaign,
    or to confirm the exact campaign name before an upload. Returns the campaigns on the
    account; present the names and ask which one.
    """
    # Never invent campaigns. If no Lemlist key has been entered, say so — the live bug was the
    # model telling the user "your campaign isn't recognised" against fake cam_mock… campaigns.
    not_connected = lemlist.connection_status()
    if not_connected:
        return json.dumps({"error": "LEMLIST NOT CONNECTED", "campaigns": [], "receipt": not_connected,
                           "note": "Show the user the 'receipt' line. Do not retry until the admin has "
                                   "added the Lemlist key."})
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
    not_connected = lemlist.connection_status()
    if not_connected:
        return json.dumps({"error": "LEMLIST NOT CONNECTED", "added": 0, "receipt": not_connected,
                           "note": "Show the user the 'receipt' line. Keep the leads; nothing was "
                                   "uploaded and nothing will be until the admin adds the key."})
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
@_safe_async
async def qwintiq_enrich(people: list[dict], confirmation_phrase: str, include_phone: bool = False,
                         only_with_email: bool = False) -> str:
    """Find the missing work EMAIL (and, only when include_phone=True, the mobile) for people you
    already have. Set only_with_email=True when the user wants ONLY the people an email was found
    for (e.g. "just give me the ones with emails") — the rest are dropped and counted, not returned.

    Use this when the user has a list of people — names, companies, or LinkedIn URLs — but is
    missing their emails, and wants them filled in before outreach. Identify each person by a
    "linkedin" URL, or a "full_name" plus a "company_domain" (or "company_name"). Any other fields
    you pass are kept as-is on the row. The vault does the lookup itself — the data key and provider
    never leave the vault — and returns the same list with "email" (and "phone" when asked) added
    plus an "enriched" flag per row. People it can't resolve come back with those blank, not as an
    error. Set include_phone=True ONLY when the user explicitly wants mobile numbers (it costs more).

    HARD GATE: finding details spends about one credit per person, so this only runs if
    confirmation_phrase is the sentence the USER typed — 'I confirm to export this and use X
    amount of credits' — where X equals the number of people. Quote them the sentence with the
    real number and wait for them to type it; never type it for them.
    """
    n = len(people or [])
    m = _CONFIRM_RE.search(confirmation_phrase or "")
    if not m:
        return (f"ENRICH REFUSED: finding contact details spends credits, so it needs the user's "
                f"typed go-ahead. Show them EXACTLY: 'I confirm to export this and use {n} amount "
                f"of credits' and wait for them to type it themselves.")
    confirmed = int(m.group(1).replace(",", ""))
    if confirmed != n:
        return (f"ENRICH REFUSED: the user confirmed {confirmed} but there are {n} people to "
                f"enrich. Re-quote {n} and have them re-confirm.")
    # NO CAP: enrich EVERYONE the user passed — that is the task. enrich_async throttles to
    # AI-ARK's rate limit and waits long enough for the whole batch to resolve, returning as soon
    # as every job is done, so small batches stay fast and larger ones just take a little longer.
    rows = await aiark.enrich_async(people or [], want_phone=include_phone)
    ark_error = next((r.get("ark_error") for r in rows if r.get("ark_error")), "")
    if ark_error:
        return json.dumps({
            "error": "ENRICH COULD NOT RUN",
            "reason": ark_error,
            "found": 0, "total": n,
            "receipt": ("The data provider refused the lookup — this reads as the AI-Ark balance "
                        "being out of credits. Nothing was charged and no emails were added. Ask "
                        "your QwintiQ admin to top up the AI-Ark balance, then run this again."),
            "note": "Show the user the 'receipt' line. Do NOT retry — a top-up is needed first.",
        })
    processed = n
    found = sum(1 for r in rows if r.get("enriched"))
    found_email = sum(1 for r in rows if (r.get("email") or "").strip())
    found_phone = sum(1 for r in rows if (r.get("phone") or "").strip())
    mock = bool(rows and rows[0].get("mock"))
    out_rows = rows
    dropped = 0
    if only_with_email:
        out_rows = [r for r in rows if (r.get("email") or "").strip()]
        dropped = len(rows) - len(out_rows)
    # Billing is per attempt, not per hit, so the spend tracks the number processed, not the finds.
    receipt = (f"Found emails for {found_email} of {processed} people"
               + (f" and mobiles for {found_phone}" if include_phone else "")
               + f" (about {processed} credits).")
    if only_with_email:
        receipt += (f" Returning only the {len(out_rows)} with an email; {dropped} dropped."
                    if dropped else " Every person had an email.")
    if include_phone and found_phone == 0 and not mock:
        receipt += " No mobiles came back — phone coverage is best-effort and often thin."
    if mock:
        receipt += " (Demo mode — no data key is set, so these are placeholder details.)"
    return json.dumps({
        "people": out_rows,
        "found": found,
        "found_email": found_email,
        "found_phone": found_phone,
        "dropped_no_email": dropped,
        "processed": processed,
        "total": n,
        "receipt": receipt,
        "note": "Show the 'receipt' line, then the people. Blank email/phone = not found.",
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
