"""Leak-guard unit tests. The filter must catch a WHOLESALE verbatim dump of a framework
while NEVER flagging finished work — even output that reuses the phrases the framework mandates
(the real-world false-positive that refused legitimate copy in production)."""
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
    results.append(ok)
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))


# 1. Legitimate finished outputs are NEVER flagged.
legit = [
    ("copywriter", mock_generate("copywriter", json.dumps(
        {"problem": "slow PR", "outcome": "coverage in 30 days", "risk_reversal": "pay on results",
         "service": "done-for-you PR"}))),
    ("icebreaker", mock_generate("icebreaker", json.dumps({"prospects": [
        {"name": "Jane", "company": "BrightSEO", "page_text": "We do SEO and content."},
        {"name": "Ravi", "company": "Partnerly", "page_text": "Become a partner today."}]}))),
    ("list_building", json.dumps({"companies_matching": 4200, "decision_makers_matching": 3000})),
    ("partner_signals", "PARTNER SIGNALS — RUN REPORT\nCompanies qualified; counts ready."),
]
for fw, out in legit:
    leaked, reason, scores = leakguard.inspect(out, fw)
    check(f"legit {fw} output not flagged", not leaked, f"{reason} {scores}")

# 2. THE regression: a real cold email that reuses the framework's mandated phrasing is SAFE.
mandated_email = (
    "Hi Sam, quick one — most agencies we speak to are stuck with unpredictable lead flow. "
    "Supposing we could fix that with done-for-you appointment setting, with you only paying "
    "per meeting that actually happens, would you be open to a quick call next week?\n\n"
    "Alternatively, if my last message wasn't relevant, I put together a short teardown of your "
    "current outbound that might be useful either way — happy to send it over.\n\n"
    "Was any of this relevant?")
leaked, reason, scores = leakguard.inspect(mandated_email, "copywriter")
check("cold email reusing mandated phrases is NOT flagged", not leaked, f"{reason} {scores}")

# 3. A wholesale verbatim dump of the framework IS caught.
for fw in FRAMEWORKS:
    chunk = load_framework(fw)[:2500]
    leaked, reason, scores = leakguard.inspect(chunk, fw)
    check(f"verbatim {fw} dump caught", leaked and reason == "verbatim_dump", f"{scores}")

# 4. A lightly-reworded but still-mostly-lifted dump is caught (ratio stays high).
raw = load_framework("copywriter")[2000:4000]
lightly = raw.replace("the ", "a ").replace(".", " ,")
leaked, reason, scores = leakguard.inspect(lightly, "copywriter")
check("lightly-edited dump still caught", leaked, f"{reason} {scores}")

failed = [i for i, ok in enumerate(results) if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
