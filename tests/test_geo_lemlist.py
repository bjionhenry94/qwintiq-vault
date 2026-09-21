"""Aliyah's 16 Sept issues ("Vault Password" thread): place filters below country, loose keywords,
honest credit notes, Lemlist 400s with no reason, and Claude not reaching for the vault.

Run:  python tests/test_geo_lemlist.py      (no network, no credits)
"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
os.environ.setdefault("VAULT_LLM", "mock")
for k in ("VAULT_AIARK", "VAULT_LEMLIST", "DATABASE_URL", "RENDER", "VAULT_ENV"):
    os.environ.pop(k, None)
os.environ["SECRET_KEY"] = "geo-test-secret"
os.environ["AI_ARK_API_KEY"] = "dev-key"
os.environ["LEMLIST_API_KEY"] = "dev-lemlist-key"
os.environ["VAULT_SQLITE"] = tempfile.mktemp(suffix=".sqlite3")

from db import dal  # noqa: E402
from vault import aiark, lemlist, tools  # noqa: E402

dal.init_db()
P = F = 0


def check(name, cond, detail=""):
    global P, F
    if cond:
        P += 1
        print("PASS", name)
    else:
        F += 1
        print("FAIL", name, detail)


PHRASE = "I confirm to export this and use {n} amount of credits"
CALLS = []
LOCS = ["New York", "New Jersey", "United States", "York"]


def fake(tool, arguments, strict=False):
    CALLS.append((tool, dict(arguments)))
    if tool == "industry_search":
        return {"industries": ["venture capital"]}
    if tool == "location_search":
        q = arguments["query"].lower()
        return {"locations": [l for l in LOCS if any(w in l.lower() for w in q.split())]}
    hq = {"country": "United States", "state": "New York", "city": "Brooklyn",
          "postal_code": "11201", "raw_address": "1 Main St, Brooklyn, New York, United States"}
    return {"totalElements": 39, "content": [{
        "summary": {"name": "ERA", "industry": "venture capital", "staff": {"total": 20}},
        "link": {"domain": "eranyc.com"}, "location": {"headquarter": hq},
        "profile": {"full_name": "Ann Lee", "title": "Partner"},
        "company": {"summary": {"name": "ERA"}, "link": {"domain": "eranyc.com"}, "location": {"headquarter": hq}}}]}


aiark._mcp_call = fake


def search_args(tool):
    return next(a for t, a in CALLS if t == tool)


# ---- place below country ----
out = json.loads(tools.qwintiq_list_count(what_you_sell="x", industry="venture capital",
                                          country="United States", location="New York",
                                          keywords=["accelerator"]))
ca, pa = search_args("company_search"), search_args("people_search")
check("state REPLACES country on company search (tokens are OR-ed upstream)", ca.get("location") == "New York", str(ca))
check("state reaches the people search as companyLocation", pa.get("companyLocation") == "New York", str(pa))
check("resolved location reported back", out["resolved"]["location"] == "New York", str(out["resolved"]))
check("sample row carries city/state/postcode/address",
      out["company_sample"][0]["city"] == "Brooklyn" and out["company_sample"][0]["postal_code"] == "11201"
      and "Main St" in out["company_sample"][0]["address"], str(out["company_sample"]))
check("keywords default to whole-word matching, sources always sent",
      ca.get("keywordMode") == "WORD" and ca.get("keywordSources") and pa.get("companyKeywordMode") == "WORD", str(ca))
check("count note states the real cost, not 'roughly 2 credits'",
      "0.6" in out["note"] and "roughly 2" not in out["note"], out["note"])

CALLS.clear()
near = {"place": "Brooklyn, NY", "lat": 40.68, "lng": -73.94, "radius_miles": 10}
tools.qwintiq_list_count(what_you_sell="x", industry="venture capital", country="United States",
                         location="New York", near=near, keyword_match="strict", keywords=["accelerator"])
ca, pa = search_args("company_search"), search_args("people_search")
check("town/city radius -> geo args in miles", (ca.get("geoLat"), ca.get("geoLng"), ca.get("geoRadius"), ca.get("geoUnit")) == (40.68, -73.94, 10.0, "mi"), str(ca))
check("...and on people search as companyGeo*", pa.get("companyGeoRadius") == 10.0 and pa.get("companyGeoUnit") == "mi", str(pa))
check("keyword_match=strict honoured", ca.get("keywordMode") == "STRICT", str(ca))

CALLS.clear()
out = tools.qwintiq_list_count(what_you_sell="x", industry="venture capital", country="United States",
                               near={"place": "Brooklyn", "radius_miles": 10})
check("near without coordinates refuses before any call", "REFUSED" in out and not [c for c in CALLS if c[0].endswith("_search") and c[0] not in ("industry_search", "location_search")], out[:120])
out = tools.qwintiq_list_count(what_you_sell="x", industry="venture capital", country="United States",
                               near={"lat": 40.6, "lng": -73.9, "radius_miles": 9000})
check("absurd radius refuses", "REFUSED" in out, out[:120])
CALLS.clear()
out = tools.qwintiq_list_count(what_you_sell="x", industry="venture capital", country="", location="Manhattan")
check("a city passed as `location` refuses (not a state) rather than pulling the wrong place",
      "REFUSED" in out and not [c for c in CALLS if c[0] in ("company_search", "people_search")], out[:160])

out = json.loads(tools.qwintiq_list_export(kind="companies", filters={"industry": "venture capital", "location": "New York", "near": near},
                                           max_rows=1, confirmation_phrase=PHRASE.format(n=1)))
head = out["csv"].splitlines()[0]
check("export accepts location + near and the CSV has city/state/postal_code/address",
      all(c in head for c in ("city", "state", "postal_code", "address")), head)
check("company export receipt uses 0.1/row", out["credits_estimate"] == 0.1 and "0.1 per row" in out["receipt"], out["receipt"])
out = json.loads(tools.qwintiq_list_export(kind="decision_makers", filters={"industry": "venture capital", "location": "New York", "seniorities": ["founder"]},
                                           max_rows=1, confirmation_phrase=PHRASE.format(n=1)))
check("people export receipt uses 0.5/row + company_city column",
      out["credits_estimate"] == 0.5 and "company_city" in out["csv"].splitlines()[0], out["receipt"])

# ---- the server tells Claude to use it without being asked ----
ins = tools.mcp.instructions or ""
check("server instructions: use by default for list requests, places below country supported",
      "USE IT BY DEFAULT" in ins and "location" in ins and "near" in ins, ins[:120])

# ---- Lemlist: vault rows map cleanly; refusals carry a reason ----
row = {"full_name": "Nathan Latka", "title": "CEO", "company_name": "Founderpath", "website": "founderpath.com",
       "linkedin": "linkedin.com/in/nathanlatka", "email": "nathan@founderpath.com", "phone": "",
       "enriched": True, "mock": False, "source": "lookup", "pending": False, "ark_error": "",
       "country": "United States", "icebreaker": "Saw the launch."}
body = lemlist._clean_lead(row)
check("vault row mapped to Lemlist fields",
      body.get("firstName") == "Nathan" and body.get("lastName") == "Latka" and body.get("jobTitle") == "CEO"
      and body.get("companyName") == "Founderpath" and body.get("companyDomain") == "founderpath.com"
      and body.get("linkedinUrl") == "https://linkedin.com/in/nathanlatka", str(body))
check("bookkeeping flags / booleans / email never sent as variables",
      not ({"enriched", "mock", "source", "pending", "ark_error", "email", "full_name"} & set(body))
      and all(isinstance(v, str) for v in body.values()), str(body))
check("a non-LinkedIn value in linkedinUrl is dropped, not sent", "linkedinUrl" not in lemlist._clean_lead({"linkedinUrl": "n/a"}))


class R:
    def __init__(self, code, text):
        self.status_code, self.text, self.content = code, text, text.encode()

    def json(self):
        return json.loads(self.text)


import httpx  # noqa: E402

ANSWERS = {"a@x.com": R(200, "{}"), "b@x.com": R(400, '{"error":"LEAD_ALREADY_IN_CAMPAIGN"}'),
           "c@x.com": R(409, '{"error":"LEAD_ALREADY_IN_OTHER_CAMPAIGN"}'), "d@x.com": R(400, "Invalid email")}
httpx.post = lambda url, **kw: ANSWERS[url.split("/leads/")[1].split("?")[0]]
lemlist.resolve_campaign = lambda c: {"id": "cam_1", "name": "Partners"}
out = json.loads(tools.qwintiq_lemlist_upload("Partners", [{"email": e} for e in ANSWERS]))
check("1 added; each refusal carries Lemlist's reason",
      out["added"] == 1 and set(out["rejected"]) == {"already_in_campaign", "in_another_campaign", "invalid_email"}, str(out)[:300])
check("receipt explains WHY in plain words",
      "already in this campaign" in out["receipt"] and "different Lemlist campaign" in out["receipt"], out["receipt"])

print(f"\n{P}/{P + F} passed")
sys.exit(1 if F else 0)
