"""Qwintiq Vault — remote MCP server (the "Qwintiq Box").

Holds Qwintiq's four skill frameworks server-side and returns finished work only.
Everything mounts in one app:

  /mcp                 — the vault's tools (behind the AuthGate: valid bearer token,
                          consultant active, key active — checked on EVERY call)
  /authorize /token
  /register /.well-known/* — the OAuth wall (the consultant login screen lives here)
  /admin               — Aliyah's control panel (two functions: people, keys)

Run locally:  python server.py     (Streamable HTTP on :$PORT)
"""
import contextlib
import os

try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:
    pass

from starlette.applications import Starlette
from starlette.responses import RedirectResponse
from starlette.routing import Mount, Route

from admin import panel
from auth import oauth
from db import dal
from vault.tools import mcp


@contextlib.asynccontextmanager
async def lifespan(app):
    dal.init_db()
    async with mcp.session_manager.run():
        yield


async def home(request):
    return RedirectResponse("/admin", status_code=302)


app = Starlette(
    lifespan=lifespan,
    routes=[
        Route("/", home),
        Route("/.well-known/oauth-protected-resource", oauth.protected_resource_metadata),
        Route("/.well-known/oauth-protected-resource/mcp", oauth.protected_resource_metadata),
        Route("/.well-known/oauth-authorization-server", oauth.as_metadata),
        Route("/register", oauth.register, methods=["POST"]),
        Route("/authorize", oauth.authorize, methods=["GET", "POST"]),
        Route("/token", oauth.token, methods=["POST"]),
        Route("/welcome", panel.welcome),
        Route("/admin/login", panel.admin_login, methods=["GET", "POST"]),
        Route("/admin/logout", panel.logout),
        Route("/admin/add", panel.add, methods=["POST"]),
        Route("/admin/remove", panel.remove, methods=["POST"]),
        Route("/admin/assign-key", panel.assign_key, methods=["POST"]),
        Route("/admin/revoke-key", panel.revoke_key, methods=["POST"]),
        Route("/admin/settings", panel.settings),
        Route("/admin/settings/set", panel.settings_set, methods=["POST"]),
        Route("/admin/settings/clear", panel.settings_clear, methods=["POST"]),
        Route("/admin/settings/test", panel.settings_test, methods=["POST"]),
        Route("/admin", panel.panel),
        Mount("/", app=oauth.AuthGate(mcp.streamable_http_app())),
    ],
)


if __name__ == "__main__":
    import uvicorn

    # proxy_headers + forwarded_allow_ips="*" so uvicorn trusts Render's x-forwarded-proto and
    # rewrites the request scheme to https — without this every OAuth discovery URL is emitted
    # as http:// and the connector handshake fails. Render is the only ingress, so "*" is safe.
    # Single process (no --workers): the MCP StreamableHTTP session manager holds state in memory.
    uvicorn.run(app, host="0.0.0.0", port=int(os.environ.get("PORT", "8000")),
                proxy_headers=True, forwarded_allow_ips="*")
