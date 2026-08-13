"""Control panel — SOW §3: exactly two jobs, done properly, and nothing else.

1. Add and remove consultants.
2. Assign and revoke keys.

No usage dashboard (SOW §5 exclusion). Server-rendered, cookie session for admins.
Adding a consultant shows a one-time temporary password for Aliyah to pass on; the
access KEY itself is a server-side row the consultant never sees.
"""
from __future__ import annotations

import hashlib
import hmac
import html
import os
import secrets
import time

from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse

from auth import ratelimit
from auth.passwords import hash_password, temp_password, verify_password
from db import dal

# Fail closed: never sign admin sessions with the source-visible dev default in production.
_SECRET = os.environ.get("SECRET_KEY", "")
if not _SECRET:
    if dal.is_prod():
        raise RuntimeError("SECRET_KEY is required in production.")
    _SECRET = "dev-secret-change-in-prod"

# One-view flash store for the just-created consultant's temp password. Kept server-side and
# popped on first render, so the secret NEVER travels in a URL/query string or a log line.
_FLASH: dict[str, dict] = {}


def _secure_cookies(request: Request) -> bool:
    return dal.is_prod() or request.headers.get("x-forwarded-proto", "").startswith("https")


def _base_url(request: Request) -> str:
    """Absolute base URL, HTTPS-correct behind Render's TLS-terminating proxy."""
    proto = request.headers.get("x-forwarded-proto", request.url.scheme).split(",")[0].strip()
    host = request.headers.get("host", request.url.netloc)
    return f"{proto}://{host}"


def _sign(admin_id: str, exp: int) -> str:
    raw = f"{admin_id}.{exp}"
    sig = hmac.new(_SECRET.encode(), raw.encode(), hashlib.sha256).hexdigest()[:32]
    return f"{raw}.{sig}"


def _check_cookie(value: str) -> str | None:
    try:
        admin_id, exp, sig = value.rsplit(".", 2)
        if (hmac.compare_digest(sig, _sign(admin_id, int(exp)).rsplit(".", 1)[1])
                and int(exp) > time.time()
                and dal.q("select id from admins where id=?", (admin_id,), fetch="one")):
            return admin_id
    except Exception:
        pass
    return None


def _admin(request: Request) -> str | None:
    return _check_cookie(request.cookies.get("qv_admin", ""))


CSS = """
:root{color-scheme:light dark;
--round:ui-rounded,"SF Pro Rounded","Poppins","Segoe UI",system-ui,-apple-system,sans-serif;
--mono:ui-monospace,SFMono-Regular,Menlo,Consolas,monospace;
--paper:#f7f3f0;--card:#ffffff;--ink:#241531;--ink-soft:#6f6678;
--brand:#341948;--brand-hover:#241033;--header:#341948;--purple:#8155ba;--cream:#efe8df;
--line:#eae3dc;--ok:#1c7a4f;--ok-bg:#e3f3ea;--bad:#b3261e;--bad-bg:#fbeceb}
@media(prefers-color-scheme:dark){:root{
--paper:#160e1f;--card:#241531;--ink:#efeaf3;--ink-soft:#b0a6bd;
--brand:#7b4fae;--brand-hover:#8a5cc0;--header:#1f1330;--purple:#b98fe0;--cream:#2c2138;
--line:#3a2b49;--ok:#57c48c;--ok-bg:#13291f;--bad:#f0938c;--bad-bg:#341620}}
:root[data-theme="light"]{--paper:#f7f3f0;--card:#fff;--ink:#241531;--ink-soft:#6f6678;
--brand:#341948;--brand-hover:#241033;--header:#341948;--purple:#8155ba;--cream:#efe8df;--line:#eae3dc;
--ok:#1c7a4f;--ok-bg:#e3f3ea;--bad:#b3261e;--bad-bg:#fbeceb}
:root[data-theme="dark"]{--paper:#160e1f;--card:#241531;--ink:#efeaf3;--ink-soft:#b0a6bd;
--brand:#7b4fae;--brand-hover:#8a5cc0;--header:#1f1330;--purple:#b98fe0;--cream:#2c2138;--line:#3a2b49;
--ok:#57c48c;--ok-bg:#13291f;--bad:#f0938c;--bad-bg:#341620}
*{box-sizing:border-box;margin:0}
body{font-family:var(--round);background:var(--paper);color:var(--ink);min-height:100vh}
header{background:var(--header);color:#fff;padding:17px 28px;display:flex;align-items:center;justify-content:space-between}
header .brand{font-weight:700;letter-spacing:.01em;font-size:19px;color:#fff}
header .brand::after{content:"";display:inline-block;width:6px;height:6px;border-radius:50%;background:var(--purple);margin-left:2px}
header a{color:rgba(255,255,255,.72);font-size:13px;text-decoration:none;font-weight:600}
header a:hover{color:#fff}
.navlinks{display:flex;gap:20px;align-items:center}
.keyrow{display:flex;gap:12px;align-items:flex-start;flex-wrap:wrap;padding:16px 0;border-top:1px solid var(--line)}
.keyrow:first-of-type{border-top:0}
.keyrow .meta{flex:1;min-width:220px}.keyrow h3{font-size:15px;font-weight:650;margin-bottom:2px}
.keyrow .desc{color:var(--ink-soft);font-size:13px;line-height:1.5}
.keyrow form{display:flex;gap:8px;align-items:center;flex-wrap:wrap}
.keyrow input{min-width:220px}
.stat{font-family:var(--mono);font-size:11px;font-weight:600;letter-spacing:.04em;text-transform:uppercase;padding:4px 10px;border-radius:99px;white-space:nowrap}
.stat.on{background:var(--ok-bg);color:var(--ok)}.stat.env{background:color-mix(in srgb,var(--purple) 12%,var(--card));color:var(--purple);border:1px solid color-mix(in srgb,var(--purple) 26%,var(--line))}
.stat.off{background:var(--cream);color:var(--ink-soft)}
main{max-width:820px;margin:38px auto;padding:0 20px}
h1{font-size:26px;font-weight:700;margin-bottom:4px;letter-spacing:-.01em}
.sub{color:var(--ink-soft);font-size:14.5px;margin-bottom:26px;line-height:1.5}
.card{background:var(--card);border-radius:18px;padding:26px 28px;margin-bottom:20px;
border:1px solid var(--line);box-shadow:0 6px 24px rgba(52,25,72,.07)}
@media(prefers-color-scheme:dark){.card{box-shadow:0 6px 24px rgba(0,0,0,.4)}}
.card h2{font-size:16.5px;font-weight:700;margin-bottom:14px}
table{width:100%;border-collapse:collapse;font-size:14.5px}
th{text-align:left;color:var(--ink-soft);font-weight:600;font-size:11.5px;text-transform:uppercase;letter-spacing:.06em;padding:8px 10px;border-bottom:1px solid var(--line)}
td{padding:13px 10px;border-bottom:1px solid var(--line);vertical-align:middle}
.pill{display:inline-block;padding:4px 11px;border-radius:99px;font-size:12px;font-weight:650}
.pill.on{background:var(--ok-bg);color:var(--ok)}.pill.off{background:var(--bad-bg);color:var(--bad)}
form.inline{display:inline}
button{padding:9px 15px;font-size:13.5px;font-weight:650;font-family:var(--round);border:0;border-radius:10px;cursor:pointer;transition:background .15s}
button.primary{background:var(--brand);color:#fff}button.primary:hover{background:var(--brand-hover)}
button.quiet{background:var(--cream);color:var(--ink)}button.quiet:hover{filter:brightness(.96)}
button.danger{background:var(--bad-bg);color:var(--bad)}button.danger:hover{filter:brightness(.97)}
input{padding:11px 13px;font-size:14.5px;font-family:var(--round);border:1.5px solid var(--line);border-radius:11px;background:transparent;color:inherit}
input:focus{outline:none;border-color:var(--purple);box-shadow:0 0 0 3px color-mix(in srgb,var(--purple) 22%,transparent)}
.addrow{display:flex;gap:10px;flex-wrap:wrap}.addrow input{flex:1;min-width:180px}
.notice{background:color-mix(in srgb,var(--purple) 8%,var(--card));border:1px solid color-mix(in srgb,var(--purple) 28%,var(--line));
border-radius:14px;padding:16px 18px;font-size:14px;margin-bottom:22px}
.notice code{font-size:15px;font-weight:700;font-family:var(--mono);background:color-mix(in srgb,var(--purple) 16%,transparent);padding:2px 9px;border-radius:6px;color:var(--ink)}
.notice-actions{margin-top:13px;display:flex;gap:10px;align-items:center;flex-wrap:wrap}
.linkbtn{font-size:13px;font-weight:650;color:var(--purple);text-decoration:none}.linkbtn:hover{text-decoration:underline}
.step{display:flex;gap:16px;margin-bottom:22px}
.step .n{flex:none;width:32px;height:32px;border-radius:50%;background:var(--brand);color:#fff;
font-weight:700;display:flex;align-items:center;justify-content:center;font-size:14px}
.step h3{font-size:15.5px;font-weight:650;margin-bottom:3px}.step p{color:var(--ink-soft);font-size:14px;line-height:1.55}
.urlbox{display:flex;gap:10px;align-items:center;background:color-mix(in srgb,var(--purple) 7%,var(--card));
border:1.5px solid color-mix(in srgb,var(--purple) 26%,var(--line));border-radius:11px;
padding:12px 14px;margin-top:9px;font-family:var(--mono);font-size:13.5px;overflow-x:auto}
.urlbox code{white-space:nowrap;color:var(--brand);font-weight:600}.urlbox button{margin-left:auto;flex:none}
@media(prefers-color-scheme:dark){.urlbox code{color:var(--purple)}}
.reassure{color:var(--ink-soft);font-size:13px;margin-top:7px}
.empty{text-align:center;color:var(--ink-soft);padding:38px 10px;font-size:14.5px;line-height:1.6}
.error{background:var(--bad-bg);color:var(--bad);border-radius:10px;padding:11px 13px;font-size:14px;margin-bottom:16px}
.login-wrap{min-height:100vh;display:flex;align-items:center;justify-content:center;
background-image:radial-gradient(circle at 50% -12%, color-mix(in srgb,var(--purple) 16%,transparent), transparent 60%)}
.login-card{background:var(--card);border-radius:20px;padding:44px 38px;max-width:410px;width:92%;
border-top:4px solid var(--brand);box-shadow:0 10px 40px rgba(52,25,72,.12)}
.login-card label{display:block;font-size:13px;font-weight:600;margin-bottom:15px;color:var(--ink)}
.login-card input{display:block;width:100%;margin-top:7px}
.login-card button{width:100%;padding:13px;margin-top:8px;font-size:15px}
.login-card .brand{font-weight:700;letter-spacing:.01em;font-size:22px;color:var(--brand);margin-bottom:18px;display:inline-block}
.login-card .brand::after{content:"";display:inline-block;width:7px;height:7px;border-radius:50%;background:var(--purple);margin-left:2px}
"""


def _page(body: str, title: str = "Qwintiq — Control panel", status_code: int = 200) -> HTMLResponse:
    return HTMLResponse(
        f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title}</title><style>{CSS}</style></head><body>{body}</body></html>""",
        status_code=status_code)


async def admin_login(request: Request):
    error = ""
    status = 200
    if request.method == "POST":
        ip = ratelimit.client_ip(request)
        allowed, retry = ratelimit.check(f"admin_login:{ip}", limit=8, window=300, lockout=900)
        if not allowed:
            status = 429
            error = (f'<div class="error">Too many attempts. Try again in about '
                     f'{retry // 60 + 1} minutes.</div>')
        else:
            form = await request.form()
            admin = dal.q("select * from admins where email=?",
                          (str(form.get("email", "")).lower().strip(),), fetch="one")
            if admin and verify_password(str(form.get("password", "")), admin["password_hash"]):
                ratelimit.clear(f"admin_login:{ip}")
                resp = RedirectResponse("/admin", status_code=302)
                resp.set_cookie("qv_admin", _sign(admin["id"], int(time.time()) + 12 * 3600),
                                httponly=True, samesite="lax", secure=_secure_cookies(request))
                return resp
            status = 401
            error = '<div class="error">Wrong email or password.</div>'
    return _page(f"""
<div class="login-wrap"><div class="login-card">
<div class="brand">Qwintiq</div><h1>Control panel</h1>
<p class="sub" style="margin:6px 0 22px;color:var(--ink-soft);font-size:14px">Sign in to manage your consultants.</p>
{error}
<form method="post"><label>Email<input type="email" name="email" required autofocus></label>
<label>Password<input type="password" name="password" required></label>
<button class="primary" type="submit">Sign in</button></form>
</div></div>""", "Sign in — Qwintiq control panel", status_code=status)


async def panel(request: Request):
    if not _admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    # Pop the one-view flash (temp password lives server-side, never in the URL).
    nonce = request.query_params.get("added", "")
    data = _FLASH.pop(nonce, None) if nonce else None
    notice = ""
    if data and data["exp"] > time.time():
        base = _base_url(request)
        name = html.escape(data["name"])
        email = html.escape(data["email"])
        pw = html.escape(data["pw"])
        invite = html.escape(
            f"Hi {data['name']}, you've been added to Qwintiq. Here's everything to get set up "
            f"(takes a minute, one time only):\n\n"
            f"1. Open this page and follow it: {base}/welcome\n"
            f"2. Your sign-in email: {data['email']}\n"
            f"3. Your one-time password: {data['pw']}\n\n"
            f"That's it — after that you just ask for Qwintiq work as normal.",
            quote=True)
        notice = (
            f'<div class="notice"><strong>{name} is in.</strong> Send them their temporary '
            f'password: <code id="pw">{pw}</code> — shown once, used the first time they sign in '
            f'(they set their own password then).'
            f'<div class="notice-actions">'
            f'<button class="quiet" type="button" onclick="cp(document.getElementById(\'pw\').textContent,this)">Copy password</button>'
            f'<button class="primary" type="button" onclick="cp(this.dataset.invite,this)" '
            f'data-invite="{invite}">Copy full invite</button>'
            f'<a class="linkbtn" href="/welcome" target="_blank">Preview what they\'ll see →</a>'
            f'</div></div>'
            '<script>function cp(t,b){navigator.clipboard.writeText(t).then(()=>{'
            'const o=b.textContent;b.textContent="Copied ✓";setTimeout(()=>b.textContent=o,1500)})}</script>')
    consultants = dal.list_consultants()
    active = [c for c in consultants if c["status"] == "active"]
    rows = ""
    for c in active:
        cid = html.escape(c["id"], quote=True)
        cname = html.escape(c["full_name"] or "—")
        cemail = html.escape(c["email"])
        key_pill = ('<span class="pill on">Key active</span>' if c["key_active"]
                    else '<span class="pill off">No key</span>')
        key_btn = (
            f'<form class="inline" method="post" action="/admin/revoke-key">'
            f'<input type="hidden" name="id" value="{cid}">'
            f'<button class="quiet">Revoke key</button></form>' if c["key_active"] else
            f'<form class="inline" method="post" action="/admin/assign-key">'
            f'<input type="hidden" name="id" value="{cid}">'
            f'<button class="primary">Assign key</button></form>')
        rows += (f'<tr><td><strong>{cname}</strong><br>'
                 f'<span style="color:var(--ink-soft);font-size:13px">{cemail}</span></td>'
                 f'<td>{key_pill}</td><td style="text-align:right;white-space:nowrap">{key_btn} '
                 f'<form class="inline" method="post" action="/admin/remove">'
                 f'<input type="hidden" name="id" value="{cid}">'
                 f'<button class="danger" onclick="return confirm(\'Remove this consultant? '
                 f'Their access ends immediately.\')">Remove</button></form></td></tr>')
    table = (f'<table><tr><th>Consultant</th><th>Access</th><th style="text-align:right">Actions</th></tr>{rows}</table>'
             if rows else
             '<div class="empty">No consultants yet.<br>Add your first one above — '
             'they get access in seconds.</div>')
    return _page(f"""
<header><span class="brand">Qwintiq</span><span class="navlinks"><a href="/admin/settings">Settings</a><a href="/admin/logout">Sign out</a></span></header>
<main>
<h1>Control panel</h1>
<p class="sub">Add or remove consultants, and switch their access key on or off. That's all this does — on purpose.</p>
{notice}
<div class="card"><h2>Add a consultant</h2>
<form method="post" action="/admin/add" class="addrow">
<input name="full_name" placeholder="Full name" required>
<input name="email" type="email" placeholder="Email address" required>
<button class="primary" type="submit">Add consultant</button>
</form>
<p style="color:var(--ink-soft);font-size:13px;margin-top:10px">They get a key automatically and you'll see a one-time
temporary password to send them. Nothing to install on their side beyond the Qwintiq connector.</p>
</div>
<div class="card"><h2>Your consultants</h2>{table}</div>
</main>""")


# Keys the admin can manage from Settings. Each is stored ENCRYPTED in the DB (dal.set_secret)
# and used only server-side; the value is never shown back.
_MANAGED_KEYS = [
    ("OPENAI_API_KEY", "ChatGPT (OpenAI) key",
     "Runs the copy & icebreaker skills. Set either this or the Anthropic key — whichever you have."),
    ("ANTHROPIC_API_KEY", "Claude (Anthropic) key",
     "Alternative engine for the copy & icebreaker skills. Only one AI key is needed."),
    ("AI_ARK_API_KEY", "AI-Ark data key",
     "Powers market-sizing / list-building. Leave empty to run those in safe demo mode."),
]

_STAT = {"managed_here": ('<span class="stat on">Set · saved here</span>', "Saved (encrypted). Enter a new value to replace it."),
         "from_env": ('<span class="stat env">Set · from host</span>', "Currently coming from the host config. Setting one here overrides it."),
         "not_set": ('<span class="stat off">Not set</span>', "")}


async def settings(request: Request):
    if not _admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    saved = request.query_params.get("saved", "")
    cleared = request.query_params.get("cleared", "")
    flash = ""
    if saved:
        flash = f'<div class="notice"><strong>Saved.</strong> {html.escape(saved)} is set and encrypted. It takes effect on the next request.</div>'
    elif cleared:
        flash = f'<div class="notice"><strong>Cleared.</strong> {html.escape(cleared)} removed. It now falls back to the host config (or demo mode).</div>'
    rows = ""
    for name, label, desc in _MANAGED_KEYS:
        status = dal.secret_status(name)
        pill, hint = _STAT[status]
        clear_btn = (f'<form class="inline" method="post" action="/admin/settings/clear">'
                     f'<input type="hidden" name="name" value="{name}">'
                     f'<button class="quiet">Clear</button></form>' if status == "managed_here" else "")
        rows += (
            f'<div class="keyrow"><div class="meta"><h3>{html.escape(label)} {pill}</h3>'
            f'<div class="desc">{html.escape(desc)}{(" " + html.escape(hint)) if hint else ""}</div></div>'
            f'<form method="post" action="/admin/settings/set">'
            f'<input type="hidden" name="name" value="{name}">'
            f'<input type="password" name="value" placeholder="Paste key…" autocomplete="off" required>'
            f'<button class="primary">Save</button></form>{clear_btn}</div>')
    return _page(f"""
<header><span class="brand">Qwintiq</span><span class="navlinks"><a href="/admin">Control panel</a><a href="/admin/logout">Sign out</a></span></header>
<main>
<h1>Settings</h1>
<p class="sub">The keys the vault uses to do its work. They're stored encrypted, used only inside the vault, and never shown to consultants.</p>
{flash}
<div class="card"><h2>API keys</h2>{rows}</div>
<p class="sub" style="font-size:13px">Keys are encrypted before they're saved, so they can't be read straight from the database. You'll never see a key again after saving — enter a new value to change it, or Clear to remove it.</p>
</main>""", "Settings — Qwintiq")


async def settings_set(request: Request):
    if not _admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    form = await request.form()
    name = str(form.get("name", ""))
    value = str(form.get("value", "")).strip()
    valid = {k for k, _, _ in _MANAGED_KEYS}
    if name in valid and value:
        dal.set_secret(name, value)
        label = next(lbl for k, lbl, _ in _MANAGED_KEYS if k == name)
        return RedirectResponse(f"/admin/settings?saved={label}", status_code=302)
    return RedirectResponse("/admin/settings", status_code=302)


async def settings_clear(request: Request):
    if not _admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    form = await request.form()
    name = str(form.get("name", ""))
    valid = {k for k, _, _ in _MANAGED_KEYS}
    if name in valid:
        dal.clear_secret(name)
        label = next(lbl for k, lbl, _ in _MANAGED_KEYS if k == name)
        return RedirectResponse(f"/admin/settings?cleared={label}", status_code=302)
    return RedirectResponse("/admin/settings", status_code=302)


async def add(request: Request):
    if not _admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    form = await request.form()
    email = str(form.get("email", "")).strip()
    name = str(form.get("full_name", "")).strip()
    existing = dal.get_consultant_by_email(email)
    if existing and existing["status"] == "active":
        return RedirectResponse("/admin", status_code=302)
    pw = temp_password()
    if existing:  # re-adding someone previously removed
        dal.q("update consultants set status='active', revoked_at=null, password_hash=?,"
              " must_change_password=1, full_name=? where id=?",
              (hash_password(pw), name, existing["id"]))
        dal.issue_key(existing["id"])
    else:
        c = dal.add_consultant(email, name, hash_password(pw))
        dal.issue_key(c["id"])
    # Stash the temp password server-side under a random nonce; the redirect carries only the
    # nonce, so the plaintext credential never lands in the URL, browser history, or proxy logs.
    nonce = secrets.token_urlsafe(16)
    _FLASH[nonce] = {"name": name, "email": email.lower().strip(), "pw": pw, "exp": time.time() + 600}
    return RedirectResponse(f"/admin?added={nonce}", status_code=302)


async def remove(request: Request):
    if not _admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    form = await request.form()
    dal.remove_consultant(str(form.get("id", "")))
    return RedirectResponse("/admin", status_code=302)


async def assign_key(request: Request):
    if not _admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    form = await request.form()
    dal.issue_key(str(form.get("id", "")))
    return RedirectResponse("/admin", status_code=302)


async def revoke_key(request: Request):
    if not _admin(request):
        return RedirectResponse("/admin/login", status_code=302)
    form = await request.form()
    dal.revoke_key(str(form.get("id", "")))
    return RedirectResponse("/admin", status_code=302)


async def welcome(request: Request):
    """Consultant onboarding — the branded page that replaces 'paste this terminal command'.
    No login needed; it's the friendly front door an invite links to."""
    mcp_url = _base_url(request) + "/mcp"
    return _page(f"""
<header><span class="brand">Qwintiq</span></header>
<main>
<h1>Welcome to Qwintiq</h1>
<p class="sub">Three quick steps, once. After this you just ask for your work as normal — nothing to install again.</p>
<div class="card">
  <div class="step"><div class="n">1</div><div>
    <h3>Open Claude and go to Connectors</h3>
    <p>In Claude, open <b>Settings → Connectors</b>. This is where Claude connects to the tools you use.</p>
  </div></div>
  <div class="step"><div class="n">2</div><div>
    <h3>Add Qwintiq as a custom connector</h3>
    <p>Click <b>Add custom connector</b>, paste the Qwintiq address below, and connect. This is the only
    setup step, and you'll only ever do it on day one.</p>
    <div class="urlbox"><code id="connect">{mcp_url}</code>
      <button class="primary" type="button" onclick="cp(document.getElementById('connect').textContent,this)">Copy</button></div>
    <p class="reassure">It just points Claude at Qwintiq — nothing is downloaded or installed on your computer.</p>
  </div></div>
  <div class="step"><div class="n">3</div><div>
    <h3>Sign in with the details Qwintiq sent you</h3>
    <p>A Qwintiq sign-in page opens. Enter the email and one-time password from your invite, then choose
    your own password. That's it — you're connected.</p>
  </div></div>
</div>
<div class="card">
  <h2>Then just ask, in plain English</h2>
  <p style="color:var(--ink-soft);font-size:14.5px;line-height:1.7">
    &ldquo;Write Qwintiq copy for a prospect who…&rdquo;<br>
    &ldquo;Size the market for dental clinics in the UK&rdquo;<br>
    &ldquo;Write icebreakers for these 10 people&rdquo;<br>
    &ldquo;Run today's partner signals&rdquo;<br>
    Qwintiq does the work and hands you the finished result. Anything that would spend real credits pauses
    and asks you to confirm first, so nothing costs money by surprise.
  </p>
</div>
<p class="sub" style="text-align:center">Stuck? Your Qwintiq admin can re-send your details or reset your access any time.</p>
</main>
<script>function cp(t,b){{navigator.clipboard.writeText(t).then(()=>{{const o=b.textContent;
b.textContent="Copied ✓";setTimeout(()=>b.textContent=o,1500)}})}}</script>""",
                 "Welcome — Qwintiq")


async def logout(request: Request):
    resp = RedirectResponse("/admin/login", status_code=302)
    # Mirror the set_cookie attributes so the browser actually clears the session cookie.
    resp.delete_cookie("qv_admin", httponly=True, samesite="lax", secure=_secure_cookies(request))
    return resp
