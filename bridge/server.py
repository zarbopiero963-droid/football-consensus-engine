from __future__ import annotations

import hmac
import asyncio
import os
import logging
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
async def browser_click(
    session_id: str,
    selector: str,
    button: str = "left",
    click_count: int = 1,
    timeout_ms: int = 30000,
    wait_after_ms: int = 0,
) -> dict[str, Any]:
    """Click the first matching element in either Browser Use or CloakBrowser."""
    return await bridge.click(session_id, selector, button, click_count, timeout_ms, wait_after_ms)


@mcp.tool()
async def browser_fill(session_id: str, selector: str, value: str, timeout_ms: int = 30000) -> dict[str, Any]:
    """Fill an input without echoing the supplied value in the result."""
    return await bridge.fill(session_id, selector, value, timeout_ms)


@mcp.tool()
async def browser_type(
    session_id: str,
    selector: str,
    text: str,
    delay_ms: int = 0,
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Type sequentially into an element without echoing the supplied text."""
    return await bridge.type_text(session_id, selector, text, delay_ms, timeout_ms)


@mcp.tool()
async def browser_press(session_id: str, selector: str, key: str, timeout_ms: int = 30000) -> dict[str, Any]:
    """Press a keyboard key or chord on the first matching element."""
    return await bridge.press(session_id, selector, key, timeout_ms)


@mcp.tool()
async def browser_select_option(
    session_id: str,
    selector: str,
    values: list[str],
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Select one or more option values from a select element."""
    return await bridge.select_option(session_id, selector, values, timeout_ms)


@mcp.tool()
async def browser_set_checked(
    session_id: str,
    selector: str,
    checked: bool = True,
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Set a checkbox or radio control to the requested checked state."""
    return await bridge.set_checked(session_id, selector, checked, timeout_ms)


@mcp.tool()
async def browser_hover(session_id: str, selector: str, timeout_ms: int = 30000) -> dict[str, Any]:
    """Hover the first matching element."""
    return await bridge.hover(session_id, selector, timeout_ms)


@mcp.tool()
async def browser_focus(session_id: str, selector: str, timeout_ms: int = 30000) -> dict[str, Any]:
    """Focus the first matching element."""
    return await bridge.focus(session_id, selector, timeout_ms)


@mcp.tool()
async def browser_scroll(session_id: str, delta_x: int = 0, delta_y: int = 700) -> dict[str, Any]:
    """Scroll the active page using mouse-wheel deltas."""
    return await bridge.scroll(session_id, delta_x, delta_y)


@mcp.tool()
async def browser_wait_for_selector(
    session_id: str,
    selector: str,
    state: str = "visible",
    timeout_ms: int = 30000,
) -> dict[str, Any]:
    """Wait for a selector to become attached, detached, visible or hidden."""
    return await bridge.wait_for_selector(session_id, selector, state, timeout_ms)


@mcp.tool()
async def browser_wait(session_id: str, milliseconds: int = 1000) -> dict[str, Any]:
    """Wait a bounded amount of time in the active page."""
    return await bridge.wait(session_id, milliseconds)


@mcp.tool()
async def browser_back(session_id: str, wait_until: str = "domcontentloaded", timeout_ms: int = 45000) -> dict[str, Any]:
    """Navigate the active page backward in history."""
    return await bridge.back(session_id, wait_until, timeout_ms)


@mcp.tool()
async def browser_forward(session_id: str, wait_until: str = "domcontentloaded", timeout_ms: int = 45000) -> dict[str, Any]:
    """Navigate the active page forward in history."""
    return await bridge.forward(session_id, wait_until, timeout_ms)


@mcp.tool()
async def browser_reload(session_id: str, wait_until: str = "domcontentloaded", timeout_ms: int = 45000) -> dict[str, Any]:
    """Reload the active page."""
    return await bridge.reload(session_id, wait_until, timeout_ms)


@mcp.tool()
async def browser_get_elements(
    session_id: str,
    selector: str = "input,button,a,select,textarea",
    limit: int = 100,
) -> dict[str, Any]:
    """Return safe metadata for matching elements; input values are deliberately omitted."""
    return await bridge.elements(session_id, selector, limit)


@mcp.tool()
async def browser_list_pages(session_id: str) -> dict[str, Any]:
    """List tabs/pages for the managed browser session."""
    return await bridge.list_pages(session_id)


@mcp.tool()
async def browser_new_page(session_id: str, url: str | None = None) -> dict[str, Any]:
    """Open a new tab/page and optionally navigate to an authorized public URL."""
    return await bridge.new_page(session_id, url)


@mcp.tool()
async def browser_switch_page(session_id: str, page_index: int) -> dict[str, Any]:
    """Switch the active tab/page by its current zero-based index."""
    return await bridge.switch_page(session_id, page_index)


@mcp.tool()
async def browser_close_page(session_id: str, page_index: int) -> dict[str, Any]:
    """Close one tab/page; the final remaining page cannot be closed."""
    return await bridge.close_page(session_id, page_index)


@mcp.tool()
async def browser_get_network_responses(session_id: str) -> dict[str, Any]:
    """Return XHR/fetch/document response metadata captured by the last navigation."""
    return await bridge.network_responses(session_id)


@mcp.tool()
async def browser_stop(session_id: str) -> dict[str, Any]:
    """Stop the bridge-owned browser session and remove its in-memory capabilities."""
    return await bridge.stop(session_id)


@mcp.tool()
async def browser_list_sessions(include_stopped: bool = False, limit: int = 200) -> dict[str, Any]:
    """List durable bridge-owned browser sessions without exposing provider capabilities."""
    return await bridge.list_sessions(include_stopped, limit)


@mcp.tool()
async def browser_session_status(session_id: str) -> dict[str, Any]:
    """Return lifecycle status for one bridge-owned browser session."""
    return await bridge.session_status(session_id)


@mcp.tool()
async def browser_reap(idle_seconds: int = 300) -> dict[str, Any]:
    """Stop expired idle bridge leases; invalid thresholds return a structured error."""
    return await bridge.reap(idle_seconds)


@mcp.tool()
async def browser_stop_all_idle(idle_seconds: int = 300) -> dict[str, Any]:
    """Stop expired idle bridge sessions while excluding active local operations."""
    return await bridge.stop_all_idle(idle_seconds)


@mcp.tool()
async def browser_stop_all() -> dict[str, Any]:
    """Emergency stop for all sessions local to this bridge worker."""
    return await bridge.stop_all()


@mcp.tool()
async def browser_health() -> dict[str, Any]:
    """Return bridge/database/provider configuration health without exposing secrets."""
    return await bridge.health()


@mcp.tool()
async def browser_cleanup_report() -> dict[str, Any]:
    """Return durable/local cleanup counts and the last reaper failure, if any."""
    return await bridge.cleanup_report()


APIFOOTBALL_REGISTRATION_ERROR_TYPE: str | None = None

def _register_optional_api_football_tools(registry) -> tuple[str, ...]:
    """Opt-in and fail closed; no optional imports when the flag is off.

    Isolate a broken/missing adapter from OAuth, browser tools and MCP startup.
    Deliberately avoid logging exception messages, which may contain settings.
    """
    global APIFOOTBALL_REGISTRATION_ERROR_TYPE
    if os.environ.get("APIFOOTBALL_ENABLED", "false").strip().lower() not in ("true", "1"):
        return ()
    try:
        from bridge.api_football.config import Settings
        from bridge.api_football.tools import register_tools
        from bridge.api_football.wiring import build_client
        settings = Settings.from_env()
        client = build_client(settings)
        return register_tools(registry, settings, client=client)
    except Exception as exc:
        APIFOOTBALL_REGISTRATION_ERROR_TYPE = type(exc).__name__
        logging.getLogger(__name__).error(
            "optional_apifootball_registration_failed type=%s; browser tools unaffected",
            APIFOOTBALL_REGISTRATION_ERROR_TYPE,
        )
        return ()


# Register after the existing browser tool definitions, before MCP transport.
# The OFF state is indistinguishable from the original tool registry.
APIFOOTBALL_REGISTERED_TOOLS = _register_optional_api_football_tools(mcp)


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
            "apifootball_enabled": os.environ.get("APIFOOTBALL_ENABLED", "false").strip().lower() in ("true", "1"),
            "apifootball_key_configured": bool(os.environ.get("APIFOOTBALL_API_KEY", "").strip()),
            "apifootball_live_authorized": os.environ.get("APIFOOTBALL_LIVE_AUTHORIZED", "false").strip().lower() in ("true", "1"),
            "apifootball_registered_count": len(APIFOOTBALL_REGISTERED_TOOLS),
            "apifootball_registration_error_type": APIFOOTBALL_REGISTRATION_ERROR_TYPE,
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
        "https://claude.ai",
        "https://claude.com",
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
    # Render should not mark a fresh worker ready until Cloak's Chromium
    # artifact is actually present. This moves the expensive cold download
    # out of the first user MCP request.
    await bridge.prepare_cloak_runtime()
    async with mcp.session_manager.run():
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
        Route("/oauth/register", oauth.register_claude_client, methods=["POST"]),
        Route("/oauth/authorize", oauth.authorize_get, methods=["GET"]),
        Route("/oauth/authorize", oauth.authorize_post, methods=["POST"]),
        Route("/oauth/token", oauth.token_endpoint, methods=["POST"]),
        Mount("/mcp", app=mcp_app),
    ],
    lifespan=lifespan,
)
app.add_middleware(OAuthBearerMiddleware)
