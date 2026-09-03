"""qwintiq_company_people — the per-company decision-maker pull (no server, no credits).

The tool the market export kept being mistaken for: given a SPECIFIC list of companies it pulls the
people AT those companies (tethered by domain), gated by the typed credit sentence, and hands the
result to qwintiq_enrich by LinkedIn URL. It must never fall through to the market export.

Run:  VAULT_AIARK=mock VAULT_LLM=mock python tests/test_company_people.py
"""
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("VAULT_AIARK", "mock")
os.environ.setdefault("VAULT_LEMLIST", "mock")
os.environ.setdefault("VAULT_LLM", "mock")
os.environ.pop("DATABASE_URL", None)
os.environ.pop("RENDER", None)
os.environ.pop("VAULT_ENV", None)
os.environ["VAULT_SQLITE"] = tempfile.mktemp(suffix=".sqlite3")

from db import dal          # noqa: E402
from vault import tools     # noqa: E402
from vault.aiark import DataUnavailable  # noqa: E402

dal.init_db()
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))


# The market export must never be touched by this tool.
calls = {"export_rows": 0, "pull": 0}
_real_export = tools.aiark.export_rows
_real_pull = tools.aiark.pull_decision_makers


def _spy_export(*a, **k):
    calls["export_rows"] += 1
    return _real_export(*a, **k)


def _spy_pull(*a, **k):
    calls["pull"] += 1
    return _real_pull(*a, **k)


tools.aiark.export_rows = _spy_export
tools.aiark.pull_decision_makers = _spy_pull

COS = [{"name": "Scribe", "website": "scribehow.com"}, {"name": "Retell AI", "website": "retellai.com"}]

# 1. Gate: no phrase -> refused, nothing pulled; quotes the right number (2 per co × 2 cos = 4).
r = tools.qwintiq_company_people(companies=COS, confirmation_phrase="")
check("no phrase is refused", r.startswith("COMPANY PULL REFUSED"), r[:100])
check("refusal quotes the right number (4)", "use 4 amount of credits" in r)
check("no pull happened", calls["pull"] == 0)

# 2. Gate: wrong number -> refused.
r = tools.qwintiq_company_people(companies=COS, confirmation_phrase="I confirm to export this and use 9 amount of credits")
check("wrong number is refused", r.startswith("COMPANY PULL REFUSED") and "9" in r)
check("still no pull", calls["pull"] == 0)

# 3. No companies -> refused.
r = tools.qwintiq_company_people(companies=[], confirmation_phrase="I confirm to export this and use 1 amount of credits")
check("empty company list is refused", r.startswith("COMPANY PULL REFUSED"))

# 4. Correct phrase -> pulls per company, tethered rows, ≤ cap, never the market export.
ok = json.loads(tools.qwintiq_company_people(companies=COS, max_per_company=2,
                                             confirmation_phrase="I confirm to export this and use 4 amount of credits"))
check("returns people", isinstance(ok.get("people"), list) and ok.get("rows", 0) >= 1, json.dumps(ok)[:150])
check("never exceeds max_per × companies", 0 < ok["rows"] <= 4)
check("one pull per company (2)", calls["pull"] == 2, f"pull={calls['pull']}")
check("market export never called", calls["export_rows"] == 0)
webs = {p.get("website") for p in ok["people"]}
check("rows are tethered to the given domains", webs <= {"scribehow.com", "retellai.com"}, str(webs))
check("per_company breakdown present", set(ok.get("per_company", {}).keys()) == {"Scribe", "Retell AI"})
check("rows carry a linkedin field for the enrich chain", all("linkedin" in p for p in ok["people"]))
check("next step routes to qwintiq_enrich by LinkedIn", "qwintiq_enrich" in ok["next"] and "linkedin" in ok["next"].lower())
check("receipt states credits = rows", f"about {ok['rows']} credits" in ok["receipt"])

# 5. Every company failing -> the honest data-outage message, never "0 found".
def _boom(*a, **k):
    raise DataUnavailable


tools.aiark.pull_decision_makers = _boom
out = tools.qwintiq_company_people(companies=COS, confirmation_phrase="I confirm to export this and use 4 amount of credits")
check("all companies failing surfaces the data-unavailable message", "temporarily unavailable" in out.lower(), out[:120])
tools.aiark.pull_decision_makers = _spy_pull

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
