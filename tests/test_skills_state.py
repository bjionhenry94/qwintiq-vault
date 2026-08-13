"""Skills + vault-state behaviour against a live local vault (run after test_gate.py,
which leaves the test consultant with an active key)."""
import asyncio
import json
import re
import sys

import httpx

from test_gate import (BASE, CONSULTANT, add_and_get_pw, admin_client, mcp_call,
                       oauth_token, results, check)


def fresh_token(ac):
    # a fresh temp password each run: remove (if present) then re-add
    page = ac.get("/admin", follow_redirects=True).text
    m = re.search(r'name="id" value="([0-9a-f-]{36})"', page)
    if m:
        ac.post("/admin/remove", data={"id": m.group(1)})
    pw = add_and_get_pw(ac)
    return oauth_token(pw)


def _cid(ac):
    page = ac.get("/admin", follow_redirects=True).text
    return re.search(r'name="id" value="([0-9a-f-]{36})"', page).group(1)


def main():
    ac = admin_client()
    token = fresh_token(ac)

    out = asyncio.run(mcp_call(token, "qwintiq_icebreaker", {"prospects": [
        {"name": "Jane", "company": "BrightSEO", "page_text": "We do SEO, paid media and content strategy for B2B brands."},
        {"name": "Ravi", "company": "Partnerly", "page_text": "Become a partner — join our referral program today."},
        {"name": "Ann", "company": "Quietco", "page_text": "We make furniture."}]}))
    check("icebreaker: services-gap + partner-invite angles fire", "PR" in out and "partner" in out.lower())
    check("icebreaker: no-hook prospect honestly flagged", "FLAGGED" in out)

    routines = json.loads(asyncio.run(mcp_call(token, "qwintiq_partner_signals", {})))
    check("partner routines listed from vault state (seeded)", len(routines.get("routines", [])) >= 2)

    # pick a seeded shared routine, not an autopilot artifact left by a prior run
    supervised = next(r for r in routines["routines"] if "AP test" not in r)
    run = asyncio.run(mcp_call(token, "qwintiq_partner_signals", {
        "routine_name": supervised,
        "candidate_companies": [{"name": "NewCo", "website": "newco.example",
                                 "what_happened": "raised Series A"}]}))
    check("partner signals: paid pull gated behind typed phrase",
          "amount of credits" in run and "gate" in run.lower())

    asyncio.run(mcp_call(token, "qwintiq_setup_save", {"name": "My test setup", "config": {"angles": {"ranked": []}, "recency_months": 3}}))
    setups = json.loads(asyncio.run(mcp_call(token, "qwintiq_setup_list", {})))
    names = [s["name"] for s in setups]
    check("setup round-trips through vault", "My test setup" in names)
    check("shared Qwintiq default setup visible", any(s["shared"] for s in setups))

    ap = asyncio.run(mcp_call(token, "qwintiq_routine_save", {"name": "AP test", "config": {"run_mode": "autopilot"}}))
    check("autopilot routine without credit cap refused", "REFUSED" in ap)

    ap2 = asyncio.run(mcp_call(token, "qwintiq_routine_save", {"name": "AP test 2", "config": {"mode": "autopilot"}}))
    check("autopilot via 'mode' synonym without cap also refused", "REFUSED" in ap2)

    ap3 = asyncio.run(mcp_call(token, "qwintiq_routine_save", {"name": "AP test 3", "config": {"mode": "Autopilot", "daily_credit_cap": 20}}))
    check("autopilot with cap saves (mode synonym, mixed case)", "saved" in ap3)

    copy = asyncio.run(mcp_call(token, "qwintiq_copywriter", {
        "problem": "referrals slowing", "outcome": "10 partner calls a month",
        "risk_reversal": "one-month pilot", "service": "outbound partnership prospecting"}))
    check("copywriter output carries the {{icebreaker}} merge slot", "{{icebreaker}}" in copy)

    # Remove consultant entirely -> everything dies
    ac.post("/admin/remove", data={"id": _cid(ac)})
    try:
        asyncio.run(mcp_call(token, "ping", {}))
        check("remove consultant kills access", False)
    except Exception:
        check("remove consultant kills access", True)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
