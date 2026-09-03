"""Market-export mis-targeting regression (no server, no credits).

The live bug from the "Prospect signal failure" thread: a curated shortlist was passed to
qwintiq_list_export as `company_domains`. The key was silently dropped, a generic worldwide
market was pulled instead, and 50 credits were charged for unusable rows. The export must now
REFUSE any targeting/unknown filter key BEFORE the credit gate and BEFORE any AI-Ark call, and
point the model at the tool that actually does per-company pulls.

Run:  VAULT_AIARK=mock VAULT_LLM=mock python tests/test_export_refusal.py
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

dal.init_db()
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))


# Any AI-Ark call on a refused path is a test failure — the whole point is "refuse before spend".
calls = {"export_rows": 0}
_real_export = tools.aiark.export_rows


def _spy_export(*a, **k):
    calls["export_rows"] += 1
    return _real_export(*a, **k)


tools.aiark.export_rows = _spy_export
PHRASE = "I confirm to export this and use 1 amount of credits"

# 1. The exact live failure: company_domains with a valid phrase -> refused, nothing pulled.
out = tools.qwintiq_list_export(kind="decision_makers",
                                filters={"company_domains": ["scribehow.com", "retellai.com"]},
                                max_rows=1, confirmation_phrase=PHRASE)
check("company_domains is refused", out.startswith("EXPORT REFUSED"), out[:120])
check("refusal names the offending key", "company_domains" in out)
check("refusal points at the per-company tool", "qwintiq_company_people" in out)
check("refusal says nothing was charged", "charged" in out.lower())
check("no AI-Ark call on the refused path", calls["export_rows"] == 0, f"export_rows called {calls['export_rows']}x")

# 2. Refusal happens BEFORE the credit gate (a mis-targeted request never asks the user to type a sentence).
out2 = tools.qwintiq_list_export(kind="decision_makers", filters={"websites": ["x.com"]},
                                 max_rows=5, confirmation_phrase="")
check("targeting refused even without a phrase (before the gate)",
      out2.startswith("EXPORT REFUSED") and "confirmation sentence is missing" not in out2)
check("still no AI-Ark call", calls["export_rows"] == 0)

# 3. An unknown, non-targeting key is refused too, listing the supported keys.
out3 = tools.qwintiq_list_export(kind="companies", filters={"industry": "saas", "revenue_band": "1m"},
                                 max_rows=1, confirmation_phrase=PHRASE)
check("unknown key refused", out3.startswith("EXPORT REFUSED") and "revenue_band" in out3)
check("refusal lists the supported keys", "seniorities" in out3 and "industry" in out3)
check("still no AI-Ark call", calls["export_rows"] == 0)

# 4. A legitimate market brief still exports (mock rows), and the credit gate still holds.
good = tools.qwintiq_list_export(kind="decision_makers", filters={"titles": ["Founder"]},
                                 max_rows=1, confirmation_phrase=PHRASE)
check("valid brief still exports", "csv" in good and "full_name" in good, good[:120])
check("valid brief made exactly one AI-Ark call", calls["export_rows"] == 1)
gated = tools.qwintiq_list_export(kind="decision_makers", filters={"titles": ["Founder"]},
                                  max_rows=1, confirmation_phrase="yes go ahead")
check("valid brief without typed phrase is still gated", gated.startswith("EXPORT REFUSED"))
check("gated call made no AI-Ark call", calls["export_rows"] == 1)

# 5. Empty filters (a bare market pull) still work as before.
empty = tools.qwintiq_list_export(kind="decision_makers", filters={}, max_rows=1, confirmation_phrase=PHRASE)
check("empty filters still export", "csv" in empty)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
