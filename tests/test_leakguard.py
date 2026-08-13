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

# 3. THE production regression: real copy that echoes ~30% of the framework's scaffolding/
# labels (as a live OpenAI call did — ratio 0.319) must PASS. Build ~30% framework, ~70% fresh.
fw_words = load_framework("copywriter").split()
echoed = " ".join(fw_words[400:460])   # ~60 words of framework scaffolding/labels
fresh = ("Hi Sam, here is your outreach. Most founders we speak to are stuck with a lead "
         "engine that sputters. We book the meetings for you and you only pay when one actually "
         "happens, so there is no risk on your side. If that sounds useful I can show you how it "
         "works on a quick call this week, no slides, just the plan. Reply and I will send times. "
         "One more thing, we can start small with a two week pilot so you can see it before you "
         "commit to anything bigger than that. Talk soon and thanks for reading this far today. ") * 1
mixed = echoed + " " + fresh
leaked, reason, scores = leakguard.inspect(mixed, "copywriter")
check("real copy echoing ~30% framework scaffolding is NOT flagged", not leaked, f"{reason} {scores}")

# 4. A wholesale verbatim dump of the framework IS still caught.
for fw in FRAMEWORKS:
    chunk = load_framework(fw)[:3500]
    leaked, reason, scores = leakguard.inspect(chunk, fw)
    check(f"verbatim {fw} dump caught", leaked and reason == "verbatim_dump", f"{scores}")

failed = [i for i, ok in enumerate(results) if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
sys.exit(1 if failed else 0)
