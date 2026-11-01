"""Inbound-only Starlette shell of the signer service. No outbound client of any kind.

Routes
- GET  /healthz                         public, no state
- POST /v1/requests/order               HMAC (request:create)   MCP files an order REQUEST
- POST /v1/requests/cancel              HMAC (request:create)   MCP files a cancel REQUEST
- GET  /v1/requests/{request_id}        HMAC (request:read)     MCP reads the status
- GET  /approve/{request_id}            owner phone: PWA page, the same static bytes for any id (no data)
- POST /approve/{request_id}/unlock/options  pre-auth: opaque unlock challenge only (no order field)
- POST /approve/{request_id}/unlock     passkey assertion over it -> session cookie (HttpOnly, Secure,
                                        SameSite=Strict, Path=/approve/<id>, 60 s, single use)
- GET  /approve/{request_id}/challenge  session required: binding + display + order challenges (once)
- POST /approve/{request_id}/assertion  session required (consumed): order-bound passkey assertion
- GET  /enroll, POST /enroll/options, POST /enroll/finish   passkey enrolment with a one-time code
- GET  /static/<file>                   fixed allowlist of the PWA assets
The approval and enrolment routes never accept HMAC credentials as authority: only a passkey assertion
(or a one-time enrolment code read on the signer host) is.
Errors: {"error": "<FIXED_CODE>"}; no exception text, no stack trace, no secret.
"""
from __future__ import annotations

import json
import pathlib
from typing import Any

from starlette.applications import Starlette
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from . import LIVE_TRADING, SERVICE_MODE
from .auth import SCOPE_CREATE, SCOPE_READ, AuthError
from .service import _REQUEST_ID, ServiceError, SignerService

MAX_BODY_BYTES = 16 * 1024
SESSION_COOKIE = "fcb_approval"
STATIC = pathlib.Path(__file__).resolve().parent / "static"
STATIC_FILES = {"approve.js": "text/javascript", "enroll.js": "text/javascript", "common.js": "text/javascript",
                "style.css": "text/css", "manifest.webmanifest": "application/manifest+json"}
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'none'; script-src 'self'; style-src 'self'; connect-src 'self'; "
                               "manifest-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; "
                               "frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff", "Referrer-Policy": "no-referrer", "X-Frame-Options": "DENY",
    "Cache-Control": "no-store", "Cross-Origin-Opener-Policy": "same-origin",
}


def _json(body: Any, status: int = 200) -> JSONResponse:
    return JSONResponse(body, status_code=status, headers=SECURITY_HEADERS)


def _err(code: str, status: int) -> JSONResponse:
    return _json({"error": code}, status)


async def _body(request: Request) -> bytes:
    raw = b""
    async for chunk in request.stream():
        raw += chunk
        if len(raw) > MAX_BODY_BYTES:
            raise ServiceError("BODY_TOO_LARGE", 413)
    return raw


def _parse(raw: bytes) -> Any:
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        raise ServiceError("BODY_NOT_JSON", 400) from None


def build_app(service: SignerService) -> Starlette:
    def guarded(handler):
        async def endpoint(request: Request) -> Response:
            try:
                return await handler(request)
            except AuthError as exc:
                return _err(exc.code, exc.status)
            except ServiceError as exc:
                return _err(exc.code, exc.status)
            except Exception:  # never leak exception text
                return _err("INTERNAL_ERROR", 500)
        return endpoint

    async def authed(request: Request, scope: str):
        raw = await _body(request)
        path = request.url.path + (("?" + request.url.query) if request.url.query else "")
        key = service.auth.verify(method=request.method, path=path, body=raw, headers=request.headers, scope=scope)
        return key, raw

    async def healthz(request: Request) -> Response:
        return _json({"status": "ok", "mode": SERVICE_MODE, "live_trading": LIVE_TRADING,
                      "data_source": service._market.data_source})

    async def create_order(request: Request) -> Response:
        key, raw = await authed(request, SCOPE_CREATE)
        status, out = service.create_order_request(key, _parse(raw))
        return _json(out, status)

    async def create_cancel(request: Request) -> Response:
        key, raw = await authed(request, SCOPE_CREATE)
        status, out = service.create_cancel_request(key, _parse(raw))
        return _json(out, status)

    async def read_request(request: Request) -> Response:
        key, _ = await authed(request, SCOPE_READ)
        return _json(service.status(key, request.path_params["request_id"]))

    def page(name: str) -> Response:
        return Response((STATIC / name).read_bytes(), media_type="text/html; charset=utf-8", headers=SECURITY_HEADERS)

    async def approve_page(request: Request) -> Response:
        # No lookup: identical bytes for every well-formed id, so the page is not an oracle on existence.
        if not _REQUEST_ID.fullmatch(request.path_params["request_id"]):
            raise ServiceError("NOT_FOUND", 404)
        return page("approve.html")

    async def unlock_options(request: Request) -> Response:
        return _json(service.unlock_options(request.path_params["request_id"]))

    async def unlock(request: Request) -> Response:
        rid = request.path_params["request_id"]
        token, expires = service.unlock(rid, _parse(await _body(request)))
        resp = _json({"unlocked": True, "expires_at": expires})
        resp.set_cookie(SESSION_COOKIE, token, max_age=60, path=f"/approve/{rid}", secure=True, httponly=True,
                        samesite="strict")
        return resp

    async def approve_challenge(request: Request) -> Response:
        return _json(service.approval_view(request.path_params["request_id"], request.cookies.get(SESSION_COOKIE)))

    async def approve_assertion(request: Request) -> Response:
        rid = request.path_params["request_id"]
        token = request.cookies.get(SESSION_COOKIE)
        try:
            raw = await _body(request)
        except ServiceError:
            service.submit_assertion(rid, None, token)  # consume / refuse the session first, then report
            raise
        try:
            body = _parse(raw)
        except ServiceError:
            body = None  # a locked request must answer APPROVAL_LOCKED, not a parse error
        resp = _json(service.submit_assertion(rid, body, token))
        resp.delete_cookie(SESSION_COOKIE, path=f"/approve/{rid}", secure=True, httponly=True, samesite="strict")
        return resp

    async def enroll_page(request: Request) -> Response:
        return page("enroll.html")

    async def enroll_options(request: Request) -> Response:
        return _json(service.enroll_options(_parse(await _body(request))))

    async def enroll_finish(request: Request) -> Response:
        return _json(service.enroll_finish(_parse(await _body(request))))

    async def static(request: Request) -> Response:
        name = request.path_params["name"]
        if name not in STATIC_FILES:
            raise ServiceError("NOT_FOUND", 404)
        return Response((STATIC / name).read_bytes(), media_type=STATIC_FILES[name], headers=SECURITY_HEADERS)

    routes = [
        Route("/healthz", guarded(healthz), methods=["GET"]),
        Route("/v1/requests/order", guarded(create_order), methods=["POST"]),
        Route("/v1/requests/cancel", guarded(create_cancel), methods=["POST"]),
        Route("/v1/requests/{request_id}", guarded(read_request), methods=["GET"]),
        Route("/approve/{request_id}", guarded(approve_page), methods=["GET"]),
        Route("/approve/{request_id}/unlock/options", guarded(unlock_options), methods=["POST"]),
        Route("/approve/{request_id}/unlock", guarded(unlock), methods=["POST"]),
        Route("/approve/{request_id}/challenge", guarded(approve_challenge), methods=["GET"]),
        Route("/approve/{request_id}/assertion", guarded(approve_assertion), methods=["POST"]),
        Route("/enroll", guarded(enroll_page), methods=["GET"]),
        Route("/enroll/options", guarded(enroll_options), methods=["POST"]),
        Route("/enroll/finish", guarded(enroll_finish), methods=["POST"]),
        Route("/static/{name}", guarded(static), methods=["GET"]),
    ]

    async def not_found(request: Request, exc: Exception) -> Response:
        return _err("NOT_FOUND", 404)

    async def not_allowed(request: Request, exc: Exception) -> Response:
        return _err("METHOD_NOT_ALLOWED", 405)

    async def internal(request: Request, exc: Exception) -> Response:
        return _err("INTERNAL_ERROR", 500)

    # DNS rebinding: refuse any Host header outside the configured allowlist (loopback by default).
    middleware = [Middleware(TrustedHostMiddleware, allowed_hosts=list(service.config.allowed_hosts))]
    return Starlette(routes=routes, middleware=middleware,
                     exception_handlers={404: not_found, 405: not_allowed, 500: internal})
