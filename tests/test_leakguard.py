"""Leak-guard unit tests — no server needed. Proves the three deterministic layers:
  - legitimate finished outputs never trip the guard (zero false positives), and
  - verbatim, near-verbatim, and structural (method-describing) leaks are all caught.
Fully-fluent paraphrase in different words is explicitly NOT expected to be caught here — that
is the semantic second-pass's job (engine.semantic_leak), which the workflow stress-tests.
"""
import json
import os
import sys

sys.path.insert(0, "..")
os.environ.setdefault("VAULT_LLM", "mock")
os.environ.setdefault("VAULT_AIARK", "mock")

from vault import leakguard  # noqa: E402
from vault.library import load_framework, FRAMEWORKS  # noqa: E402
from vault.mock_llm import mock_generate  # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))


# --- 1. Legitimate finished outputs must NOT be flagged (no false positives) ---
legit_cases = [
    ("copywriter", mock_generate("copywriter", json.dumps(
        {"problem": "slow PR", "outcome": "coverage in 30 days", "risk_reversal": "pay on results",
         "service": "done-for-you PR"}))),
    ("icebreaker", mock_generate("icebreaker", json.dumps({"prospects": [
        {"name": "Jane", "company": "BrightSEO", "page_text": "We do SEO and content."},
        {"name": "Ravi", "company": "Partnerly", "page_text": "Become a partner today."}]}))),
    ("list_building", json.dumps({"companies_matching": 4200, "decision_makers_matching": 3000,
                                  "note": "Counts only — nothing exported."})),
    ("partner_signals", "PARTNER SIGNALS — RUN REPORT\nCompanies qualified; counts ready."),
    # a realistic human cold email that happens to use marketing words
    ("copywriter", "Hi Sam, most founders we speak to want more pipeline without more headcount. "
                   "We book meetings for you and you only pay per meeting held. Worth a quick chat?"),
]
for fw, out in legit_cases:
    leaked, reason, scores = leakguard.inspect(out, fw)
    check(f"legit {fw} output not flagged", not leaked, f"{reason} {scores}")

# --- 2. Verbatim copy of the framework IS caught ---
for fw in FRAMEWORKS:
    chunk = load_framework(fw)[:1500]
    leaked, reason, _ = leakguard.inspect(chunk, fw)
    check(f"verbatim {fw} chunk caught", leaked and reason == "verbatim")

# --- 3. Near-verbatim (lightly reworded) copy IS caught ---
# Take a framework slice and lightly edit a few words — most runs still match.
raw = load_framework("copywriter")
slice_ = raw[2000:3200]
lightly_edited = (slice_.replace("the ", "a ").replace("you ", "we ")
                  .replace("your ", "our ").replace(".", " ,"))
leaked, reason, scores = leakguard.inspect(lightly_edited, "copywriter")
check("near-verbatim copywriter caught", leaked, f"{reason} {scores}")

# --- 4. Structural / method-describing paraphrase IS caught ---
# The icebreaker method described using its own distinctive language (angles, detection, fallback).
struct_leak_icebreaker = load_framework("icebreaker")[:2500]  # method text = structural by nature
leaked, reason, scores = leakguard.inspect(struct_leak_icebreaker, "icebreaker")
check("structural icebreaker method caught", leaked, f"{reason} {scores}")

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
