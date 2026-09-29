from __future__ import annotations

import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any

from mcp.server import MCPServer
from mcp.server.transport_security import TransportSecuritySettings
from starlette.applications import Starlette
from starlette.middleware.base import BaseHTTPMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Mount, Route

from bridge import oauth, oauth_store
from bridge.browser_use import BrowserUseBridge

bridge = BrowserUseBridge()
mcp = MCPServer(
    "football-consensus-browser-bridge",
    instructions=(
        "Browser Use is infrastructure only. Tools return raw browser state. "
        "Do not use these tools to bypass paywalls, authentication, or access controls. "
        "Always stop browser sessions when work is complete."
    ),
)


@mcp.tool()
async def browser_create(proxy_country_code: str | None = None) -> dict[str, Any]:
    """Create one Browser Use cloud browser. Returns an opaque session id, never the CDP URL."""
    return await bridge.create(proxy_country_code)


@mcp.tool()
async def browser_open_url(
    session_id: str,
    url: str,
    wait_until: str = "domcontentloaded",
    timeout_ms: int = 45000,
    capture_network: bool = True,
) -> dict[str, Any]:
    """Navigate a managed browser to an authorized public http/https URL."""
    return await bridge.open_url(session_id, url, wait_until, timeout_ms, capture_network)


@mcp.tool()
async def browser_get_html(session_id: str, selector: str | None = None) -> dict[str, Any]:
    """Return raw outer HTML for the page or one selector."""
    return await bridge.html(session_id, selector)


@mcp.tool()
async def browser_get_dom(
    session_id: str,
    selector: str = "body",
    max_chars: int = 100000,
) -> dict[str, Any]:
    """Return visible DOM text for a selector without AI interpretation."""
    return await bridge.dom_text(session_id, selector, max_chars)


@mcp.tool()
async def browser_screenshot(session_id: str, full_page: bool = True) -> dict[str, Any]:
    """Return a PNG screenshot as base64."""
    return await bridge.screenshot(session_id, full_page)


@mcp.tool()
async def browser_get_network_responses(session_id: str) -> dict[str, Any]:
    """Return XHR/fetch/document response metadata captured by the last navigation."""
    return await bridge.network_responses(session_id)


@mcp.tool()
async def browser_stop(session_id: str) -> dict[str, Any]:
    """Stop the Browser Use cloud browser and remove its in-memory CDP capability."""
    return await bridge.stop(session_id)


class OAuthBearerMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request: Request, call_next):
        if request.url.path.startswith("/mcp"):
            auth = request.headers.get("authorization", "")
            token = auth[7:] if auth.startswith("Bearer ") else ""
            valid = False
            if token:
                try:
                    valid = oauth_store.valid_access_token(token, oauth.resource_url(), oauth.SCOPE)
                except Exception:
                    return JSONResponse({"detail": "authorization backend unavailable"}, status_code=503)
            if not valid:
                metadata = oauth.public_base() + "/.well-known/oauth-protected-resource"
                return JSONResponse(
                    {"detail": "unauthorized"},
                    status_code=401,
                    headers={"WWW-Authenticate": f'Bearer resource_metadata="{metadata}"'},
                )
        return await call_next(request)


async def healthz(_request: Request):
    return JSONResponse(
        {
            "ok": True,
            "browser_use_configured": bool(os.environ.get("BROWSER_USE_API_KEY", "").strip()),
            "oauth_password_configured": bool(os.environ.get("MCP_OAUTH_PASSWORD", "").strip()),
            "database_configured": bool(os.environ.get("DATABASE_URL", "").strip()),
        }
    )


public_host = os.environ.get(
    "MCP_PUBLIC_HOST",
    "football-consensus-browser-bridge.onrender.com",
).strip()
transport_security = TransportSecuritySettings(
    enable_dns_rebinding_protection=True,
    allowed_hosts=[public_host, f"{public_host}:*"],
    allowed_origins=[
        "https://chatgpt.com",
        "https://chat.openai.com",
    ],
)

mcp_app = mcp.streamable_http_app(
    streamable_http_path="/",
    json_response=True,
    stateless_http=True,
    transport_security=transport_security,
)


@asynccontextmanager
async def lifespan(_app: Starlette) -> AsyncIterator[None]:
    async with mcp.session_manager.run():
        yield


app = Starlette(
    routes=[
        Route("/healthz", healthz, methods=["GET"]),
        Route("/.well-known/oauth-protected-resource", oauth.protected_resource, methods=["GET"]),
        Route("/.well-known/oauth-authorization-server", oauth.auth_metadata, methods=["GET"]),
        Route("/oauth/authorize", oauth.authorize_get, methods=["GET"]),
        Route("/oauth/authorize", oauth.authorize_post, methods=["POST"]),
        Route("/oauth/token", oauth.token_endpoint, methods=["POST"]),
        Mount("/mcp", app=mcp_app),
    ],
    lifespan=lifespan,
)
app.add_middleware(OAuthBearerMiddleware)
