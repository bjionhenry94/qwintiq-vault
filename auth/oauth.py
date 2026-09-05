"""The Wall — a spec-minimal OAuth 2.1 authorization server + the per-call MCP gate.

Flow (what Claude Code does automatically when a consultant adds the connector):
  401 on /mcp -> /.well-known discovery -> dynamic client registration -> /authorize
  (the login screen: email + password) -> code + PKCE -> /token -> bearer token.

The token is opaque and stored hashed. EVERY /mcp call re-checks token + consultant
status + key row (dal.check_token) — revoking a key in the panel kills the live
session on the very next call. The consultant never sees the key itself.
"""
from __future__ import annotations

import base64
import hashlib
import html
import json
import secrets
from urllib.parse import urlencode

from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from auth import ratelimit
from auth.context import set_consultant
from auth.passwords import hash_password, verify_password
from db import dal

SESSION_DAYS = 30  # "same amount of time a normal Google sign-in session would last"


def _issuer(request: Request) -> str:
    """Absolute base URL a connector will bind to. Behind Render's TLS-terminating proxy the
    app sees http + x-forwarded-proto: https, so derive the scheme from the forwarded header
    (defence-in-depth alongside uvicorn's proxy-header trust) — otherwise every discovery URL
    is emitted as http:// and the connector handshake fails."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


async def protected_resource_metadata(request: Request):
    iss = _issuer(request)
    return JSONResponse({"resource": iss + "/mcp", "authorization_servers": [iss],
                         "bearer_methods_supported": ["header"]})


async def as_metadata(request: Request):
    iss = _issuer(request)
    return JSONResponse({
        "issuer": iss,
        "authorization_endpoint": iss + "/authorize",
        "token_endpoint": iss + "/token",
        "registration_endpoint": iss + "/register",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
    })


async def register(request: Request):
    allowed, retry = ratelimit.check(f"register:{ratelimit.client_ip(request)}",
                                     limit=20, window=300, lockout=300)
    if not allowed:
        return JSONResponse({"error": "rate_limited"}, status_code=429,
                            headers={"retry-after": str(retry)})
    body = await request.json()
    client_id = "qv-" + secrets.token_hex(12)
    dal.save_client(client_id, body.get("client_name", "MCP client"),
                    body.get("redirect_uris", []))
    return JSONResponse({"client_id": client_id,
                         "redirect_uris": body.get("redirect_uris", []),
                         "token_endpoint_auth_method": "none"}, status_code=201)


_LOGIN_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/jpeg" href="/static/favicon.jpeg">
<title>Sign in — QwintiQ</title><style>{css}</style></head><body>
<main class="card">
  <img class="logo" src="/static/logo.png" alt="QwintiQ Consulting">
  <h1>Sign in</h1>
  <p class="sub"><b>{client}</b> is asking to connect to QwintiQ. Use the email and password QwintiQ gave you.</p>
  {error}
  <form method="post" action="/authorize">
    {hidden}
    <label>Email<input type="email" name="email" required autofocus autocomplete="username"></label>
    <label>Password<input type="password" name="password" required autocomplete="current-password"></label>
    <button type="submit">Sign in</button>
  </form>
  <p class="foot">Locked out? Ask your QwintiQ admin — access is managed centrally.</p>
</main></body></html>"""

_SETPW_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<link rel="icon" type="image/jpeg" href="/static/favicon.jpeg">
<title>Set your password — QwintiQ</title><style>{css}</style></head><body>
<main class="card">
  <img class="logo" src="/static/logo.png" alt="QwintiQ Consulting">
  <h1>Set your password</h1>
  <p class="sub">First time in — choose a password you'll use from now on. Your temporary
  one won't work again.</p>
  {error}
  <form method="post" action="/authorize">
    {hidden}
    <label>New password<input type="password" name="new_password" required autofocus
      minlength="10" autocomplete="new-password"></label>
    <label>Confirm password<input type="password" name="confirm_password" required
      minlength="10" autocomplete="new-password"></label>
    <button type="submit">Set password &amp; continue</button>
  </form>
  <p class="foot">At least 10 characters.</p>
</main></body></html>"""

CSS = """
:root{color-scheme:light dark;
--round:ui-rounded,"SF Pro Rounded","Poppins","Segoe UI",system-ui,-apple-system,sans-serif;
--paper:#f7f3f0;--card:#ffffff;--ink:#241531;--ink-soft:#6f6678;
--brand:#341948;--brand-hover:#241033;--purple:#8155ba;--cream:#e6ded3;
--line:#eae3dc;--bad:#b3261e;--bad-bg:#fbeceb}
@media(prefers-color-scheme:dark){:root{
--paper:#160e1f;--card:#241531;--ink:#efeaf3;--ink-soft:#b0a6bd;
--brand:#7b4fae;--brand-hover:#8a5cc0;--purple:#b98fe0;--cream:#2c2138;
--line:#3a2b49;--bad:#f0938c;--bad-bg:#341620}}
:root[data-theme="light"]{--paper:#f7f3f0;--card:#fff;--ink:#241531;--ink-soft:#6f6678;
--brand:#341948;--brand-hover:#241033;--purple:#8155ba;--cream:#e6ded3;--line:#eae3dc;--bad:#b3261e;--bad-bg:#fbeceb}
:root[data-theme="dark"]{--paper:#160e1f;--card:#241531;--ink:#efeaf3;--ink-soft:#b0a6bd;
--brand:#7b4fae;--brand-hover:#8a5cc0;--purple:#b98fe0;--cream:#2c2138;--line:#3a2b49;--bad:#f0938c;--bad-bg:#341620}
*{box-sizing:border-box;margin:0}
body{font-family:var(--round);min-height:100vh;display:flex;align-items:center;justify-content:center;
background:var(--paper);color:var(--ink);
background-image:radial-gradient(circle at 50% -12%, color-mix(in srgb,var(--purple) 16%,transparent), transparent 60%)}
.card{background:var(--card);border-radius:20px;padding:44px 38px;max-width:410px;width:92%;
border-top:4px solid var(--brand);box-shadow:0 10px 40px rgba(52,25,72,.12)}
@media(prefers-color-scheme:dark){.card{box-shadow:0 10px 40px rgba(0,0,0,.5)}}
.logo{width:210px;max-width:70%;height:auto;display:block;margin:0 auto 16px}
h1{font-size:25px;font-weight:700;margin-bottom:6px;letter-spacing:-.01em}
.sub{color:var(--ink-soft);font-size:14.5px;margin-bottom:24px;line-height:1.5}
label{display:block;font-size:13px;font-weight:600;margin-bottom:15px;color:var(--ink)}
input{display:block;width:100%;margin-top:7px;padding:12px 13px;font-size:15px;font-family:var(--round);
border:1.5px solid var(--line);border-radius:11px;background:transparent;color:inherit}
input:focus{outline:none;border-color:var(--purple);box-shadow:0 0 0 3px color-mix(in srgb,var(--purple) 22%,transparent)}
button{width:100%;padding:13px;margin-top:8px;font-size:15px;font-weight:650;font-family:var(--round);
border:0;border-radius:11px;background:var(--brand);color:#fff;cursor:pointer;transition:background .15s}
button:hover{background:var(--brand-hover)}
.error{background:var(--bad-bg);color:var(--bad);border-radius:10px;padding:11px 13px;font-size:14px;margin-bottom:16px}
.foot{margin-top:22px;font-size:12.5px;color:var(--ink-soft)}
"""


_OAUTH_FIELDS = ("client_id", "redirect_uri", "state", "code_challenge",
                 "code_challenge_method", "response_type", "scope", "resource")


def _hidden(params: dict, extra: tuple = ()) -> str:
    # html.escape(quote=True) so attacker-influenced query params can't break out of the
    # value="" attribute (reflected-XSS defence on the sign-in page).
    return "".join(
        f'<input type="hidden" name="{k}" value="{html.escape(str(params.get(k, "")), quote=True)}">'
        for k in _OAUTH_FIELDS + extra
    )


def _client_name(params: dict) -> str:
    """The registered client's name, shown on the sign-in page so a consultant can see WHO is
    asking for their QwintiQ session (a phished link from an unknown client stands out)."""
    client = dal.get_client(params.get("client_id", "") or "")
    return (client or {}).get("name") or "An unregistered app"


def _login_html(params: dict, error: str = "") -> str:
    err = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return _LOGIN_PAGE.format(css=CSS, hidden=_hidden(params), error=err,
                              client=html.escape(_client_name(params)))


def _pkce_problem(params: dict) -> str:
    """PKCE (S256) is REQUIRED, not optional: it is what binds the code to the client that started
    the flow. Without it an intercepted code could be exchanged by anyone. Empty '' if fine."""
    if not params.get("code_challenge"):
        return "This sign-in link is missing its security check (PKCE). Re-add the QwintiQ connector and try again."
    if (params.get("code_challenge_method") or "S256").upper() != "S256":
        return "This sign-in link uses an unsupported security method. Re-add the QwintiQ connector and try again."
    return ""


def _setpw_html(params: dict, error: str = "") -> str:
    # carry the oauth params + the identity + the temp password forward through the set step
    err = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return _SETPW_PAGE.format(css=CSS, hidden=_hidden(params, ("email", "password")), error=err)


def _issue_code(request: Request, params: dict, redirect_uri: str, consultant_id: str):
    code = secrets.token_urlsafe(32)
    dal.save_code(code, params["client_id"], redirect_uri,
                  params.get("code_challenge", ""), consultant_id)
    sep = "&" if "?" in redirect_uri else "?"
    return RedirectResponse(
        redirect_uri + sep + urlencode({"code": code, "state": params.get("state", "")}),
        status_code=302)


async def authorize(request: Request):
    if request.method == "GET":
        params = dict(request.query_params)
        bad = _pkce_problem(params)
        if bad:
            return HTMLResponse(_login_html(params, bad), status_code=400)
        return HTMLResponse(_login_html(params))
    form = await request.form()
    params = {k: str(form.get(k, "")) for k in form}
    client = dal.get_client(params.get("client_id", ""))
    redirect_uri = params.get("redirect_uri", "")
    if not client or redirect_uri not in client["redirect_uris"]:
        return HTMLResponse(_login_html(params, "This sign-in link is not valid. "
                                        "Re-add the QwintiQ connector and try again."), status_code=400)
    bad = _pkce_problem(params)
    if bad:
        return HTMLResponse(_login_html(params, bad), status_code=400)

    email = params.get("email", "").lower().strip()
    ip = ratelimit.client_ip(request)
    allowed, retry = ratelimit.check(f"authorize:{ip}:{email}", limit=8, window=300, lockout=900)
    if not allowed:
        return HTMLResponse(_login_html(params, f"Too many attempts. Try again in about "
                                        f"{retry // 60 + 1} minutes."), status_code=429)

    consultant = dal.get_consultant_by_email(email)
    if (not consultant or consultant["status"] != "active"
            or not verify_password(params.get("password", ""), consultant["password_hash"] or "")):
        return HTMLResponse(_login_html(params, "Wrong email or password — or your access "
                                        "has been switched off."), status_code=401)
    if not dal.active_key(consultant["id"]):
        return HTMLResponse(_login_html(params, "Your account exists but no access key is "
                                        "assigned. Ask your QwintiQ admin to assign one."), status_code=403)

    # First sign-in: force the consultant to replace the one-time temp password before any
    # token is issued, so the temp password is never a standing credential.
    if consultant["must_change_password"]:
        new_pw = params.get("new_password", "")
        if not new_pw:
            return HTMLResponse(_setpw_html(params))  # they've verified; now show the set-password step
        if new_pw != params.get("confirm_password", ""):
            return HTMLResponse(_setpw_html(params, "The two passwords don't match."))
        if len(new_pw) < 10:
            return HTMLResponse(_setpw_html(params, "Use at least 10 characters."))
        dal.set_password(consultant["id"], hash_password(new_pw))

    ratelimit.clear(f"authorize:{ip}:{email}")
    return _issue_code(request, params, redirect_uri, consultant["id"])


async def token(request: Request):
    form = await request.form()
    if form.get("grant_type") != "authorization_code":
        return JSONResponse({"error": "unsupported_grant_type"}, status_code=400)
    row = dal.take_code(str(form.get("code", "")))
    if not row:
        return JSONResponse({"error": "invalid_grant"}, status_code=400)
    # The code is bound to the client (and redirect) that started the flow. A public client
    # identifies itself in the token request; if it does, it must be the same one.
    client_id = str(form.get("client_id", "") or "")
    if client_id and client_id != row["client_id"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "client mismatch"},
                            status_code=400)
    redirect_uri = str(form.get("redirect_uri", "") or "")
    if redirect_uri and redirect_uri != row["redirect_uri"]:
        return JSONResponse({"error": "invalid_grant", "error_description": "redirect mismatch"},
                            status_code=400)
    verifier = str(form.get("code_verifier", ""))
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    if not row["code_challenge"] or expected != row["code_challenge"]:  # PKCE is mandatory
        return JSONResponse({"error": "invalid_grant", "error_description": "PKCE failed"},
                            status_code=400)
    key = dal.active_key(row["consultant_id"])
    if not key:
        return JSONResponse({"error": "invalid_grant", "error_description": "no active key"},
                            status_code=403)
    access = "qv_" + secrets.token_urlsafe(40)
    dal.save_token(_hash(access), row["consultant_id"], key["id"], days=SESSION_DAYS)
    return JSONResponse({"access_token": access, "token_type": "Bearer",
                         "expires_in": SESSION_DAYS * 86400})


class AuthGate:
    """ASGI wrapper around the MCP app: no valid bearer -> 401 + discovery pointer.
    Runs on EVERY call, so a revoked key kills a live session mid-sentence."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        headers = {k.decode().lower(): v.decode() for k, v in scope.get("headers", [])}
        authz = headers.get("authorization", "")
        identity = None
        if authz.lower().startswith("bearer "):
            identity = dal.check_token(_hash(authz[7:].strip()))
        if not identity:
            host = headers.get("host", "localhost")
            proto = headers.get("x-forwarded-proto", "http")
            www = (f'Bearer resource_metadata='
                   f'"{proto}://{host}/.well-known/oauth-protected-resource"')
            body = json.dumps({"error": "unauthorized",
                               "error_description": "Sign in to the QwintiQ vault to use it."}).encode()
            await send({"type": "http.response.start", "status": 401,
                        "headers": [(b"content-type", b"application/json"),
                                    (b"www-authenticate", www.encode())]})
            await send({"type": "http.response.body", "body": body})
            return
        set_consultant(identity["consultant"])
        try:
            await self.app(scope, receive, send)
        finally:
            set_consultant(None)
