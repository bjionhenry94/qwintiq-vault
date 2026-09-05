"""Design-audit fixes (2026-09-05) — every one is a refuse-before-spend or never-swap rule.

  2. exclude_keywords refused (AI-Ark has no such dial); people-export keywords go on companyKeyword.
  3. Unknown industry / location refuses before any paid search.
  4. Production never falls back to host-env keys; an undecryptable saved key is reported, not swapped.
  6. No credit caps: autopilot needs no daily_credit_cap.
  9. Enrich: cache (never bill twice), resume a still-running job free, per-person provider refusals.
 10. Role-less routines refused at save AND at pull.
 11. Export dedupe keeps rows that have nothing to dedupe on.
 12. PKCE mandatory; code bound to the client; atomic gate consume.

Run:  python tests/test_design_fixes.py     (no network, no credits; sqlite temp DB)
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("VAULT_LLM", "mock")
os.environ.setdefault("VAULT_LEMLIST", "mock")
os.environ.pop("VAULT_AIARK", None)          # we drive the real code path with a fake transport
os.environ.pop("DATABASE_URL", None)
os.environ.pop("RENDER", None)
os.environ.pop("VAULT_ENV", None)
os.environ.pop("AI_ARK_API_KEY", None)
os.environ["SECRET_KEY"] = "design-fixes-test-secret"
os.environ["VAULT_SQLITE"] = tempfile.mktemp(suffix=".sqlite3")

from db import dal  # noqa: E402
from vault import aiark, tools  # noqa: E402

dal.init_db()
PASSED = FAILED = 0


def check(name, cond, detail=""):
    global PASSED, FAILED
    if cond:
        PASSED += 1
        print("PASS", name)
    else:
        FAILED += 1
        print("FAIL", name, detail)


PHRASE = "I confirm to export this and use {n} amount of credits"

# ---- 4. never swap keys: prod ignores env; decrypt failure is reported ----
os.environ["AI_ARK_API_KEY"] = "navreo-env-key"
check("dev: env key accepted as a fallback", dal.get_secret("AI_ARK_API_KEY") == "navreo-env-key")
os.environ["RENDER"] = "true"
check("prod: env key IGNORED (no fallback)", dal.get_secret("AI_ARK_API_KEY") is None)
check("prod: status says not_set even though env has one", dal.secret_status("AI_ARK_API_KEY") == "not_set")
check("prod: aiark is NOT mock with no key (it refuses instead)", aiark._mock() is False)
try:
    aiark.count_companies({"industry": "software", "country": "United States"})
    check("prod: count with no key raises DataKeyMissing", False)
except aiark.DataKeyMissing:
    check("prod: count with no key raises DataKeyMissing", True)
out = tools.qwintiq_list_count(what_you_sell="x", industry="software", country="United States")
check("tool surface: no-key refusal is loud and says nothing was charged",
      "NOT CONNECTED" in out and "nothing was charged" in out, out[:120])
# an undecryptable saved key
dal.set_secret("AI_ARK_API_KEY", "aliyah-key")
check("prod: saved key read back", dal.get_secret("AI_ARK_API_KEY") == "aliyah-key")
dal.q("update settings set value=? where name=?", ("gAAAAA-corrupt", "AI_ARK_API_KEY"))
check("prod: corrupt saved key -> None, not the env key", dal.get_secret("AI_ARK_API_KEY") is None)
check("prod: status = decrypt_failed", dal.secret_status("AI_ARK_API_KEY") == "decrypt_failed")
dal.set_secret("AI_ARK_API_KEY", "aliyah-key")
check("prod: re-saving heals it", dal.secret_status("AI_ARK_API_KEY") == "managed_here")
os.environ.pop("RENDER", None)
os.environ.pop("AI_ARK_API_KEY", None)
# From here: dev mode, DB key set -> real (fake-transport) path, never mock.

# ---- fake AI-Ark transport ----
CALLS: list[tuple[str, dict]] = []
CATALOG = {"industry_search": {"industries": ["software", "software development"]},
           "location_search": {"locations": ["United States", "United Kingdom"]}}


def fake_mcp_call(tool, arguments, strict=False):
    CALLS.append((tool, arguments))
    if tool in CATALOG:
        q = arguments.get("query", "").lower()
        opts = [o for o in CATALOG[tool][next(iter(CATALOG[tool]))] if q in o.lower()]
        return {next(iter(CATALOG[tool])): opts}
    if tool in ("company_search", "people_search"):
        size = int(arguments.get("size", 25))
        tag = arguments.get("companyDomain", "")
        rows = [{"summary": {"name": f"Co {i}"}, "link": {"domain": ""},
                 "profile": {"full_name": f"P {i} {tag}"}, "company": {"summary": {"name": f"Co {i}"}}}
                for i in range(min(size, 4))]
        return {"totalElements": 4, "content": rows}
    return {}


aiark._mcp_call = fake_mcp_call
tools.aiark._mcp_call = fake_mcp_call

# ---- 2. exclude_keywords refused; keywords land on companyKeyword for people ----
CALLS.clear()
out = tools.qwintiq_list_count(what_you_sell="x", industry="software", country="United States",
                               exclude_keywords=["agency"])
check("count: exclude_keywords refused before any call", "REFUSED" in out and not CALLS, out[:100])
out = tools.qwintiq_list_export(kind="decision_makers", filters={"industry": "software", "exclude_keywords": ["agency"]},
                                max_rows=4, confirmation_phrase=PHRASE.format(n=4))
check("export: exclude_keywords refused before any call", "REFUSED" in out and not CALLS, out[:100])
out = tools.qwintiq_list_export(kind="decision_makers", filters={"industry": "software", "exclude_keywords": []},
                                max_rows=4, confirmation_phrase=PHRASE.format(n=4))
check("export: an EMPTY exclude_keywords list is harmless", "csv" in out, out[:100])
args = aiark._people_args({"keywords": ["fintech", "payroll"]}, "", "")
check("people keywords -> companyKeyword (were dropped)", args.get("companyKeyword") == "fintech,payroll"
      and args.get("companyKeywordMode") == "SMART", str(args))
check("people keywords carry companyKeywordSources (omitting it = provider 401)",
      args.get("companyKeywordSources") == "NAME,KEYWORD,SEO,DESCRIPTION,INDUSTRY", str(args))
cargs = aiark._company_args({"keywords": ["payroll"]}, "", "")
check("company keywords carry keywordSources", cargs.get("keywordSources") == "NAME,KEYWORD,SEO,DESCRIPTION,INDUSTRY", str(cargs))

# ---- 3. unresolved industry / location refuse before spend ----
CALLS.clear()
out = tools.qwintiq_list_count(what_you_sell="x", industry="underwater basket weaving", country="United States")
check("unknown industry refuses", "REFUSED" in out and "nothing was pulled or charged" in out, out[:120])
check("...and made only the catalog lookup, no search", all(t == "industry_search" for t, _ in CALLS), str(CALLS))
CALLS.clear()
out = tools.qwintiq_list_count(what_you_sell="x", industry="soft", country="United States")
check("FUZZY industry hit refuses too (live: 'basket weaving' -> 'basketball'), listing options",
      "REFUSED" in out and "software development" in out, out[:160])
check("...no paid search fired on the fuzzy hit", all(t == "industry_search" for t, _ in CALLS), str(CALLS))
CALLS.clear()
out = tools.qwintiq_list_count(what_you_sell="x", industry="software, software development", country="United States")
check("comma-separated exact industries resolve", json.loads(out)["resolved"]["industry"] == "software,software development", out[:160])
CALLS.clear()
out = tools.qwintiq_list_count(what_you_sell="x", industry="software", country="United")
check("ambiguous location refuses and names the catalog options",
      "REFUSED" in out and "United States" in out and "United Kingdom" in out, out[:160])
check("...no paid search fired", not any(t.endswith("_search") and t not in CATALOG for t, _ in CALLS), str(CALLS))
out = tools.qwintiq_list_count(what_you_sell="x", industry="software", country="United States")
check("exact names still count", json.loads(out)["companies_matching"] == 4, out[:120])

# ---- provider error envelope -> plain refusal naming the keyword cause (live: 401 on keyword search) ----
_orig = aiark._mcp_call
def refusing_mcp_call(tool, arguments, strict=False):
    if tool.endswith("_search") and ("keyword" in arguments or "companyKeyword" in arguments):
        return {"error": "401 service unavailable"}
    return _orig(tool, arguments, strict)
aiark._mcp_call = refusing_mcp_call
out = tools.qwintiq_list_export(kind="companies", filters={"industry": "software", "keywords": ["payroll"]},
                                max_rows=1, confirmation_phrase=PHRASE.format(n=1))
check("keyword search rejected by provider -> REFUSED BY THE DATA PROVIDER, keyword named",
      "REFUSED BY THE DATA PROVIDER" in out and "keyword" in out and "nothing was charged" in out, out[:200])
out = tools.qwintiq_list_count(what_you_sell="x", industry="software", country="United States", keywords=["payroll"])
check("...same on count", "REFUSED BY THE DATA PROVIDER" in out, out[:120])
aiark._mcp_call = _orig

# ---- 11. export dedupe keeps rows with nothing to dedupe on ----
out = json.loads(tools.qwintiq_list_export(kind="companies", filters={"industry": "software"},
                                           max_rows=4, confirmation_phrase=PHRASE.format(n=4)))
check("4 companies with empty websites all kept (were collapsed to 1)", out["rows"] == 4, str(out["rows"]))

# ---- 10. role-less routine refused at save and at pull ----
out = tools.qwintiq_routine_save("No roles", {"run_mode": "supervised", "signals": ["x"]})
check("routine without roles refused at save", "ROUTINE REFUSED" in out, out[:100])
out = tools.qwintiq_routine_save("Roles ok", {"run_mode": "autopilot",
                                              "decision_makers": {"target_roles": ["Founder"], "max_per_company": 2}})
check("autopilot routine saves WITHOUT a daily_credit_cap (no caps)", "saved" in out, out)
out = tools._run_partner_pull({"decision_makers": {}}, [{"name": "A", "website": "a.com"}], "", None)
check("pull with no roles refused before spend", "ROUTINE PULL REFUSED" in out, out[:100])
check("pull_decision_makers with role-less sets pulls nothing",
      aiark.pull_decision_makers({"name": "A", "website": "a.com"}, [{}], 3) == [])

# ---- 6. autopilot runs with no cap, bounded by max_per × companies ----
CALLS.clear()
out = json.loads(tools.qwintiq_partner_signals(routine_name="Roles ok",
                                               candidate_companies=[{"name": "A", "website": "a.com"},
                                                                    {"name": "B", "website": "b.com"}]))
check("autopilot pulls without a cap, bounded 2×2", out.get("rows") == 4, str(out)[:160])

# ---- 9. enrich: cache, resume, per-person refusals (fake async transport) ----
ACALLS: list[tuple[str, dict]] = []
STATE = {"credits": True, "done": set()}


async def fake_amcp(client, tool, arguments, key):
    ACALLS.append((tool, arguments))
    if tool == "email_finder":
        if not STATE["credits"]:
            return {"error": "HTTP 402: insufficient credits"}
        who = arguments.get("linkedin") or arguments.get("fullName")
        return {"trackId": "t-" + who, "state": "PENDING"}
    if tool == "email_finder_results":
        who = arguments["trackId"][2:]
        if who in STATE["done"]:
            return {"state": "DONE", "content": [{"email": {"output": [{"address": f"{who}@x.com", "status": "VALID"}]}}]}
        return {"state": "PENDING"}
    return {}


aiark._amcp = fake_amcp
aiark._POLL_EVERY_S = 0.01
aiark._poll_budget_s = lambda pending: 0.05
PEOPLE = [{"linkedin": "https://linkedin.com/in/ann"}, {"linkedin": "https://linkedin.com/in/bob"}]
STATE["done"] = {"https://linkedin.com/in/ann"}
r1 = json.loads(asyncio.run(tools.qwintiq_enrich(PEOPLE, PHRASE.format(n=2))))
check("first run: ann found, bob still pending", r1["found_email"] == 1 and r1["still_pending"] == 1, r1["receipt"])
check("first run: charged 2 (both lookups fired)", r1["charged"] == 2, r1["receipt"])
fired = [a for t, a in ACALLS if t == "email_finder"]
ACALLS.clear()
STATE["done"].add("https://linkedin.com/in/bob")
r2 = json.loads(asyncio.run(tools.qwintiq_enrich(PEOPLE, PHRASE.format(n=2))))
check("second run: NO new email_finder fired (ann cached, bob resumed)",
      not [a for t, a in ACALLS if t == "email_finder"], str(ACALLS)[:200])
check("second run: both emails present, charged 0", r2["found_email"] == 2 and r2["charged"] == 0, r2["receipt"])
check("receipt says free", "free" in r2["receipt"], r2["receipt"])
# provider refuses mid-batch: a NEW person after credits run out is 'couldn't be checked', not 'not found'
STATE["credits"] = False
r3 = json.loads(asyncio.run(tools.qwintiq_enrich(PEOPLE + [{"linkedin": "https://linkedin.com/in/cid"}],
                                                 PHRASE.format(n=3))))
check("cached people still returned when provider refuses new lookups", r3["found_email"] == 2, r3.get("receipt"))
check("refused person reported as could_not_check, charged 0", r3["could_not_check"] == 1 and r3["charged"] == 0, r3["receipt"])
r4 = json.loads(asyncio.run(tools.qwintiq_enrich([{"linkedin": "https://linkedin.com/in/dan"}], PHRASE.format(n=1))))
check("all-new lookups refused -> ENRICH COULD NOT RUN", r4.get("error") == "ENRICH COULD NOT RUN", str(r4)[:120])
STATE["credits"] = True

# ---- 12. atomic gate consume ----
dal.gate_put("tok1", {"n": 1}, 60)
a, b = dal.gate_take("tok1"), dal.gate_take("tok1")
check("gate token consumed exactly once", a == {"n": 1} and b is None)

print(f"\n{PASSED}/{PASSED + FAILED} passed")
sys.exit(1 if FAILED else 0)
