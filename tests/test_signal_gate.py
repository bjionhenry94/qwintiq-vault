"""Signal-gate durability + real-pull regression (no server needed).

Proves the two live bugs from the "Prospect signal failure" report are fixed:
  1. The supervised gate token survives between the count call and the confirm call because it
     lives in the DB (dal.gate_put / gate_take), not process memory — and is single-use and
     TTL-bounded. The old in-memory dict "expired" on the very next call once deployed.
  2. Confirming actually PULLS decision-makers (real data shape), instead of running the framework
     through the LLM and returning conversational filler that pulled nobody.

Run:  VAULT_AIARK=mock VAULT_LLM=mock python tests/test_signal_gate.py
"""
import json
import os
import sys
import tempfile
import time
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


# 1. DAL-level: put -> take once -> None (single use), and durability (a fresh read, no in-memory).
dal.gate_put("tok-A", {"candidates": [{"name": "X"}], "n": 3}, 1800)
check("gate_take returns the stored snapshot", dal.gate_take("tok-A") == {"candidates": [{"name": "X"}], "n": 3})
check("gate_take is single-use (second read is None)", dal.gate_take("tok-A") is None)

# 2. TTL expiry
dal.gate_put("tok-B", {"n": 1}, 1)
time.sleep(1.3)
check("gate_take rejects an expired token", dal.gate_take("tok-B") is None)

# 3. Unknown token
check("gate_take on an unknown token is None", dal.gate_take("does-not-exist") is None)

# 4. Tool flow (mock; consultant context None -> shared seed routine)
rlist = json.loads(tools.qwintiq_routine_list())
rname = rlist[0]["name"] if rlist else "Partner and PR signals"
cands = [{"name": "Acme", "website": "acme.com", "what_happened": "hired a Head of Partnerships"},
         {"name": "Beta", "website": "beta.io", "what_happened": "launched a partner program"}]
g = json.loads(tools.qwintiq_partner_signals(routine_name=rname, candidate_companies=cands))
check("gate call returns a token + count", bool(g.get("gate_token")) and bool(g.get("estimated_people")))

n = g["estimated_people"]
ph = f"I confirm to export this and use {n} amount of credits"
proceed = json.loads(tools.qwintiq_partner_signals(routine_name=rname, confirmation_phrase=ph,
                                                   gate_token=g["gate_token"]))
check("confirm pulls real decision-makers (not an LLM essay)",
      isinstance(proceed.get("decision_makers"), list) and proceed.get("rows", 0) >= 1)
check("pull never exceeds the confirmed number", 0 < proceed.get("rows", 0) <= n)

reuse = json.loads(tools.qwintiq_partner_signals(routine_name=rname, confirmation_phrase=ph,
                                                 gate_token=g["gate_token"]))
check("token is single-use across tool calls (expired on reuse)", "expired" in json.dumps(reuse).lower())

# 5. A wrong number is refused, but re-issues a fresh, usable token (no stranded shortlist).
g2 = json.loads(tools.qwintiq_partner_signals(routine_name=rname, candidate_companies=cands))
bad = f"I confirm to export this and use {g2['estimated_people'] + 99} amount of credits"
mism = json.loads(tools.qwintiq_partner_signals(routine_name=rname, confirmation_phrase=bad,
                                                gate_token=g2["gate_token"]))
check("number mismatch refused with a fresh token", bool(mism.get("gate")) and bool(mism.get("gate_token")))
good = f"I confirm to export this and use {g2['estimated_people']} amount of credits"
recover = json.loads(tools.qwintiq_partner_signals(routine_name=rname, confirmation_phrase=good,
                                                   gate_token=mism["gate_token"]))
check("re-issued token then confirms and pulls", recover.get("rows", 0) >= 1)

print(f"\n{sum(results)}/{len(results)} passed")
sys.exit(0 if all(results) else 1)
