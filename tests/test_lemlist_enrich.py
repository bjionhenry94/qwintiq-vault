"""Lemlist "not connected" must be LOUD, and enrich must support only-with-email (no server, no credits).

Two faces of the silent-wrong bug class from the "Prospect signal failure" thread:
  1. With no Lemlist key ever entered, the vault answered with invented cam_mock… campaigns, so the
     model told the user "your campaign isn't recognised". Now: a plain "Lemlist isn't connected"
     message, and fake campaigns ONLY when a test explicitly asks (VAULT_LEMLIST=mock).
  2. "5 emails out of 34 — can it only give me the ones with emails?" Now: only_with_email=True
     returns just those rows and counts the dropped ones honestly.

Run:  VAULT_AIARK=mock VAULT_LLM=mock python tests/test_lemlist_enrich.py
"""
import asyncio
import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
os.environ.setdefault("VAULT_AIARK", "mock")
os.environ.setdefault("VAULT_LLM", "mock")
os.environ.pop("VAULT_LEMLIST", None)   # deliberately NOT mock: we are testing the unset-key path
os.environ.pop("DATABASE_URL", None)
os.environ.pop("RENDER", None)
os.environ.pop("VAULT_ENV", None)
os.environ["VAULT_SQLITE"] = tempfile.mktemp(suffix=".sqlite3")

from db import dal          # noqa: E402
from vault import lemlist, tools  # noqa: E402

dal.init_db()  # fresh DB -> no LEMLIST_API_KEY secret
results = []


def check(name, ok, detail=""):
    results.append(bool(ok))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))


# ---- 1. Lemlist not connected (no key, no explicit mock) ----
check("connection_status names the problem", "isn't connected" in lemlist.connection_status())
c = json.loads(tools.qwintiq_lemlist_campaigns())
check("campaigns tool returns NOT CONNECTED, not fake campaigns",
      c.get("error") == "LEMLIST NOT CONNECTED" and c.get("campaigns") == [], json.dumps(c)[:150])
check("no cam_mock ids leak", "cam_mock" not in json.dumps(c))
check("message points the admin at Settings", "Settings" in c.get("receipt", ""))
u = json.loads(tools.qwintiq_lemlist_upload(campaign="Partner & PR outreach",
                                            leads=[{"email": "x@example.com"}]))
check("upload tool refuses when not connected", u.get("error") == "LEMLIST NOT CONNECTED" and u.get("added") == 0)

# ---- 2. Explicit mock still works for dev/tests ----
os.environ["VAULT_LEMLIST"] = "mock"
check("explicit mock -> connection_status is clear", lemlist.connection_status() == "")
c2 = json.loads(tools.qwintiq_lemlist_campaigns())
check("explicit mock returns the mock campaigns", any(x["id"].startswith("cam_mock") for x in c2.get("campaigns", [])))
os.environ.pop("VAULT_LEMLIST", None)

# ---- 3. enrich only_with_email ----
PH = "I confirm to export this and use 2 amount of credits"
PEOPLE = [{"full_name": "A One", "linkedin": "https://linkedin.com/in/aone"},
          {"full_name": "B Two", "linkedin": "https://linkedin.com/in/btwo"}]


async def _fake_enrich(people, want_phone=False):
    return [{**people[0], "email": "a@one.com", "phone": "", "enriched": True, "mock": False},
            {**people[1], "email": "", "phone": "", "enriched": False, "mock": False}]


tools.aiark.enrich_async = _fake_enrich
d = json.loads(asyncio.run(tools.qwintiq_enrich(people=PEOPLE, confirmation_phrase=PH)))
check("default returns everyone", len(d["people"]) == 2 and d["found_email"] == 1 and d["dropped_no_email"] == 0)
check("receipt reports email count honestly", "Found emails for 1 of 2" in d["receipt"], d["receipt"])
e = json.loads(asyncio.run(tools.qwintiq_enrich(people=PEOPLE, confirmation_phrase=PH, only_with_email=True)))
check("only_with_email returns just the emailed person", len(e["people"]) == 1 and e["people"][0]["email"] == "a@one.com")
check("dropped count is honest", e["dropped_no_email"] == 1 and "1 dropped" in e["receipt"], e["receipt"])
check("total still reflects what was charged", e["total"] == 2)
p = json.loads(asyncio.run(tools.qwintiq_enrich(people=PEOPLE, confirmation_phrase=PH, include_phone=True)))
check("phone asked but none found is said plainly", p["found_phone"] == 0 and "No mobiles came back" in p["receipt"])

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
