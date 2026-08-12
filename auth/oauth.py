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
<title>Sign in — Qwintiq</title><style>{css}</style></head><body>
<main class="card">
  <div class="brand">Qwintiq</div>
  <h1>Sign in</h1>
  <p class="sub">Use the email and password Qwintiq gave you.</p>
  {error}
  <form method="post" action="/authorize">
    {hidden}
    <label>Email<input type="email" name="email" required autofocus autocomplete="username"></label>
    <label>Password<input type="password" name="password" required autocomplete="current-password"></label>
    <button type="submit">Sign in</button>
  </form>
  <p class="foot">Locked out? Ask your Qwintiq admin — access is managed centrally.</p>
</main></body></html>"""

_SETPW_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Set your password — Qwintiq</title><style>{css}</style></head><body>
<main class="card">
  <div class="brand">Qwintiq</div>
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
:root{color-scheme:light dark}*{box-sizing:border-box;margin:0}
body{font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',sans-serif;min-height:100vh;
display:flex;align-items:center;justify-content:center;background:#f4f5f7;color:#111}
@media(prefers-color-scheme:dark){body{background:#101216;color:#eee}}
.card{background:#fff;border-radius:14px;padding:40px 36px;max-width:400px;width:92%;
box-shadow:0 8px 30px rgba(0,0,0,.08)}
@media(prefers-color-scheme:dark){.card{background:#1a1d23;box-shadow:0 8px 30px rgba(0,0,0,.5)}}
.brand{font-weight:700;letter-spacing:.14em;text-transform:uppercase;font-size:13px;color:#c07b28;margin-bottom:18px}
h1{font-size:24px;margin-bottom:6px}.sub{color:#777;font-size:14px;margin-bottom:22px}
label{display:block;font-size:13px;font-weight:600;margin-bottom:14px;color:#555}
@media(prefers-color-scheme:dark){label{color:#aaa}}
input{display:block;width:100%;margin-top:6px;padding:11px 12px;font-size:15px;border:1px solid #d5d8de;
border-radius:8px;background:transparent;color:inherit}
input:focus{outline:2px solid #c07b28;border-color:transparent}
button{width:100%;padding:12px;margin-top:6px;font-size:15px;font-weight:600;border:0;border-radius:8px;
background:#c07b28;color:#fff;cursor:pointer}button:hover{background:#a96a1f}
.error{background:#fdecec;color:#b3261e;border-radius:8px;padding:10px 12px;font-size:14px;margin-bottom:16px}
.foot{margin-top:20px;font-size:12.5px;color:#999}
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


def _login_html(params: dict, error: str = "") -> str:
    err = f'<div class="error">{html.escape(error)}</div>' if error else ""
    return _LOGIN_PAGE.format(css=CSS, hidden=_hidden(params), error=err)


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
        return HTMLResponse(_login_html(dict(request.query_params)))
    form = await request.form()
    params = {k: str(form.get(k, "")) for k in form}
    client = dal.get_client(params.get("client_id", ""))
    redirect_uri = params.get("redirect_uri", "")
    if not client or redirect_uri not in client["redirect_uris"]:
        return HTMLResponse(_login_html(params, "This sign-in link is not valid. "
                                        "Re-add the Qwintiq connector and try again."), status_code=400)

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
                                        "assigned. Ask your Qwintiq admin to assign one."), status_code=403)

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
    verifier = str(form.get("code_verifier", ""))
    expected = base64.urlsafe_b64encode(
        hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    if row["code_challenge"] and expected != row["code_challenge"]:
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
                               "error_description": "Sign in to the Qwintiq vault to use it."}).encode()
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
