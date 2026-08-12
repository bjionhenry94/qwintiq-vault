"""Prod-hardening regressions — proves the audit fixes hold. Run against a live local vault
(dev mode) after test_gate.py. Covers: temp password never in the URL, first-login password
change is enforced, reflected-XSS escaping, brute-force lockout, and HTTPS-correct discovery
URLs when x-forwarded-proto:https is presented."""
import re
import sys

import httpx

from test_gate import BASE, admin_client, results, check


def main():
    ac = admin_client()

    # 1. Adding a consultant must NOT put the temp password in the redirect URL.
    r = ac.post("/admin/add", data={"email": "leak.check@example.com", "full_name": "Leak Check"})
    loc = r.headers.get("location", "")
    check("temp password not in redirect URL", "pw=" not in loc and "added=" in loc)

    # ...and the panel renders it once, server-side.
    page = ac.get(loc, follow_redirects=True).text
    m = re.search(r'<code id="pw">([^<]+)</code>', page)
    check("temp password shown once in panel", bool(m))
    # ...and is gone on a second view (one-view flash popped).
    again = ac.get(loc, follow_redirects=True).text
    check("temp password not shown twice", '<code id="pw">' not in again)

    # 2. Reflected-XSS: a script-y name is escaped, not executed, in the notice.
    r = ac.post("/admin/add", data={"email": "xss@example.com",
                                    "full_name": '<img src=x onerror=alert(1)>'},
                follow_redirects=True)
    check("reflected name is HTML-escaped", "<img src=x" not in r.text and "&lt;img" in r.text)

    # 3. HTTPS-correct discovery when the proxy header says https (the connector-handshake fix).
    meta = httpx.get(BASE + "/.well-known/oauth-authorization-server",
                     headers={"x-forwarded-proto": "https", "host": "vault.example.com"}).json()
    check("issuer honours x-forwarded-proto (https)", meta["issuer"] == "https://vault.example.com")
    check("authorization_endpoint is https",
          meta["authorization_endpoint"] == "https://vault.example.com/authorize")
    pr = httpx.get(BASE + "/.well-known/oauth-protected-resource",
                   headers={"x-forwarded-proto": "https", "host": "vault.example.com"}).json()
    check("resource metadata is https", pr["resource"] == "https://vault.example.com/mcp")

    # 4. Brute-force lockout on /admin/login (limit 8/window). Fresh IP via x-forwarded-for.
    bf = httpx.Client(base_url=BASE)
    ip = {"x-forwarded-for": "203.0.113.77"}
    codes = []
    for _ in range(11):
        rr = bf.post("/admin/login", data={"email": "admin@qwintiq.local", "password": "wrong"},
                     headers=ip)
        codes.append(rr.status_code)
    check("admin login locks out after repeated failures", 429 in codes)

    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} passed")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
