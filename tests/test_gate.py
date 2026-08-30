"""End-to-end gate test against a live local vault.

Proves, in order:
  1. Unauthenticated MCP call -> 401 with discovery pointer (Step 3 done-rule).
  2. Full OAuth flow (register -> authorize/login -> code+PKCE -> token) -> tools work.
  3. Session persists across calls without re-login.
  4. Revoking the key in the Keyring kills the LIVE session on the next call (Step 4).
  5. A consultant with no active key can't log in.

Run:  VAULT_ENV=dev VAULT_LLM=mock VAULT_AIARK=mock python tests/test_gate.py <base_url>
Needs a consultant seeded; the harness creates one via the admin panel first.
"""
import asyncio
import base64
import hashlib
import json
import os
import re
import secrets
import sys

import httpx

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:8200"
# Admin creds default to the local dev seed; override via env to run against a real deploy.
ADMIN = (os.environ.get("ADMIN_EMAIL", "admin@qwintiq.local"),
         os.environ.get("ADMIN_PASSWORD", "qwintiq-admin-dev"))
CONSULTANT = ("test.consultant@example.com", "Test Consultant")

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(("PASS " if ok else "FAIL ") + name + (f" — {detail}" if detail and not ok else ""))


def admin_client():
    c = httpx.Client(base_url=BASE, follow_redirects=False)
    r = c.post("/admin/login", data={"email": ADMIN[0], "password": ADMIN[1]})
    assert r.status_code == 302, f"admin login failed: {r.status_code}"
    return c


def add_and_get_pw(c, email=None, name=None):
    """Add a consultant and recover their one-time password from the panel notice (it is no
    longer in the redirect URL — it lives server-side and renders once in the HTML)."""
    email = email or CONSULTANT[0]
    name = name or CONSULTANT[1]
    r = c.post("/admin/add", data={"email": email, "full_name": name}, follow_redirects=True)
    assert email in r.text, f"consultant not listed after add: {r.status_code}"
    m = re.search(r'<code id="pw">([^<]+)</code>', r.text)
    return m.group(1) if m else None


def setup_consultant(c):
    return add_and_get_pw(c)


NEW_PW = "consultant-new-pass-123"


def oauth_token(password, new_password=NEW_PW):
    """Full connector flow. A freshly-added consultant must set their own password on first
    sign-in (must_change_password), so the authorize POST returns the set-password page and we
    complete it before a code is issued."""
    c = httpx.Client(base_url=BASE, follow_redirects=False)
    reg = c.post("/register", json={"client_name": "test", "redirect_uris": ["http://localhost:1/cb"]})
    client_id = reg.json()["client_id"]
    verifier = secrets.token_urlsafe(40)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    params = {
        "client_id": client_id, "redirect_uri": "http://localhost:1/cb", "state": "s1",
        "code_challenge": challenge, "code_challenge_method": "S256", "response_type": "code",
        "email": CONSULTANT[0], "password": password}
    r = c.post("/authorize", data=params)
    if r.status_code == 200 and "Set your password" in r.text:
        r = c.post("/authorize", data={**params, "new_password": new_password,
                                       "confirm_password": new_password})
    assert r.status_code == 302, f"authorize: {r.status_code} {r.text[:200]}"
    code = re.search(r"code=([^&]+)", r.headers["location"]).group(1)
    tok = c.post("/token", data={"grant_type": "authorization_code", "code": code,
                                 "code_verifier": verifier})
    assert tok.status_code == 200, tok.text
    return tok.json()["access_token"]


async def mcp_call(token, tool, args):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    headers = {"Authorization": f"Bearer {token}"} if token else {}
    async with streamablehttp_client(BASE + "/mcp", headers=headers) as (r, w, _):
        async with ClientSession(r, w) as s:
            await s.initialize()
            res = await s.call_tool(tool, args)
            return res.content[0].text


def main():
    # 1. Unauthenticated -> 401 + WWW-Authenticate
    r = httpx.post(BASE + "/mcp", json={"jsonrpc": "2.0", "id": 1, "method": "initialize"})
    check("unauthenticated call refused (401)", r.status_code == 401)
    check("401 carries discovery pointer", "oauth-protected-resource" in r.headers.get("www-authenticate", ""))

    ac = admin_client()
    temp_pw = setup_consultant(ac)
    check("admin add consultant + temp password", bool(temp_pw))

    token = oauth_token(temp_pw)
    check("OAuth flow issues token", token.startswith("qv_"))

    out = asyncio.run(mcp_call(token, "ping", {}))
    check("authenticated tool call works", "online" in out)

    # Regression: behind a real proxy the Host is the public domain, not localhost. The MCP
    # server's DNS-rebinding protection must not 421 it (this only surfaces off-localhost).
    fh = httpx.post(BASE + "/mcp", headers={
        "Authorization": f"Bearer {token}", "Host": "vault.example.com",
        "Accept": "application/json, text/event-stream", "Content-Type": "application/json"},
        json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "t", "version": "1"}}}, timeout=30)
    check("foreign Host header accepted (no 421)", fh.status_code != 421, f"got {fh.status_code}")

    out = asyncio.run(mcp_call(token, "qwintiq_copywriter", {
        "problem": "slow PR cycles", "outcome": "coverage in 30 days",
        "risk_reversal": "pay on results", "service": "done-for-you PR"}))
    check("copywriter returns finished sequences", "EMAIL SEQUENCE" in out and "LINKEDIN" in out)

    out2 = asyncio.run(mcp_call(token, "qwintiq_list_count", {
        "what_you_sell": "PR retainers", "industry": "marketing services",
        "country": "United Kingdom", "size_min": 11, "size_max": 50,
        "roles": "founders and partnerships leads", "seniorities": ["founder", "owner"]}))
    data = json.loads(out2)
    check("list count returns totals, no export", data["companies_matching"] > 0 and "csv" not in out2)

    bad = asyncio.run(mcp_call(token, "qwintiq_list_export", {
        "kind": "decision_makers", "filters": {}, "max_rows": 500,
        "confirmation_phrase": "yes go ahead"}))
    check("export refused without typed phrase", "EXPORT REFUSED" in bad)

    wrongn = asyncio.run(mcp_call(token, "qwintiq_list_export", {
        "kind": "decision_makers", "filters": {}, "max_rows": 500,
        "confirmation_phrase": "I confirm to export this and use 200 amount of credits"}))
    check("export refused on number mismatch", "EXPORT REFUSED" in wrongn)

    good = asyncio.run(mcp_call(token, "qwintiq_list_export", {
        "kind": "decision_makers", "filters": {"titles": ["Founder"]}, "max_rows": 10,
        "confirmation_phrase": "I confirm to export this and use 10 amount of credits"}))
    check("export runs with valid phrase", "csv" in good and "full_name" in good)

    # Session persists (second call, same token, no re-login)
    out = asyncio.run(mcp_call(token, "ping", {}))
    check("session persists across calls", "online" in out)

    # Partner-signals stateful gate (regression: the count used to drift and a dropped candidate
    # list used to run on nothing — the gate now hands back a one-time token that carries both).
    cands = [{"name": "A Co", "website": "a.com", "what_happened": "launched a partner program"},
             {"name": "B Co", "website": "b.com", "what_happened": "hired a Head of Partnerships"}]
    rlist = json.loads(asyncio.run(mcp_call(token, "qwintiq_routine_list", {})))
    names = rlist if isinstance(rlist, list) else rlist.get("routines", [])
    rname = (names[0]["name"] if names and isinstance(names[0], dict) else
             (names[0] if names else "Partner and PR signals"))
    g = json.loads(asyncio.run(mcp_call(token, "qwintiq_partner_signals",
                                        {"routine_name": rname, "candidate_companies": cands})))
    check("signals gate returns a one-time token + count", bool(g.get("gate_token")) and g.get("estimated_people") is not None)
    ph = f"I confirm to export this and use {g.get('estimated_people')} amount of credits"
    proceed = asyncio.run(mcp_call(token, "qwintiq_partner_signals",
                                   {"routine_name": rname, "confirmation_phrase": ph, "gate_token": g.get("gate_token")}))
    pj = json.loads(proceed)
    check("signals confirm via token pulls real decision-makers (not an LLM essay)",
          isinstance(pj.get("decision_makers"), list) and pj.get("rows", 0) >= 1)
    reuse = asyncio.run(mcp_call(token, "qwintiq_partner_signals",
                                 {"routine_name": rname, "confirmation_phrase": ph, "gate_token": g.get("gate_token")}))
    check("signals token is one-time", "expired" in reuse.lower())
    lost = asyncio.run(mcp_call(token, "qwintiq_partner_signals",
                                {"routine_name": rname, "confirmation_phrase": ph}))
    check("signals phrase without shortlist refuses (no empty run)", "Missing the shortlist" in lost)

    # Real time-based TTL expiry (distinct from one-time-use): a fresh token left to age past the
    # TTL is rejected. Needs the server started with a short SIGNAL_GATE_TTL_S.
    ttl = float(os.environ.get("SIGNAL_GATE_TTL_S", "0") or 0)
    if 0 < ttl <= 10:
        import time as _time
        g2 = json.loads(asyncio.run(mcp_call(token, "qwintiq_partner_signals",
                                             {"routine_name": rname, "candidate_companies": cands})))
        _time.sleep(ttl + 1.5)
        ph2 = f"I confirm to export this and use {g2.get('estimated_people')} amount of credits"
        aged = asyncio.run(mcp_call(token, "qwintiq_partner_signals",
                                    {"routine_name": rname, "confirmation_phrase": ph2, "gate_token": g2.get("gate_token")}))
        check("signals token expires after its TTL", "expired" in aged.lower())

    # Enrichment runs through the real MCP transport (regression for the async-tool fix).
    en = json.loads(asyncio.run(mcp_call(token, "qwintiq_enrich", {
        "people": [{"full_name": "Jane Doe", "company_domain": "acme.com"}],
        "confirmation_phrase": "I confirm to export this and use 1 amount of credits"})))
    check("enrich async tool works via MCP", en.get("total") == 1 and isinstance(en.get("people"), list))

    # 4. Revoke key -> live session dies on next call
    consultants = json.loads(re.search(r"", "") or "null") if False else None
    page = ac.get("/admin", follow_redirects=True).text
    cid = re.search(r'name="id" value="([0-9a-f-]{36})"', page).group(1)
    ac.post("/admin/revoke-key", data={"id": cid})
    try:
        asyncio.run(mcp_call(token, "ping", {}))
        check("revoke kills live session", False, "call still succeeded after revoke")
    except Exception:
        check("revoke kills live session", True)

    # 5. No active key -> login refused
    r = httpx.Client(base_url=BASE).post("/register", json={"client_name": "t2", "redirect_uris": ["http://localhost:1/cb"]})
    try:
        oauth_token(temp_pw)
        check("login refused when key revoked", False)
    except AssertionError:
        check("login refused when key revoked", True)

    # restore for later steps
    ac.post("/admin/assign-key", data={"id": cid})

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
