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

import asyncio
import hashlib
import json
import logging
import os
import time
from datetime import datetime, timezone

# AI-ARK's hosted MCP. Same key, JSON-RPC over HTTP with the token on the query string.
MCP_BASE = "https://api.ai-ark.com/v1/mcp"

_log = logging.getLogger("qwintiq.vault")
# httpx logs every request URL at INFO — and AI-ARK's key travels on the query string, so the
# default logger was writing the data key into the host's log stream on every call. Silence it.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)


class DataUnavailable(Exception):
    """The proxied data lookup failed. Deliberately carries NO upstream detail — no provider
    name, URL, status code, or key hint — so nothing about the vault's internals can reach the
    consultant through an error. The real cause is logged server-side for the admin only."""


class DataKeyMissing(Exception):
    """No AI-Ark key is saved in the Control Panel. Raised BEFORE any provider call so the tool
    refuses loudly instead of running in demo mode or on a host-env key (production never falls
    back to env keys — see dal.get_secret)."""


class ProviderRefused(DataUnavailable):
    """The provider answered a search with an error envelope instead of results (live: a 401 on
    any keyword search that omitted keywordSources). Not a charge and not an outage, so the
    consultant gets the real reason, not 'try again later'."""

    def __init__(self, tool: str, message: str, args: dict):
        self.tool, self.message, self.request_args = tool, message, dict(args)
        super().__init__(message)


class UnresolvedFilter(Exception):
    """A brief's industry/location is not a name AI-Ark's catalog recognises. Raised BEFORE any
    paid search, because an unknown enum passed through would be ignored upstream and pull (and
    charge for) an unfiltered worldwide market — the silent-wrong-then-charge bug class."""

    def __init__(self, kind: str, text: str, options: list[str]):
        self.kind, self.text, self.options = kind, text, options
        hint = (f" Closest catalog names: {', '.join(options)}. Ask the user to pick one and retry "
                f"with that exact name." if options else
                f" Ask the user for a different {kind} wording (a plain word like 'software' or "
                f"'health care'; a country or state name for location).")
        an = "an" if kind[0] in "aeiou" else "a"
        super().__init__(f"REFUSED (nothing was pulled or charged): '{text}' is not {an} {kind} the data "
                         f"provider recognises, so searching on it would pull the wrong market.{hint}")


def _key() -> str | None:
    """The AI-ARK key: admin-set (in /admin/settings, encrypted in the DB). In dev only, the host
    env var is an accepted fallback; in production it never is."""
    from db import dal

    return dal.get_secret("AI_ARK_API_KEY")


def _require_key() -> str:
    k = _key()
    if not k:
        raise DataKeyMissing
    return k


def _mock() -> bool:
    """Deterministic mock data ONLY when explicitly asked (VAULT_AIARK=mock) or in dev with no key.
    In production a missing key REFUSES (DataKeyMissing) — never placeholder data, never demo mode."""
    if os.environ.get("VAULT_AIARK", "").lower() == "mock":
        return True
    from db import dal

    return (not _key()) and dal.dev_fallbacks_allowed()


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

    token = _require_key()  # refuse before any call; not swallowed below
    try:
        r = httpx.post(
            MCP_BASE,
            params={"token": token},
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
    """Resolve an industry to AI-ARK's exact catalog label(s). Comma-separated inputs are resolved
    one by one. ONLY an exact (case-insensitive) catalog name passes. A fuzzy hit is refused with
    the catalog options, never silently substituted — live, 'underwater basket weaving' fuzzy-
    matched to 'basketball' and the count ran on sports teams; an export would have charged for
    them. Empty in -> empty out (no industry filter)."""
    text = (text or "").strip()
    if not text:
        return ""
    resolved: list[str] = []
    for part in [t.strip() for t in text.split(",") if t.strip()]:
        payload = _mcp_call("industry_search", {"query": part}, strict=True)
        opts = [o for o in (payload.get("industries") or []) if isinstance(o, str)]
        exact = next((o for o in opts if o.lower() == part.lower()), None)
        if exact is None:
            if not opts:
                # The catalog search is a plain substring match ('recruitment' finds nothing, though
                # 'staffing and recruiting' exists). Gather SUGGESTIONS from word stems — free
                # catalog lookups, still a refusal, never a substitution.
                for w in part.lower().split():
                    stem = w[:5] if len(w) > 5 else w
                    if len(stem) < 4:
                        continue
                    more = _mcp_call("industry_search", {"query": stem}, strict=False)
                    opts += [o for o in (more.get("industries") or []) if isinstance(o, str) and o not in opts]
            raise UnresolvedFilter("industry", part, opts[:12])
        resolved.append(exact)
    return ",".join(resolved)


def _resolve_location(text: str) -> str:
    """Resolve a location to an exact catalog leaf name (comma-separated inputs one by one).
    Exact match only — anything else is refused with the catalog's closest names so the exact one
    can be chosen and retried. Empty in -> empty out."""
    text = (text or "").strip()
    if not text:
        return ""
    resolved: list[str] = []
    for part in [t.strip() for t in text.split(",") if t.strip()]:
        payload = _mcp_call("location_search", {"query": part}, strict=True)
        opts = [o for o in (payload.get("locations") or []) if isinstance(o, str)]
        exact = next((o for o in opts if o.lower() == part.lower()), None)
        if exact is None:
            raise UnresolvedFilter("location", part, opts[:12])
        resolved.append(exact)
    return ",".join(resolved)


def _resolve(f: dict) -> tuple[str, str]:
    return _resolve_industry(f.get("industry", "")), _resolve_location(f.get("country", ""))


_COMPANY_KEYWORD_SOURCES = "NAME,KEYWORD,SEO,DESCRIPTION,INDUSTRY"


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
        # keywordSources is REQUIRED by the hosted MCP even though its schema says "defaults to all":
        # without it AI-ARK answers '401 service unavailable' (verified live 2026-09-05 — with the
        # sources named, the same search returns keyword-matched companies). Send all five.
        a["keyword"] = ",".join(f["keywords"])
        a["keywordMode"] = "SMART"
        a["keywordSources"] = _COMPANY_KEYWORD_SOURCES
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
    if f.get("keywords"):
        # A brief's keywords describe the COMPANY ("fintech", "payroll"), so for a people search they
        # go on the company-keyword dial. They used to be dropped here silently (a keyworded
        # decision-maker export pulled the whole industry and charged for it).
        a["companyKeyword"] = ",".join(f["keywords"])
        a["companyKeywordMode"] = "SMART"
        a["companyKeywordSources"] = _COMPANY_KEYWORD_SOURCES  # required, see _company_args
    return a


def _search(tool: str, args: dict) -> dict:
    """One strict search call. Raises DataUnavailable if the response is not a real search result
    (missing totalElements) — a genuine zero-match search still carries totalElements:0."""
    payload = _mcp_call(tool, args, strict=True)
    if not isinstance(payload, dict) or "totalElements" not in payload:
        keys = list(payload)[:10] if isinstance(payload, dict) else type(payload).__name__
        err = payload.get("error") or payload.get("message") or payload.get("text") if isinstance(payload, dict) else ""
        _log.warning("ai-ark %s returned no totalElements; keys=%s; error=%s; args=%s",
                     tool, keys, str(err)[:300], sorted(args))
        if err:
            raise ProviderRefused(tool, str(err)[:160], args)
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


# ---------- Partner-signal pull: decision-makers AT one company (scoped by domain) ----------

def _domain_of(website: str) -> str:
    """Reduce a website/URL to a bare domain AI-ARK can match a company on."""
    d = (website or "").strip().lower()
    for pre in ("https://", "http://"):
        if d.startswith(pre):
            d = d[len(pre):]
    if d.startswith("www."):
        d = d[4:]
    return d.split("/")[0].strip()


def pull_decision_makers(company: dict, role_sets: list[dict], cap: int) -> list[dict]:
    """Pull up to `cap` decision-makers AT one company, tied to it by domain, across the given
    role_sets (each = {seniorities, departments, titles}). This is the partner-signal Phase-D pull:
    real credits track the rows returned, and it never returns more than `cap`. Mock mode returns
    deterministic rows so the flow is exercised without a key. One people_search per role set until
    the cap is filled — so the credit spend is bounded by `cap`, not by the number of role sets."""
    cap = int(cap)
    if cap <= 0:
        return []
    # A role set with no seniority/department/title would match EVERYONE at the company (interns
    # included) and charge for them — drop such sets; with none left, pull nothing.
    role_sets = [rs for rs in (role_sets or [])
                 if isinstance(rs, dict) and (rs.get("seniorities") or rs.get("departments") or rs.get("titles"))]
    if not role_sets:
        return []
    domain = _domain_of(company.get("website") or company.get("domain") or "")
    cname = company.get("name") or company.get("company_name") or ""
    if _mock():
        base = (cname or domain or "Company").split(".")[0].title()
        titles = ["Head of Partnerships", "Founder", "Head of Communications"]
        return [{"full_name": f"{base} DM {i + 1}", "title": titles[i % len(titles)],
                 "company_name": cname or base, "website": domain or "mock.example",
                 "country": "", "linkedin": ""} for i in range(min(cap, 3))]
    rows: list[dict] = []
    seen: set = set()
    for rs in role_sets:
        if len(rows) >= cap:
            break
        args: dict = {"page": 0, "size": min(100, cap - len(rows))}
        if domain:
            args["companyDomain"] = domain
        elif cname:
            args["companyName"] = cname
        else:
            break  # nothing to tie the search to — never pull an untethered company-wide list
        if rs.get("seniorities"):
            args["seniority"] = ",".join(rs["seniorities"])
        if rs.get("departments"):
            args["department"] = ",".join(rs["departments"])
        if rs.get("titles"):
            args["title"] = ",".join(rs["titles"])
        payload = _search("people_search", args)
        for p in (payload.get("content") or []):
            fr = _flatten_person(p)
            key = (fr.get("linkedin") or "", (fr.get("full_name") or "").strip().lower())
            if key == ("", ""):
                key = (fr.get("full_name", ""), fr.get("company_name", ""))
            if key in seen:
                continue
            seen.add(key)
            rows.append(fr)
            if len(rows) >= cap:
                break
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


def _person_id(p: dict) -> tuple[str, str, str, str]:
    linkedin = str(p.get("linkedin") or p.get("linkedin_url") or "").strip()
    name = str(p.get("full_name") or p.get("name") or "").strip()
    domain = str(p.get("company_domain") or p.get("domain") or p.get("website") or "").strip()
    company = str(p.get("company_name") or p.get("company") or "").strip()
    return linkedin, name, domain, company


def _has_id(p: dict) -> bool:
    linkedin, name, _, _ = _person_id(p)
    return bool(linkedin or name)


def _email_args(p: dict) -> dict:
    # A LinkedIn URL is a UNIQUE key, so search on it ALONE. Adding fullName + companyDomain on top
    # AND-s three fields, so any mismatch in AI-ARK's stored name/domain (e.g. "Ryan Y." vs the
    # profile's real name) excludes the right person. Only fall back to name + company when there
    # is no LinkedIn URL (then take a few candidates so a valid email in row 1-2 is still caught).
    linkedin, name, domain, company = _person_id(p)
    if linkedin:
        return {"size": 1, "linkedin": linkedin}
    args: dict = {"size": 3}
    if name:
        args["fullName"] = name
    if domain:
        args["companyDomain"] = domain
    elif company:
        args["companyName"] = company
    return args


def _phone_body(p: dict) -> dict | None:
    linkedin, name, domain, _ = _person_id(p)
    body: dict = {"type": "MOBILE"}
    if linkedin:
        body["linkedin"] = linkedin
    elif name and domain:
        body.update({"name": name, "domain": domain})
    else:
        return None
    return body


def _extract_email(payload: dict) -> str:
    """AI-ARK's email_finder nests the address at content[].email.output[].address. Prefer a VALID
    result; fall back to any address returned."""
    for row in _rows(payload):
        if not isinstance(row, dict):
            continue
        em = row.get("email")
        if isinstance(em, str) and "@" in em:
            return em
        outs = em.get("output") if isinstance(em, dict) else (em if isinstance(em, list) else [])
        best = ""
        for o in outs or []:
            if not isinstance(o, dict):
                continue
            addr = o.get("address") or o.get("email")
            if not addr:
                continue
            if str(o.get("status", "")).upper() == "VALID":
                return str(addr)
            best = best or str(addr)
        if best:
            return best
    return ""


def _extract_phone(payload: dict) -> str:
    """mobile_phone_finder shape is not firmly documented, so read defensively: nested
    <field>.output[].{number,phone,address}, a list of the same, or a flat field."""
    for row in _rows(payload):
        if not isinstance(row, dict):
            continue
        for key in ("phone", "mobile", "phoneNumber", "number"):
            v = row.get(key)
            if isinstance(v, str) and v:
                return v
            outs = v.get("output") if isinstance(v, dict) else (v if isinstance(v, list) else [])
            for o in outs or []:
                if isinstance(o, dict):
                    n = o.get("number") or o.get("phone") or o.get("address") or o.get("value")
                    if n:
                        return str(n)
                elif o:
                    return str(o)
    return ""


def _ark_error(payload: dict) -> str:
    """Detect an AI-ARK error envelope (out of credits, quota, auth) so we can surface it instead
    of silently reporting 'nothing found'. Returns the message, or '' if the payload looks fine."""
    if not isinstance(payload, dict):
        return ""
    for k in ("error", "message", "text", "detail"):
        v = payload.get(k)
        if isinstance(v, str) and any(w in v.lower() for w in
                                      ("credit", "402", "quota", "insufficient", "unauthor", "forbidden")):
            return v.strip()
    return ""


async def _amcp(client, tool: str, arguments: dict, key: str) -> dict:
    """Async call to one AI-ARK MCP tool. Returns the parsed payload, or a {"error": ...} envelope
    on an HTTP error status (so out-of-credits / auth refusals are visible to _ark_error), or {}
    on a transport failure (logged, never raised). Async so the event loop is never blocked."""
    try:
        r = await client.post(
            MCP_BASE,
            params={"token": key},
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                  "params": {"name": tool, "arguments": arguments}},
            headers={"content-type": "application/json",
                     "accept": "application/json, text/event-stream"},
        )
        if r.status_code >= 400:
            _log.warning("ai-ark %s -> HTTP %s: %s", tool, r.status_code, r.text[:200])
            return {"error": f"HTTP {r.status_code}: {r.text[:200]}"}
        return _mcp_payload(r)
    except Exception as e:
        _log.warning("ai-ark async call failed (%s): %s", tool, e)
        return {}


# email_finder is asynchronous (a quick job that resolves in a few seconds). We fire every job
# up front, then poll the still-pending ones together on a short, non-blocking budget. The whole
# batch finishes in seconds and NEVER blocks the server — awaits yield the loop so ping and other
# calls stay live (mcp 1.x runs sync tools on the event loop, so a sleeping sync tool would hang
# the whole vault — which is the bug this replaced).
_POLL_BUDGET_S = 20.0     # base; the real budget scales with how many jobs are still pending
_POLL_EVERY_S = 2.5
_POLL_SIZE = 3            # ask for a few results per job, not 1 — the person's email may not be row 0
_MAX_CONCURRENCY = 5     # AI-ARK rate limit is ~5/s; never fire more calls than that at once
_PENDING_STALE_S = 300   # a job still 'pending' after this long is reported as not found, not "running"


def _poll_budget_s(pending: int) -> float:
    """How long to WAIT for the whole batch to resolve. No cap on how many people we enrich — this
    is only a wait ceiling. The poll loop exits the instant every job is done, so small batches stay
    fast; this just gives a big batch enough time to finish rather than dropping anyone. Generous
    upper bound guards against a pathological hang, not against batch size."""
    return min(180.0, 25.0 + 5.0 * pending)


def _ident(p: dict) -> str:
    """Stable identity for the enrichment cache: the LinkedIn URL, else name@company."""
    linkedin, name, domain, company = _person_id(p)
    if linkedin:
        return "li:" + linkedin.lower().rstrip("/")
    if name and (domain or company):
        return "nm:" + name.lower() + "@" + (domain or company).lower()
    return ""


async def enrich_async(people: list[dict], want_phone: bool = True) -> list[dict]:
    """Add 'email' (and 'phone' when want_phone) to each person. Input rows are identified by a
    LinkedIn URL, or a name plus company domain/name. Returns a NEW list; originals untouched.

    Each row also carries: 'source' — 'cache' (known from an earlier lookup, NOT charged),
    'lookup' (a paid lookup fired now), 'resumed' (a still-running earlier job collected for
    free), or 'skipped' (no usable identity); 'pending' True when the provider's job is still
    running at return time (call again in a minute — it is collected free, not re-charged); and
    'ark_error' when the provider refused that person's lookup (out of credits, auth). Rows we
    can't resolve come back with empty email/phone and enriched=False — never an exception."""
    people = people or []
    if _mock():
        out = []
        for i, p in enumerate(people):
            _, name, domain, _ = _person_id(p)
            handle = (name or f"person{i + 1}").lower().replace(" ", ".")
            out.append({**p, "email": f"{handle}@{domain or 'example.com'}",
                        "phone": (f"+1415555{1000 + i:04d}" if want_phone else ""),
                        "enriched": True, "mock": True, "source": "lookup", "pending": False,
                        "ark_error": ""})
        return out

    from db import dal

    key = _require_key()  # refuse before spend
    import httpx

    n = len(people)
    emails, phones = [""] * n, [""] * n
    source, errs = ["skipped"] * n, [""] * n
    phone_fired = [False] * n
    stale: set[int] = set()  # resumed jobs that have sat 'pending' too long to still be running
    idents = [_ident(p) for p in people]
    cache = dal.enrich_cache_get(idents)
    pending: list[tuple[int, str]] = []
    to_fire: list[int] = []
    for i, p in enumerate(people):
        row = cache.get(idents[i])
        if row and row["state"] in ("done", "miss"):
            emails[i], phones[i], source[i] = row["email"], row["phone"], "cache"
        elif row and row["state"] == "pending" and row["track_id"]:
            pending.append((i, row["track_id"]))
            source[i] = "resumed"
            try:
                age = (datetime.now(timezone.utc) - datetime.fromisoformat(row["updated_at"])).total_seconds()
            except Exception:
                age = 0
            if age > _PENDING_STALE_S:
                stale.add(i)
        elif _has_id(p):
            to_fire.append(i)

    async with httpx.AsyncClient(timeout=30) as client:
        sem = asyncio.Semaphore(_MAX_CONCURRENCY)  # respect AI-ARK's ~5/s rate limit on every call

        async def call(tool, args):
            async with sem:
                return await _amcp(client, tool, args, key)

        # 1. Fire an email_finder job for everyone not already known (throttled). Any provider
        #    refusal is recorded PER PERSON, so a balance that runs out mid-batch shows as
        #    "couldn't be checked" for the people after it — never as "not found".
        started = await asyncio.gather(*[call("email_finder", _email_args(people[i])) for i in to_fire])
        for i, s_ in zip(to_fire, started):
            source[i] = "lookup"
            err = _ark_error(s_)
            if err:
                errs[i] = err
                continue
            em = _extract_email(s_)
            if em:
                emails[i] = em
                continue
            track = s_.get("trackId") or s_.get("trackID") or s_.get("track_id")
            if track:
                pending.append((i, str(track)))
                dal.enrich_cache_put(idents[i], "pending", track_id=str(track))
        # 2. Poll the pending jobs together on a budget that scales with how many are still open.
        #    The loop exits the moment every job is done; the ceiling only guards against a hang.
        deadline = time.monotonic() + _poll_budget_s(len(pending))
        while pending and time.monotonic() < deadline:
            await asyncio.sleep(_POLL_EVERY_S)
            polled = await asyncio.gather(
                *[call("email_finder_results", {"trackId": t, "size": _POLL_SIZE}) for _, t in pending])
            still: list[tuple[int, str]] = []
            for (i, t), res in zip(pending, polled):
                err = _ark_error(res)
                if err:
                    errs[i] = err
                    continue
                em = _extract_email(res)
                if em:
                    emails[i] = em
                elif str(res.get("state", "")).upper() != "DONE":
                    still.append((i, t))  # keep waiting; DONE-with-no-email drops out
            pending = still
        # 3. Phones — best-effort, throttled, no polling; only for people not already holding one.
        if want_phone:
            pidx = [i for i, p in enumerate(people) if not phones[i] and _phone_body(p) and not errs[i]]
            phres = await asyncio.gather(
                *[call("mobile_phone_finder", {"requestBody": json.dumps(_phone_body(people[i]))})
                  for i in pidx])
            for i, res in zip(pidx, phres):
                phone_fired[i] = True  # a paid attempt, hit or miss
                phones[i] = _extract_phone(res)

    # 4. Remember what we learned so the same person is never billed twice.
    still_pending = {i for i, _ in pending}
    # A resumed job that is STILL not done after the stale window is a miss, not "still running":
    # the provider never marks some thin-coverage profiles DONE. Say "not found" and stop waiting.
    for i in list(still_pending):
        if i in stale:
            still_pending.discard(i)
    for i in range(n):
        if source[i] in ("lookup", "resumed") and not errs[i] and i not in still_pending:
            dal.enrich_cache_put(idents[i], "done" if emails[i] else "miss", email=emails[i], phone=phones[i])
        elif source[i] == "cache" and want_phone and phones[i] and not cache[idents[i]].get("phone"):
            dal.enrich_cache_put(idents[i], "done" if emails[i] else "miss", email=emails[i], phone=phones[i])
    return [{**p, "email": emails[i], "phone": phones[i],
             "enriched": bool(emails[i] or phones[i]), "mock": False, "source": source[i],
             "pending": i in still_pending, "phone_lookup": phone_fired[i], "ark_error": errs[i]}
            for i, p in enumerate(people)]
