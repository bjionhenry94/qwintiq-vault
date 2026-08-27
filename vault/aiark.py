"""AI-ARK proxy — QwintiQ's data key lives HERE (env/DB), never on a consultant's machine.

Real mode: AI_ARK_API_KEY set -> calls AI-ARK's hosted MCP (api.ai-ark.com/v1/mcp).
Mock mode: VAULT_AIARK=mock (or no key) -> deterministic counts/rows for dev + tests.

ONE transport for everything (search, count, export, enrich): AI-ARK's hosted MCP with FLAT
parameters. We deliberately do NOT hand-roll the developer-portal REST search endpoints — their
request body is a nested `account`/`contact` schema that drifts, and a stale shape silently fails
(that is exactly what broke list-building against AI-ARK's 2026 API update). The hosted MCP takes
flat params, resolves cleanly, and is maintained by AI-ARK. Industry/location are strict catalogs,
so we resolve them to exact enum values (industry_search / location_search) before searching.

Counting uses size=1 and reads totalElements (~1 credit). Exports are capped hard at the number
the consultant confirmed — the server never pulls a row past the cap.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import time

# AI-ARK's hosted MCP. Same key, JSON-RPC over HTTP with the token on the query string.
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


def _mock_total(seed: str, lo: int, hi: int) -> int:
    h = int(hashlib.sha256(seed.encode()).hexdigest(), 16)
    return lo + h % (hi - lo)


# ---------- Hosted-MCP transport (shared by search + enrich) ----------

def _mcp_call(tool: str, arguments: dict, strict: bool = False) -> dict:
    """Call one AI-ARK MCP tool over JSON-RPC. Returns the parsed tool payload as a dict.

    strict=False (enrichment): any transport/parse problem is logged and swallowed -> {} so a
    single unresolvable person degrades to "not found", never an error to the consultant.
    strict=True (search/count/export/label-resolve): a transport failure raises DataUnavailable so
    the curtain guard shows the honest "data unavailable" message instead of a bogus empty result.
    """
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
        _log.warning("ai-ark call failed (%s): %s", tool, e)
        if strict:
            raise DataUnavailable from None
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


# ---------- Filter-label resolution (strict catalogs) ----------

def _resolve_industry(text: str) -> str:
    """Resolve a plain industry word to AI-ARK's exact catalog label(s). An exact (case-insensitive)
    match wins and stays tight; otherwise return the matched labels (CSV) so an intent like
    'recruitment' still covers its real labels. Empty in -> empty out (no industry filter)."""
    text = (text or "").strip()
    if not text:
        return ""
    payload = _mcp_call("industry_search", {"query": text}, strict=True)
    opts = [o for o in (payload.get("industries") or []) if isinstance(o, str)]
    for o in opts:
        if o.lower() == text.lower():
            return o
    if opts:
        return ",".join(opts[:12])
    return text.lower()


def _resolve_location(text: str) -> str:
    """Resolve a location to an exact catalog leaf name. location_search does substring matching,
    so we take the exact (case-insensitive) match if present, else pass the input through (a valid
    leaf name still resolves). Empty in -> empty out."""
    text = (text or "").strip()
    if not text:
        return ""
    payload = _mcp_call("location_search", {"query": text}, strict=True)
    for o in (payload.get("locations") or []):
        if isinstance(o, str) and o.lower() == text.lower():
            return o
    return text


def _resolve(f: dict) -> tuple[str, str]:
    return _resolve_industry(f.get("industry", "")), _resolve_location(f.get("country", ""))


def _company_args(f: dict, ind: str, loc: str) -> dict:
    a: dict = {}
    if ind:
        a["industry"] = ind
    if loc:
        a["location"] = loc
    if f.get("size_min") is not None:
        a["minEmployees"] = int(f["size_min"])
    if f.get("size_max") is not None:
        a["maxEmployees"] = int(f["size_max"])
    if f.get("keywords"):
        a["keyword"] = ",".join(f["keywords"])
        a["keywordMode"] = "SMART"
    return a


def _people_args(f: dict, ind: str, loc: str) -> dict:
    a: dict = {}
    if ind:
        a["companyIndustry"] = ind
    if loc:
        a["companyLocation"] = loc
    if f.get("size_min") is not None:
        a["minEmployees"] = int(f["size_min"])
    if f.get("size_max") is not None:
        a["maxEmployees"] = int(f["size_max"])
    if f.get("seniorities"):
        a["seniority"] = ",".join(f["seniorities"])
    if f.get("departments"):
        a["department"] = ",".join(f["departments"])
    if f.get("titles"):
        a["title"] = ",".join(f["titles"])
    if f.get("exclude_titles"):
        a["excludeTitle"] = ",".join(f["exclude_titles"])
    return a


def _search(tool: str, args: dict) -> dict:
    """One strict search call. Raises DataUnavailable if the response is not a real search result
    (missing totalElements) — a genuine zero-match search still carries totalElements:0."""
    payload = _mcp_call(tool, args, strict=True)
    if not isinstance(payload, dict) or "totalElements" not in payload:
        keys = list(payload)[:10] if isinstance(payload, dict) else type(payload).__name__
        _log.warning("ai-ark %s returned no totalElements; keys=%s", tool, keys)
        raise DataUnavailable
    return payload


# ---------- Flatten AI-ARK's nested rows to the vault's CSV columns ----------

def _flatten_company(c: dict) -> dict:
    summ = c.get("summary") or {}
    link = c.get("link") or {}
    hq = (c.get("location") or {}).get("headquarter") or {}
    staff = summ.get("staff") or {}
    return {
        "company_name": summ.get("name") or c.get("name") or "",
        "website": link.get("domain_ltd") or link.get("domain") or link.get("website") or "",
        "country": hq.get("country") or "",
        "employee_count": (staff.get("total") if isinstance(staff, dict) else "") or "",
        "industry": summ.get("industry") or "",
        "linkedin": link.get("linkedin") or "",
    }


def _flatten_person(p: dict) -> dict:
    prof = p.get("profile") or {}
    link = p.get("link") or {}
    comp = p.get("company") or {}
    csum = comp.get("summary") or {}
    clink = comp.get("link") or {}
    loc = p.get("location") or {}
    return {
        "full_name": prof.get("full_name") or p.get("full_name") or "",
        "title": prof.get("title") or "",
        "company_name": csum.get("name") or comp.get("name") or "",
        "website": clink.get("domain_ltd") or clink.get("domain") or clink.get("website") or "",
        "country": loc.get("country") or "",
        "linkedin": link.get("linkedin") or "",
    }


# ---------- Public API: count + export ----------

def count_companies(filters: dict) -> dict:
    if _mock():
        total = _mock_total("c" + json.dumps(filters, sort_keys=True), 800, 9000)
        sample = [{"company_name": "Sample Co (mock)", "website": "sample.example",
                   "country": filters.get("country", ""), "employee_count": 24,
                   "industry": filters.get("industry", "")}]
        return {"total": total, "sample": sample, "mock": True,
                "resolved_industry": filters.get("industry", ""),
                "resolved_location": filters.get("country", "")}
    ind, loc = _resolve(filters)
    payload = _search("company_search", {**_company_args(filters, ind, loc), "page": 0, "size": 1})
    return {"total": payload.get("totalElements", 0),
            "sample": [_flatten_company(c) for c in (payload.get("content") or [])[:1]],
            "mock": False, "resolved_industry": ind, "resolved_location": loc}


def count_people(filters: dict) -> dict:
    if _mock():
        total = _mock_total("p" + json.dumps(filters, sort_keys=True), 300, 5000)
        return {"total": total, "sample": [{"full_name": "Sam Sample (mock)",
                "title": (filters.get("titles") or ["Founder"])[0]}], "mock": True,
                "resolved_industry": filters.get("industry", ""),
                "resolved_location": filters.get("country", "")}
    ind, loc = _resolve(filters)
    payload = _search("people_search", {**_people_args(filters, ind, loc), "page": 0, "size": 1})
    return {"total": payload.get("totalElements", 0),
            "sample": [_flatten_person(p) for p in (payload.get("content") or [])[:1]],
            "mock": False, "resolved_industry": ind, "resolved_location": loc}


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
    ind, loc = _resolve(filters)
    if kind == "companies":
        tool, args, flat = "company_search", _company_args(filters, ind, loc), _flatten_company
    else:
        tool, args, flat = "people_search", _people_args(filters, ind, loc), _flatten_person
    rows: list[dict] = []
    page = 0
    while len(rows) < cap:
        size = min(100, cap - len(rows))  # AI-ARK search caps page size at 100
        payload = _search(tool, {**args, "page": page, "size": size})
        got = payload.get("content") or []
        rows.extend(flat(r) for r in got)
        if len(got) < size:
            break
        page += 1
    return rows[:cap]


# ---------- Enrichment: add email + mobile to a known person ----------
# The enrich path talks to the SAME hosted MCP as search (email_finder / mobile_phone_finder), but
# is intentionally fail-safe: any transport or parsing problem degrades to "no contact found" for
# that person and is logged server-side — it never raises to the consultant and never crashes the
# vault. Mock mode is exercised by the tests.

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
