from __future__ import annotations

import hmac
import asyncio
import os
import re
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
async def browser_create_cloak() -> dict[str, Any]:
    """Create one bridge-owned CloakBrowser session when server-side enabled."""
    return await bridge.create_cloak()


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
    """Stop the bridge-owned browser session and remove its in-memory capabilities."""
    return await bridge.stop(session_id)


@mcp.tool()
async def browser_reap(idle_seconds: int = 300) -> dict[str, Any]:
    """Stop only bridge-owned expired idle leases; active operations are excluded."""
    return await bridge.reap(idle_seconds)


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


async def internal_pinnacle_leagues(request: Request):
    expected = os.environ.get("COLLECTOR_BRIDGE_TOKEN", "").strip()
    supplied = request.headers.get("x-collector-token", "")
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)

    session_id = None; result = None; failure = None
    try:
        created = await bridge.create(); session_id = created['session_id']
        result = await bridge.json_document(session_id,
            'https://guest.api.arcadia.pinnacle.com/0.1/sports/29/leagues?brandId=0')
    except Exception as exc:
        failure = type(exc).__name__
    finally:
        if session_id:
            try:
                stopped = await bridge.stop(session_id)
                if stopped.get('status') != 'stopped':
                    raise RuntimeError('browser stop not confirmed')
            except Exception as exc:
                failure = 'STOP_FAILED:' + type(exc).__name__
    if failure:
        return JSONResponse({'detail': failure}, status_code=502)
    return JSONResponse(result)


async def internal_pinnacle_league_batch(request: Request):
    expected = os.environ.get('COLLECTOR_BRIDGE_TOKEN', '').strip()
    supplied = request.headers.get('x-collector-token', '')
    if not expected or not supplied or not hmac.compare_digest(expected, supplied):
        return JSONResponse({'detail': 'unauthorized'}, status_code=401)
    raw = request.query_params.get('ids', '')
    if not re.fullmatch(r'[1-9][0-9]{0,8}(?:,[1-9][0-9]{0,8}){0,11}', raw):
        return JSONResponse({'detail': '1..12 positive league IDs required'}, status_code=400)
    ids = [int(x) for x in raw.split(',')]
    if len(set(ids)) != len(ids):
        return JSONResponse({'detail': 'duplicate league IDs'}, status_code=400)
    session_id = None; items = []; failure = None; total_bytes = 0
    try:
        async with asyncio.timeout(140):
            created = await bridge.create(); session_id = created['session_id']
            for lid in ids:
                item = {'id': lid}
                try:
                    for kind, suffix in (('matchups','matchups?brandId=0'), ('markets','markets/straight')):
                        result = await bridge.json_document(session_id,
                            f'https://guest.api.arcadia.pinnacle.com/0.1/leagues/{lid}/{suffix}')
                        total_bytes += result['bytes_utf8']
                        if total_bytes > 20 * 1024 * 1024:
                            raise RuntimeError('batch document size limit exceeded')
                        item[kind] = result
                except Exception as exc:
                    # A failing league stays explicit; it cannot hide other league results.
                    item = {'id': lid, 'error': type(exc).__name__}
                items.append(item)
    except Exception as exc:
        failure = type(exc).__name__
    finally:
        if session_id:
            try:
                result = await bridge.stop(session_id)
                if result.get('status') != 'stopped':
                    raise RuntimeError('browser stop not confirmed')
            except Exception as exc:
                failure = 'STOP_FAILED:' + type(exc).__name__
    if failure:
        return JSONResponse({'detail': failure}, status_code=502)
    return JSONResponse({'items': items, 'transport': 'BROWSER_JSON_DOCUMENT', 'session_stopped': True})


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
        if os.environ.get("CLOAK_STARTUP_SMOKE", "").strip().lower() in {"1", "true", "yes", "on"}:
            smoke_session = None
            try:
                created = await bridge.create_cloak()
                smoke_session = created["session_id"]
                opened = await bridge.open_url(smoke_session, "https://example.com", "domcontentloaded", 45000, False)
                from bridge.browser_use import LOG
                LOG.info("CLOAK_STARTUP_SMOKE_OK provider=%s status=%s title=%s http_status=%s",
                         created.get("provider"), created.get("status"), opened.get("title"), opened.get("http_status"))
            except Exception as exc:
                from bridge.browser_use import LOG
                LOG.error("CLOAK_STARTUP_SMOKE_FAILED error=%s", type(exc).__name__)
            finally:
                if smoke_session:
                    try:
                        await bridge.stop(smoke_session)
                    except Exception as exc:
                        from bridge.browser_use import LOG
                        LOG.error("CLOAK_STARTUP_SMOKE_STOP_FAILED error=%s", type(exc).__name__)
        reaper = asyncio.create_task(bridge.reaper_loop())
        try:
            yield
        finally:
            reaper.cancel()
            try:
                await reaper
            except asyncio.CancelledError:
                pass
            result = await bridge.stop_all()
            if result['failed']:
                from bridge.browser_use import LOG
                LOG.error('Bridge shutdown could not stop %s owned sessions', len(result['failed']))


app = Starlette(
    routes=[
        Route("/healthz", healthz, methods=["GET"]),
        Route("/internal/pinnacle/leagues", internal_pinnacle_leagues, methods=["GET"]),
        Route('/internal/pinnacle/league-batch', internal_pinnacle_league_batch, methods=['GET']),
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
