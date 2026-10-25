from __future__ import annotations

import base64
import hashlib
import hmac
import html
import json
import logging
import os
import time
import secrets
from urllib.parse import urlencode, urlparse

import httpx
from starlette.requests import Request
from starlette.responses import HTMLResponse, JSONResponse, RedirectResponse

from bridge import oauth_store

SCOPE = "browser:use"
OFFLINE = "offline_access"

CLAUDE_CIMD_URL = "https://claude.ai/oauth/mcp-oauth-client-metadata"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
_DCR_PREFIX = "fcbdcr1_"
_DCR_MAX_JSON_BYTES = 8192
_CIMD_MAX_BYTES = 64 * 1024
LOG = logging.getLogger(__name__)


def public_base() -> str:
    host = os.environ.get("MCP_PUBLIC_HOST", "football-consensus-browser-bridge.onrender.com").strip()
    return f"https://{host}"


def resource_url() -> str:
    return public_base() + "/mcp"


def issuer() -> str:
    return public_base()


def protected_resource_metadata():
    return {
        "resource": resource_url(),
        "authorization_servers": [issuer()],
        "scopes_supported": [SCOPE, OFFLINE],
        "bearer_methods_supported": ["header"],
    }


def authorization_server_metadata():
    base = issuer()
    return {
        "issuer": base,
        "authorization_endpoint": base + "/oauth/authorize",
        "token_endpoint": base + "/oauth/token",
        "response_types_supported": ["code"],
        "grant_types_supported": ["authorization_code", "refresh_token"],
        "code_challenge_methods_supported": ["S256"],
        "token_endpoint_auth_methods_supported": ["none"],
        "scopes_supported": [SCOPE, OFFLINE],
        "client_id_metadata_document_supported": True,
        "authorization_response_iss_parameter_supported": True,
        "registration_endpoint": base + "/oauth/register",
    }


def _is_trusted_cimd_url(client_id: str) -> bool:
    try:
        parsed = urlparse(client_id)
        port = parsed.port
        hostname = parsed.hostname
    except ValueError:
        return False
    if (
        parsed.scheme != "https"
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or parsed.query
        or parsed.fragment
    ):
        return False
    if client_id == CLAUDE_CIMD_URL:
        return True
    return (
        hostname == "chatgpt.com"
        and parsed.path.startswith("/oauth/")
        and parsed.path.endswith("/client.json")
    )


def _cimd_document_failure(client_id: str, redirect_uri: str, data: object) -> str | None:
    """Only fixed reason codes. Never include document contents or OAuth parameters."""
    if not isinstance(data, dict):
        return "metadata_not_object"
    if data.get("client_id") != client_id:
        return "metadata_client_id_mismatch"
    redirects = data.get("redirect_uris")
    if not isinstance(redirects, list) or not redirects or any(
        not isinstance(item, str) for item in redirects
    ):
        return "metadata_invalid_redirect_list"
    if redirect_uri not in redirects:
        return "metadata_redirect_not_registered"
    auth_method = data.get("token_endpoint_auth_method")
    if client_id == CLAUDE_CIMD_URL and auth_method != "none":
        return "metadata_claude_auth_method"
    if client_id != CLAUDE_CIMD_URL and auth_method not in (None, "none", "private_key_jwt"):
        return "metadata_auth_method"
    response_types = data.get("response_types")
    if not isinstance(response_types, list) or "code" not in response_types:
        return "metadata_response_type"
    grants = data.get("grant_types")
    if not isinstance(grants, list) or "authorization_code" not in grants:
        return "metadata_grant_type"
    return None


def _valid_cimd_document(client_id: str, redirect_uri: str, data: object) -> bool:
    return _cimd_document_failure(client_id, redirect_uri, data) is None


def _client_kind(client_id: str) -> str:
    """Return a predefined label, never an untrusted URL or identifier."""
    if client_id == CLAUDE_CIMD_URL or client_id.startswith(_DCR_PREFIX):
        return "claude"
    if _is_trusted_cimd_url(client_id):
        return "chatgpt"
    return "untrusted"


def _reject(client_id: str, stage: str, reason: str) -> bool:
    # SECURITY: do not log request URLs, redirects, state, PKCE, tokens,
    # passwords, exception messages, response bodies or raw client IDs.
    LOG.warning(
        "oauth_rejected client=%s stage=%s reason=%s",
        _client_kind(client_id), stage, reason,
    )
    return False


async def validate_cimd(client_id: str, redirect_uri: str) -> bool:
    if not _is_trusted_cimd_url(client_id):
        return _reject(client_id, "metadata", "untrusted_client_id")
    try:
        async with httpx.AsyncClient(timeout=15, follow_redirects=False) as client:
            r = await client.get(client_id, headers={"Accept": "application/json"})
            r.raise_for_status()
            if len(r.content) > _CIMD_MAX_BYTES:
                return _reject(client_id, "metadata", "metadata_too_large")
            data = r.json()
    except httpx.HTTPStatusError as exc:
        # HTTP status is safe; no upstream response body or URL is logged.
        code = exc.response.status_code
        return _reject(client_id, "metadata", f"metadata_http_{code}")
    except httpx.TimeoutException:
        return _reject(client_id, "metadata", "metadata_timeout")
    except httpx.RequestError:
        return _reject(client_id, "metadata", "metadata_transport_error")
    except ValueError:
        return _reject(client_id, "metadata", "metadata_invalid_json")
    except Exception:
        return _reject(client_id, "metadata", "metadata_unexpected_error")

    failure = _cimd_document_failure(client_id, redirect_uri, data)
    if failure is not None:
        return _reject(client_id, "metadata", failure)
    return True



def _claude_dcr_registration_valid(data: object) -> bool:
    """Only the official Claude web callback can be registered.

    DCR public client metadata is not proof of vendor identity. Security comes
    from the pinned HTTPS callback, authorization password, PKCE and binding
    every code/token to its registered client and protected resource.
    """
    if not isinstance(data, dict):
        return False
    if data.get("redirect_uris") != [CLAUDE_CALLBACK]:
        return False
    if data.get("token_endpoint_auth_method", "none") != "none":
        return False
    responses = data.get("response_types", ["code"])
    if not isinstance(responses, list) or responses != ["code"]:
        return False
    grants = data.get("grant_types", ["authorization_code"])
    if not isinstance(grants, list) or "authorization_code" not in grants:
        return False
    # We only grant these two operations; reject registrations requesting
    # unsupported actions, without treating extra advertised client metadata
    # (e.g. JWT bearer) as permission to use such flows.
    allowed = {"authorization_code", "refresh_token", "urn:ietf:params:oauth:grant-type:jwt-bearer"}
    if any(not isinstance(v, str) or v not in allowed for v in grants):
        return False
    scope = data.get("scope")
    if scope is not None:
        if not isinstance(scope, str) or not set(scope.split()).issubset({SCOPE, OFFLINE}):
            return False
    name = data.get("client_name", "")
    if not isinstance(name, str) or len(name) > 128:
        return False
    client_uri = data.get("client_uri")
    if client_uri is not None and client_uri not in ("https://claude.ai", "https://claude.ai/"):
        return False
    return True


async def register_claude_client(request: Request):
    """Claude.ai DCR fallback for cloud-side CIMD/Cloudflare HTTP 403.

    Registration never returns an OAuth secret; this is a public PKCE client.
    """
    if request.headers.get("content-type", "").split(";", 1)[0].strip().lower() != "application/json":
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    chunks = []
    total = 0
    async for chunk in request.stream():
        total += len(chunk)
        if total > _DCR_MAX_JSON_BYTES:
            return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
        chunks.append(chunk)
    try:
        data = json.loads(b"".join(chunks))
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    if not _claude_dcr_registration_valid(data):
        _reject("", "registration", "invalid_claude_client_metadata")
        return JSONResponse({"error": "invalid_client_metadata"}, status_code=400)
    try:
        client_id = oauth_store.register_claude_dcr_client(CLAUDE_CALLBACK)
    except Exception:
        LOG.error("oauth_registration_error reason=storage_unavailable")
        return JSONResponse({"error": "server_error"}, status_code=503)
    return JSONResponse(
        {
            "client_id": client_id,
            "client_id_issued_at": int(time.time()),
            "client_name": "Claude",
            "redirect_uris": [CLAUDE_CALLBACK],
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "token_endpoint_auth_method": "none",
            "scope": f"{SCOPE} {OFFLINE}",
        },
        status_code=201,
        headers={"Cache-Control": "no-store"},
    )


def _cert_redirect_allowed(uri: str) -> bool:
    try:
        parsed = urlparse(uri)
        return (parsed.scheme == "http" and parsed.hostname in ("127.0.0.1", "localhost")
                and parsed.port is not None and 1024 <= parsed.port <= 65535
                and parsed.path == "/oauth/callback" and not parsed.query
                and not parsed.fragment and parsed.username is None)
    except ValueError:
        return False


async def register_cert_client(request: Request):
    """Registration explicitly enabled for local PKCE one-time consent only."""
    if os.getenv("FCB_CERT_OAUTH_REGISTRATION_ENABLED") != "true":
        return JSONResponse({"error": "not_found"}, status_code=404)
    try:
        body = await request.json()
    except (ValueError, UnicodeDecodeError):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    uri = body.get("redirect_uri") if isinstance(body, dict) else None
    if not isinstance(uri, str) or not _cert_redirect_allowed(uri):
        return JSONResponse({"error": "invalid_redirect_uri"}, status_code=400)
    try:
        client_id = oauth_store.register_cert_client(uri)
    except Exception:
        return JSONResponse({"error": "server_error"}, status_code=503)
    return JSONResponse({"client_id": client_id, "redirect_uri": uri,
                         "grant_types": ["authorization_code", "refresh_token"],
                         "token_endpoint_auth_method": "none"},
                        status_code=201, headers={"Cache-Control": "no-store"})


async def _valid_oauth_client(client_id: str, redirect_uri: str) -> bool:
    if client_id.startswith("fcbe2e1_"):
        if not _cert_redirect_allowed(redirect_uri):
            return False
        try:
            return oauth_store.valid_cert_client(client_id, redirect_uri)
        except Exception:
            return False
    if client_id.startswith(_DCR_PREFIX):
        if redirect_uri != CLAUDE_CALLBACK:
            return _reject(client_id, "registration", "invalid_claude_callback")
        try:
            result = oauth_store.valid_claude_dcr_client(client_id, redirect_uri)
        except Exception:
            return _reject(client_id, "registration", "registration_storage_error")
        if not result:
            return _reject(client_id, "registration", "unknown_or_expired_client_id")
        return True
    return await validate_cimd(client_id, redirect_uri)


def _pkce_s256(verifier: str) -> str:
    raw = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


async def protected_resource(_request: Request):
    return JSONResponse(protected_resource_metadata())


async def auth_metadata(_request: Request):
    return JSONResponse(authorization_server_metadata())


def _authorize_params(request: Request) -> dict[str, str]:
    q = request.query_params
    return {k: q.get(k, "") for k in (
        "client_id","redirect_uri","response_type","scope","state",
        "code_challenge","code_challenge_method","resource"
    )}


def _authorization_request_failure(p: dict[str, str]) -> str | None:
    if p["response_type"] != "code":
        return "unsupported_response_type"
    if p["code_challenge_method"] != "S256":
        return "unsupported_pkce_method"
    if p["resource"] != resource_url():
        return "resource_mismatch"
    if SCOPE not in set(p["scope"].split()):
        return "required_scope_missing"
    return None


async def authorize_get(request: Request):
    p = _authorize_params(request)
    failure = _authorization_request_failure(p)
    if failure is not None:
        _reject(p["client_id"], "authorize_get", failure)
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    if not await _valid_oauth_client(p["client_id"], p["redirect_uri"]):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    hidden = "".join(
        f'<input type="hidden" name="{html.escape(k)}" value="{html.escape(v)}">'
        for k,v in p.items()
    )
    body = f"""<!doctype html><html><body>
    <h2>Football Consensus Browser</h2>
    <p>Autorizza il client OAuth a usare il browser remoto.</p>
    <form method="post">{hidden}
      <label>Password di autorizzazione</label><br>
      <input type="password" name="password" required autofocus><br><br>
      <button type="submit">Autorizza</button>
    </form></body></html>"""
    return HTMLResponse(body)


async def authorize_post(request: Request):
    form = await request.form()
    p = {k: str(form.get(k, "")) for k in (
        "client_id","redirect_uri","response_type","scope","state",
        "code_challenge","code_challenge_method","resource"
    )}
    expected = os.environ.get("MCP_OAUTH_PASSWORD", "")
    supplied = str(form.get("password", ""))
    if not expected or not hmac.compare_digest(supplied, expected):
        return HTMLResponse("Password non valida", status_code=401)
    failure = _authorization_request_failure(p)
    if failure is not None:
        _reject(p["client_id"], "authorize_post", failure)
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    if not await _valid_oauth_client(p["client_id"], p["redirect_uri"]):
        return JSONResponse({"error": "invalid_request"}, status_code=400)
    code = secrets.token_urlsafe(32)
    oauth_store.save_code(
        code,
        client_id=p["client_id"],
        redirect_uri=p["redirect_uri"],
        code_challenge=p["code_challenge"],
        resource=p["resource"],
        scope=p["scope"],
    )
    query = {"code": code, "iss": issuer()}
    if p["state"]:
        query["state"] = p["state"]
    return RedirectResponse(p["redirect_uri"] + "?" + urlencode(query), status_code=302)


def _token_payload(client_id: str, resource: str, scope: str):
    access = secrets.token_urlsafe(32)
    refresh = secrets.token_urlsafe(48)
    oauth_store.save_access_token(access, client_id=client_id, resource=resource, scope=scope)
    oauth_store.save_refresh_token(refresh, client_id=client_id, resource=resource, scope=scope)
    return {
        "access_token": access,
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": refresh,
        "scope": scope,
    }


async def token_endpoint(request: Request):
    form = await request.form()
    grant = str(form.get("grant_type", ""))
    client_id = str(form.get("client_id", ""))
    if grant == "authorization_code":
        code = str(form.get("code", ""))
        redirect_uri = str(form.get("redirect_uri", ""))
        verifier = str(form.get("code_verifier", ""))
        row = oauth_store.consume_code(code)
        if not row:
            return JSONResponse({"error":"invalid_grant"}, status_code=400)
        if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri:
            return JSONResponse({"error":"invalid_grant"}, status_code=400)
        if not hmac.compare_digest(_pkce_s256(verifier), row["code_challenge"]):
            return JSONResponse({"error":"invalid_grant"}, status_code=400)
        return JSONResponse(_token_payload(client_id, row["resource"], row["scope"]))
    if grant == "refresh_token":
        refresh = str(form.get("refresh_token", ""))
        row = oauth_store.rotate_refresh_token(refresh, client_id=client_id)
        if not row or row["client_id"] != client_id:
            return JSONResponse({"error":"invalid_grant"}, status_code=400)
        return JSONResponse(_token_payload(client_id, row["resource"], row["scope"]))
    return JSONResponse({"error":"unsupported_grant_type"}, status_code=400)
