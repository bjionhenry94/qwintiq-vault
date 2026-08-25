"""AI-ARK proxy — QwintiQ's data key lives HERE (env), never on a consultant's machine.

Real mode: AI_ARK_API_KEY set -> calls api.ai-ark.com developer-portal endpoints.
Mock mode: VAULT_AIARK=mock (or no key) -> deterministic counts/rows for dev + tests.

Counting uses size=1 and reads totalElements (≈1 credit). Exports are capped hard at the
number the consultant confirmed — the server never pulls a row past the cap.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time

BASE = "https://api.ai-ark.com/api/developer-portal/v1"
# Enrichment (finding a known person's email/phone) is served by AI-ARK's MCP tools, not the
# developer-portal search REST. Same key, different transport (JSON-RPC over HTTP, token query).
MCP_BASE = "https://api.ai-ark.com/v1/mcp"

_log = logging.getLogger("qwintiq.vault")


class DataUnavailable(Exception):
    """The proxied data lookup failed. Deliberately carries NO upstream detail — no provider
    name, URL, status code, or key hint — so nothing about the vault's internals can reach the
    consultant through an error. The real cause is logged server-side for the admin only."""


def _key() -> str | None:
    """The AI-ARK key: admin-set (in /admin/settings, encrypted in the DB) takes precedence over
    the host env var."""
    from db import dal

    return dal.get_secret("AI_ARK_API_KEY")


def _mock() -> bool:
    return os.environ.get("VAULT_AIARK", "").lower() == "mock" or not _key()


def _post(path: str, body: dict) -> dict:
    import httpx

    try:
        r = httpx.post(
            f"{BASE}{path}",
            json=body,
            headers={"x-api-key": _key(), "content-type": "application/json"},
            timeout=60,
        )
        r.raise_for_status()
        return r.json()
    except Exception as e:
        # Log the true error server-side (admin can see it in the service logs); raise a bare,
        # detail-free exception so the provider/URL never travels back to the consultant.
        logging.getLogger("qwintiq.vault").warning("data provider call failed: %s", e)
        raise DataUnavailable from None


def _mock_total(seed: str, lo: int, hi: int) -> int:
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return lo + h % (hi - lo)


def count_companies(filters: dict) -> dict:
    if _mock():
        total = _mock_total("c" + json.dumps(filters, sort_keys=True), 800, 9000)
        sample = [{"company_name": "Sample Co (mock)", "website": "sample.example",
                   "country": filters.get("country", ""), "employee_count": 24,
                   "industry": filters.get("industry", "")}]
        return {"total": total, "sample": sample, "mock": True}
    body = {**_company_body(filters), "page": 0, "size": 1}
    data = _post("/companies", body)
    return {"total": data.get("totalElements", 0),
            "sample": data.get("content", [])[:1], "mock": False}


def count_people(filters: dict) -> dict:
    if _mock():
        total = _mock_total("p" + json.dumps(filters, sort_keys=True), 300, 5000)
        return {"total": total, "sample": [{"full_name": "Sam Sample (mock)",
                "title": (filters.get("titles") or ["Founder"])[0]}], "mock": True}
    body = {**_company_body(filters), **_people_body(filters), "page": 0, "size": 1}
    data = _post("/people", body)
    return {"total": data.get("totalElements", 0),
            "sample": data.get("content", [])[:1], "mock": False}


def export_rows(filters: dict, kind: str, cap: int) -> list[dict]:
    """Pull at most `cap` rows. The cap is the confirmed spend — never exceeded."""
    if _mock():
        n = min(cap, 25)
        if kind == "companies":
            return [{"company_name": f"Mock Co {i+1}", "website": f"mock{i+1}.example",
                     "country": filters.get("country", ""), "employee_count": 10 + i,
                     "industry": filters.get("industry", ""), "linkedin": ""} for i in range(n)]
        return [{"full_name": f"Mock Person {i+1}", "title": (filters.get("titles") or ["Founder"])[0],
                 "company_name": f"Mock Co {i+1}", "website": f"mock{i+1}.example",
                 "country": filters.get("country", ""), "linkedin": ""} for i in range(n)]
    rows: list[dict] = []
    page = 0
    path = "/companies" if kind == "companies" else "/people"
    base = _company_body(filters) if kind == "companies" else {**_company_body(filters), **_people_body(filters)}
    while len(rows) < cap:
        size = min(100, cap - len(rows))
        data = _post(path, {**base, "page": page, "size": size})
        got = data.get("content", [])
        rows.extend(got)
        if len(got) < size:
            break
        page += 1
    return rows[:cap]


def _company_body(f: dict) -> dict:
    body: dict = {}
    if f.get("industry"):
        body["industries"] = [f["industry"].lower()]
    if f.get("country"):
        body["locations"] = [f["country"]]
    if f.get("size_min") is not None or f.get("size_max") is not None:
        body["employeeSize"] = {"min": f.get("size_min") or 1, "max": f.get("size_max") or 100000}
    if f.get("keywords"):
        body["keywords"] = f["keywords"]
    if f.get("exclude_keywords"):
        body["excludeKeywords"] = f["exclude_keywords"]
    return body


def _people_body(f: dict) -> dict:
    body: dict = {}
    if f.get("seniorities"):
        body["seniorities"] = f["seniorities"]
    if f.get("departments"):
        body["departments"] = f["departments"]
    if f.get("titles"):
        body["titles"] = f["titles"]
    if f.get("exclude_titles"):
        body["excludeTitles"] = f["exclude_titles"]
    return body


# ---------- Enrichment: add email + mobile to a known person ----------
# NOTE: the real (live) path below is written to AI-ARK's verified MCP tool contract
# (email_finder / mobile_phone_finder) but is intentionally fail-safe: any transport or
# parsing problem degrades to "no contact found" for that person and is logged server-side —
# it never raises to the consultant and never crashes the vault. Mock mode is exercised by the
# tests; the first live run is the true end-to-end check.

def _mcp_call(tool: str, arguments: dict) -> dict:
    """Call one AI-ARK MCP tool over JSON-RPC. Returns the parsed tool payload as a dict, or {}
    on any failure (logged, never raised)."""
    import httpx

    try:
        r = httpx.post(
            MCP_BASE,
            params={"token": _key() or ""},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": tool, "arguments": arguments}},
            headers={"content-type": "application/json",
                     "accept": "application/json, text/event-stream"},
            timeout=60,
        )
        r.raise_for_status()
        return _mcp_payload(r)
    except Exception as e:
        _log.warning("ai-ark enrich call failed (%s): %s", tool, e)
        return {}


def _mcp_payload(r) -> dict:
    """Unwrap a JSON-RPC / MCP tools-call response into the tool's own payload dict, tolerating
    either a plain JSON body or an SSE (text/event-stream) body."""
    env = None
    if "text/event-stream" in r.headers.get("content-type", ""):
        for line in r.text.splitlines():
            if line.startswith("data:"):
                try:
                    env = json.loads(line[5:].strip())
                except Exception:
                    env = None
    else:
        try:
            env = r.json()
        except Exception:
            env = None
    result = (env or {}).get("result", env) or {}
    if isinstance(result, dict) and isinstance(result.get("structuredContent"), dict):
        return result["structuredContent"]
    content = result.get("content", []) if isinstance(result, dict) else []
    for item in content if isinstance(content, list) else []:
        if isinstance(item, dict) and item.get("type") == "text":
            try:
                return json.loads(item.get("text", ""))
            except Exception:
                return {"text": item.get("text", "")}
    return result if isinstance(result, dict) else {}


def _rows(payload: dict) -> list:
    """Best-effort: pull a list of result records out of whatever shape a finder returns."""
    if not isinstance(payload, dict):
        return []
    for key in ("results", "content", "data", "people", "items"):
        v = payload.get(key)
        if isinstance(v, list):
            return v
        if isinstance(v, dict) and isinstance(v.get("content"), list):
            return v["content"]
    return [payload]  # a single flat record


def _first(payload: dict, *fields: str) -> str:
    for row in _rows(payload):
        if not isinstance(row, dict):
            continue
        for fld in fields:
            val = row.get(fld)
            if isinstance(val, list) and val:
                val = val[0].get(fld[:-1]) if isinstance(val[0], dict) else val[0]
            if val:
                return str(val)
    for fld in fields:  # also try the top level
        if payload.get(fld):
            return str(payload[fld])
    return ""


def _find_email(linkedin: str, name: str, domain: str, company: str) -> str:
    args: dict = {"size": 1}
    if linkedin:
        args["linkedin"] = linkedin
    if name:
        args["fullName"] = name
    if domain:
        args["companyDomain"] = domain
    elif company:
        args["companyName"] = company
    started = _mcp_call("email_finder", args)
    inline = _first(started, "email", "emails")
    if inline:
        return inline
    track = started.get("trackId") or started.get("trackID") or started.get("track_id")
    if not track:
        return ""
    for _ in range(12):  # ~60s ceiling; email_finder is async
        res = _mcp_call("email_finder_results", {"trackId": track, "size": 1})
        email = _first(res, "email", "emails")
        if email:
            return email
        if str(res.get("state", "")).upper() == "DONE":
            return ""
        time.sleep(5)
    return ""


def _find_phone(linkedin: str, name: str, domain: str) -> str:
    body: dict = {"type": "MOBILE"}
    if linkedin:
        body["linkedin"] = linkedin
    elif name and domain:
        body.update({"name": name, "domain": domain})
    else:
        return ""
    res = _mcp_call("mobile_phone_finder", {"requestBody": json.dumps(body)})
    return _first(res, "phone", "phoneNumber", "mobile", "number")


def _person_id(p: dict) -> tuple[str, str, str, str]:
    linkedin = str(p.get("linkedin") or p.get("linkedin_url") or "").strip()
    name = str(p.get("full_name") or p.get("name") or "").strip()
    domain = str(p.get("company_domain") or p.get("domain") or p.get("website") or "").strip()
    company = str(p.get("company_name") or p.get("company") or "").strip()
    return linkedin, name, domain, company


def enrich(people: list[dict], want_phone: bool = True) -> list[dict]:
    """Add 'email' (and 'phone' when want_phone) to each person. Input rows are identified by a
    LinkedIn URL, or a name plus company domain/name. Returns a NEW list; originals untouched.
    Rows we can't resolve come back with empty email/phone and enriched=False — never an error."""
    out: list[dict] = []
    if _mock():
        for i, p in enumerate(people or []):
            _, name, domain, _ = _person_id(p)
            handle = (name or f"person{i+1}").lower().replace(" ", ".")
            out.append({**p, "email": f"{handle}@{domain or 'example.com'}",
                        "phone": (f"+1415555{1000 + i:04d}" if want_phone else ""),
                        "enriched": True, "mock": True})
        return out
    for p in people or []:
        linkedin, name, domain, company = _person_id(p)
        email = _find_email(linkedin, name, domain, company) if (linkedin or name) else ""
        phone = _find_phone(linkedin, name, domain) if want_phone else ""
        out.append({**p, "email": email, "phone": phone,
                    "enriched": bool(email or phone), "mock": False})
    return out
