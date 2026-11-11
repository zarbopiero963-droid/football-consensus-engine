"""Authenticated end-to-end certification of the REAL deployed MCP bridge (Render).

Run only by .github/workflows/fcb-mcp-e2e.yml (environment mcp-e2e, owner dispatch from main).
It plays a public PKCE client against the pinned production origin:

  healthz + discovery -> /mcp negatives (no token, malformed, unknown bearer)
  -> DCR registration (+ refused foreign redirect) -> consent page headers
  -> one wrong password (401) -> code #1: wrong PKCE verifier burns it (+ replay refused)
  -> code #2: token exchange (+ replay refused)
  -> MCP initialize, tools/list (paginated, == /healthz count), tools/call on an allowlist of
     zero-cost local tools only (no provider request, no browser, no order)
  -> refresh: wrong client refused, rotation, old refresh replay refused, new access works.

Secrets: the password is read from the environment once and sent only in the consent POST body
to the pinned HTTPS origin. Codes, verifiers, tokens and the client id are masked in the Actions
log as soon as they exist, are never printed, and the report is leak-checked against all of
them before it is written. The report holds fixed outcomes and shapes only. No artifact.

The authorization redirect goes to Claude's callback URL; it is read from the Location header
and never followed, so nothing is sent to claude.ai.

HTTP uses the standard library only (no third-party package runs next to the password) and
never follows redirects, nor uses a proxy from the environment.

What the run proves and what it does not: `network_calls_made == 0` is what the two health tools
REPORT (their code makes no provider request; that rests on code review); the refresh-token-as-
bearer check is decided by the bearer format check before any database lookup.

Opt-in API-FOOTBALL suites (`--suite apifootball`, `apifootball_full` or `apifootball_completion`,
owner dispatch only): after the health tools, read-only apifootball_* tools are called through the
same authenticated MCP session under a hard per-campaign provider budget (default and ceiling 45,
every call charged its worst case). See the section
"API-FOOTBALL real-data suite" below. The default `health` suite is unchanged.

Production leftovers per run (no revocation endpoint yet, WP-022): one DCR client row (730-day
TTL), two used authorization codes, two access tokens (1 h) and one unrevoked refresh token
(30 days) whose plaintext existed only in the ephemeral runner's memory. One consent failure is
recorded against the runner's address (5 per 15 min per address, 50 per hour globally) and two
registrations (10 per hour per address, 100 globally).
"""
from __future__ import annotations

import argparse
import base64
from datetime import datetime, timedelta, timezone
import hashlib
import http.client
import json
import os
import re
import secrets
import sys
import time
from typing import Any, Callable
import urllib.error
import urllib.request
from urllib.parse import parse_qs, urlencode, urlparse


PINNED_BASE = "https://football-consensus-browser-bridge.onrender.com"
CLAUDE_CALLBACK = "https://claude.ai/api/mcp/auth_callback"
PROTOCOL_VERSION = "2025-06-18"
# Zero-cost, local-only tools: both report network_calls_made == 0 and touch no provider,
# browser or wallet. Nothing outside this set is ever called.
ALLOWED_TOOL_CALLS = frozenset({"sxbet_health", "apifootball_health"})
ISSUED_TOKEN_RE = re.compile(r"^[A-Za-z0-9_-]{43}$")
SAFE_TOKEN_RE = re.compile(r"^[A-Za-z0-9._~-]{1,512}$")
COMMIT_PREFIX_RE = re.compile(r"^[0-9a-f]{12}$")
USER_AGENT = "fcb-mcp-e2e/1"


class Response:
    """The small subset of an HTTP response the run needs (same shape as httpx.Response)."""

    def __init__(self, status_code: int, headers, body: bytes):
        self.status_code, self.headers, self._body = status_code, headers, body

    @property
    def text(self) -> str:
        return self._body.decode("utf-8", errors="replace")

    def json(self) -> Any:
        return json.loads(self._body)


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # a 3xx is returned to the caller, never followed
        return None


class StdlibClient:
    """Standard-library HTTPS client: no third-party package runs next to the password
    (review P2-1), and redirects are NEVER followed, so the consent 302 to claude.ai is only
    parsed from its Location header (review P2-2). No proxy from the environment is used."""

    follow_redirects = False

    def __init__(self, timeout: float = 30.0):
        self.timeout = timeout
        self._opener = urllib.request.build_opener(_NoRedirect(), urllib.request.ProxyHandler({}))

    def _send(self, method: str, url: str, headers=None, data=None, json=None) -> Response:
        hdrs = {"User-Agent": USER_AGENT, **(headers or {})}
        body = None
        if json is not None:
            body = _json_dumps(json).encode()
            hdrs.setdefault("Content-Type", "application/json")
        elif data is not None:
            body = urlencode(data).encode()
            hdrs.setdefault("Content-Type", "application/x-www-form-urlencoded")
        req = urllib.request.Request(url, data=body, headers=hdrs, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout) as r:
                return Response(r.status, r.headers, r.read())
        except urllib.error.HTTPError as e:  # 3xx/4xx/5xx: a response, not an error
            try:
                return Response(e.code, e.headers, e.read())
            finally:
                e.close()

    def get(self, url: str, headers=None) -> Response:
        return self._send("GET", url, headers=headers)

    def post(self, url: str, headers=None, data=None, json=None) -> Response:
        return self._send("POST", url, headers=headers, data=data, json=json)

    def close(self) -> None:
        pass


_json_dumps = json.dumps


class Fail(Exception):
    """A check failed; the message is a fixed code, never a server or exception text."""


class Run:
    def __init__(self, client: Any, base: str, password: str, *,
                 expected_commit: str | None, mask: Callable[[str], None], sleep=time.sleep,
                 expected_db_identity: str | None = None, suite: str = "health",
                 apifootball_budget: int = 0, now: Callable[[], float] = time.time):
        self.c, self.base, self._password = client, base.rstrip("/"), password
        self.expected_commit = expected_commit
        self.expected_db_identity = expected_db_identity
        self._mask, self._sleep, self._now = mask, sleep, now
        if suite not in SUITES:
            raise ValueError("unknown suite")
        self.suite = suite
        # The default suite keeps the zero-cost allowlist; the opt-in suites add only the
        # read-only provider tools, each call of which needs an open budget reservation.
        self.allowed_tools = ALLOWED_TOOL_CALLS | (frozenset(APIFOOTBALL_PROVIDER_TOOLS) if suite != "health" else frozenset())
        self.af_budget = (ProviderBudget(apifootball_budget, SUITE_BUDGET_CEILING[suite])
                          if suite != "health" else None)
        self.af_suite: ApiFootballSuite | None = None
        self.sensitive: list[str] = [password]
        self.results: list[dict[str, Any]] = []
        self.facts: dict[str, Any] = {}
        self._rpc_id = 0
        self.tools_called: list[str] = []

    # ---- bookkeeping ---------------------------------------------------------------------
    def secret(self, value: str) -> str:
        if value:
            self.sensitive.append(value)
            # Values come from the server: mask only well-formed URL-safe tokens, so a malformed
            # value (e.g. with a newline) can never inject an Actions workflow command.
            if not SAFE_TOKEN_RE.fullmatch(value):
                raise Fail("unexpected_secret_format")
            self._mask(value)
        return value

    def check(self, name: str, ok: bool, **info: Any) -> None:
        self.results.append({"check": name, "outcome": "PASS" if ok else "FAIL", **info})
        if not ok:
            raise Fail(name)

    # ---- HTTP helpers --------------------------------------------------------------------
    def _url(self, path: str) -> str:
        return self.base + path

    def get(self, path: str, **kw) -> Any:
        return self.c.get(self._url(path), **kw)

    def post(self, path: str, **kw) -> Any:
        return self.c.post(self._url(path), **kw)

    def rpc(self, method: str, params: dict[str, Any] | None, token: str | None,
            notification: bool = False) -> Any:
        if method == "tools/call" and (params or {}).get("name") not in self.allowed_tools:
            raise Fail("tool_not_allowlisted")  # enforced for every path, not only call_tool()
        if method == "tools/call" and params["name"] in APIFOOTBALL_PROVIDER_TOOLS:
            # A provider tool runs only inside its own open budget reservation (hard stop).
            if self.af_budget is None or self.af_budget.open is None or self.af_budget.open[0] != params["name"]:
                raise Fail("provider_call_without_reservation")
        if method == "tools/call":
            self.tools_called.append(params["name"])
        body: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            body["params"] = params
        if not notification:
            self._rpc_id += 1
            body["id"] = self._rpc_id
        headers = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
                   "MCP-Protocol-Version": PROTOCOL_VERSION}
        if token is not None:
            headers["Authorization"] = "Bearer " + token
        return self.post("/mcp/", headers=headers, json=body)

    @staticmethod
    def rpc_result(r: Any) -> dict[str, Any] | None:
        if r.status_code != 200:
            return None
        ctype = r.headers.get("content-type", "")
        try:
            if ctype.startswith("text/event-stream"):
                for line in r.text.splitlines():
                    if line.startswith("data:"):
                        msg = json.loads(line[5:].strip())
                        if "result" in msg or "error" in msg:
                            return msg.get("result")
                return None
            return r.json().get("result")
        except (ValueError, AttributeError):
            return None

    # ---- PKCE / OAuth --------------------------------------------------------------------
    def pkce(self) -> tuple[str, str]:
        verifier = self.secret(secrets.token_urlsafe(48))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).decode().rstrip("=")
        return verifier, challenge

    def consent_form(self, client_id: str, challenge: str, state: str, password: str) -> dict[str, str]:
        return {"client_id": client_id, "redirect_uri": CLAUDE_CALLBACK, "response_type": "code",
                "scope": "browser:use offline_access", "state": state, "code_challenge": challenge,
                "code_challenge_method": "S256", "resource": self.base + "/mcp", "password": password}

    def authorize(self, client_id: str, challenge: str) -> str:
        state = self.secret(secrets.token_urlsafe(16))
        r = self.post("/oauth/authorize", data=self.consent_form(client_id, challenge, state, self._password))
        self.check("authorize_post_redirects", r.status_code == 302, http=r.status_code)
        loc = urlparse(r.headers.get("location", ""))
        q = parse_qs(loc.query)
        self.check("authorize_redirect_target_is_registered_callback",
                   f"{loc.scheme}://{loc.netloc}{loc.path}" == CLAUDE_CALLBACK)
        self.check("authorize_redirect_state_and_iss",
                   q.get("state") == [state] and q.get("iss") == [self.base])
        code = (q.get("code") or [""])[0]
        self.secret(code)
        self.check("authorize_code_present", len(code) >= 32)
        return code

    def token(self, data: dict[str, str]) -> Any:
        return self.post("/oauth/token", data=data)

    # ---- the run ---------------------------------------------------------------------------
    def healthz(self) -> dict[str, Any]:
        # Render free instances sleep: wake up with bounded retries (about 3 minutes in total).
        last = None
        for attempt in range(12):
            try:
                r = self.get("/healthz")
                if r.status_code == 200:
                    return r.json()
                last = r.status_code
            except (OSError, ValueError) as exc:  # network errors, timeouts, non-JSON pages
                last = type(exc).__name__
            self._sleep(15)
        self.check("healthz_reachable", False, last=last)
        return {}

    def execute(self) -> None:
        h = self.healthz()
        self.facts.update({k: h.get(k) for k in (
            "deploy_commit", "mcp_tools_total", "mcp_tools_by_component", "browser_tools_enabled",
            "cloak_enabled", "schema_state", "oauth_password_configured")})
        self.check("healthz_ok", h.get("ok") is True and h.get("oauth_password_configured") is True)
        self.check("browser_and_cloak_off", h.get("browser_tools_enabled") is False and h.get("cloak_enabled") is False)
        if self.expected_commit:
            self.check("deployed_commit_is_main_head",
                       bool(COMMIT_PREFIX_RE.fullmatch(str(h.get("deploy_commit") or "")))
                       and self.expected_commit.startswith(str(h.get("deploy_commit"))),
                       deployed=h.get("deploy_commit"), expected=self.expected_commit[:12])

        prm = self.get("/.well-known/oauth-protected-resource").json()
        asm = self.get("/.well-known/oauth-authorization-server").json()
        self.check("discovery_metadata",
                   prm.get("resource") == self.base + "/mcp" and prm.get("authorization_servers") == [self.base]
                   and asm.get("issuer") == self.base and asm.get("code_challenge_methods_supported") == ["S256"]
                   and asm.get("token_endpoint") == self.base + "/oauth/token"
                   and asm.get("registration_endpoint") == self.base + "/oauth/register")

        # /mcp negatives: never 2xx, always the fixed 401 with resource metadata.
        for name, token in (("mcp_without_token_401", None),
                            ("mcp_malformed_bearer_401", "not-an-issued-token"),
                            ("mcp_unknown_wellformed_bearer_401", secrets.token_urlsafe(32))):
            r = self.rpc("tools/list", {}, token)
            self.check(name, r.status_code == 401 and "resource_metadata=" in r.headers.get("www-authenticate", ""),
                       http=r.status_code)

        # DCR: the foreign callback is refused; the Claude callback is registered.
        bad = self.post("/oauth/register", json={"redirect_uris": ["https://attacker.example/cb"],
                                                  "token_endpoint_auth_method": "none"})
        self.check("dcr_foreign_redirect_refused", bad.status_code == 400, http=bad.status_code)
        reg = self.post("/oauth/register", json={
            "client_name": "fcb-mcp-e2e", "redirect_uris": [CLAUDE_CALLBACK],
            "grant_types": ["authorization_code", "refresh_token"], "response_types": ["code"],
            "token_endpoint_auth_method": "none", "scope": "browser:use offline_access"})
        self.check("dcr_register_201", reg.status_code == 201, http=reg.status_code)
        body = reg.json()
        client_id = self.secret(str(body.get("client_id", "")))
        self.check("dcr_public_client_no_secret", client_id.startswith("fcbdcr1_") and "client_secret" not in body)

        # Consent page: served with the hardened headers.
        verifier1, challenge1 = self.pkce()
        query = {k: v for k, v in self.consent_form(client_id, challenge1, "probe", "").items() if k != "password"}
        page = self.get("/oauth/authorize?" + urlencode(query))
        csp = page.headers.get("content-security-policy", "")
        self.check("consent_page_headers",
                   page.status_code == 200 and "frame-ancestors 'none'" in csp and "form-action" not in csp
                   and page.headers.get("x-frame-options") == "DENY"
                   and "no-store" in page.headers.get("cache-control", ""), http=page.status_code)

        # One wrong password: 401, no code (counts once against the consent failure window).
        wrong = self.post("/oauth/authorize", data=self.consent_form(
            client_id, challenge1, "w", "wrong-" + secrets.token_urlsafe(12)))
        self.check("consent_wrong_password_401", wrong.status_code == 401 and "location" not in wrong.headers,
                   http=wrong.status_code)

        # Code #1: a wrong PKCE verifier is refused AND burns the code.
        code1 = self.authorize(client_id, challenge1)
        other_verifier, _ = self.pkce()
        r = self.token({"grant_type": "authorization_code", "code": code1, "client_id": client_id,
                        "redirect_uri": CLAUDE_CALLBACK, "code_verifier": other_verifier})
        self.check("token_wrong_verifier_invalid_grant", r.status_code == 400 and r.json() == {"error": "invalid_grant"})
        r = self.token({"grant_type": "authorization_code", "code": code1, "client_id": client_id,
                        "redirect_uri": CLAUDE_CALLBACK, "code_verifier": verifier1})
        self.check("token_code_burned_after_failed_binding", r.status_code == 400)

        # Code #2: the real exchange, then replay is refused.
        verifier2, challenge2 = self.pkce()
        code2 = self.authorize(client_id, challenge2)
        exchange = {"grant_type": "authorization_code", "code": code2, "client_id": client_id,
                    "redirect_uri": CLAUDE_CALLBACK, "code_verifier": verifier2}
        r = self.token(exchange)
        tok = r.json() if r.status_code == 200 else {}
        access = self.secret(str(tok.get("access_token") or ""))
        refresh = self.secret(str(tok.get("refresh_token") or ""))
        self.check("token_exchange_200", r.status_code == 200 and tok.get("token_type") == "Bearer"
                   and "browser:use" in str(tok.get("scope", "")).split()
                   and "no-store" in r.headers.get("cache-control", ""), http=r.status_code)
        self.check("access_token_issued_format", bool(ISSUED_TOKEN_RE.fullmatch(access)) and bool(refresh))
        r = self.token(exchange)
        self.check("token_code_replay_refused", r.status_code == 400)

        # MCP with the real token.
        init = self.rpc("initialize", {"protocolVersion": PROTOCOL_VERSION, "capabilities": {},
                                       "clientInfo": {"name": "fcb-mcp-e2e", "version": "1"}}, access)
        res = self.rpc_result(init) or {}
        self.check("mcp_initialize", init.status_code == 200 and bool(res.get("serverInfo"))
                   and "tools" in (res.get("capabilities") or {}), http=init.status_code)
        self.facts["protocol_version"] = res.get("protocolVersion")
        note = self.rpc("notifications/initialized", None, access, notification=True)
        self.check("mcp_initialized_notification_accepted", note.status_code in (200, 202), http=note.status_code)

        names: list[str] = []
        cursor = None
        for _ in range(20):
            r = self.rpc("tools/list", {"cursor": cursor} if cursor else {}, access)
            page_res = self.rpc_result(r)
            self.check("mcp_tools_list_page", page_res is not None and isinstance(page_res.get("tools"), list),
                       http=r.status_code)
            names += [t.get("name") for t in page_res["tools"]]
            cursor = page_res.get("nextCursor")
            if not cursor:
                break
        self.facts["tools_listed"] = len(names)
        self.check("mcp_tools_list_matches_healthz",
                   len(names) == len(set(names)) == h.get("mcp_tools_total") and len(names) > 0,
                   listed=len(names), healthz=h.get("mcp_tools_total"))
        self.check("mcp_allowlisted_tools_present", ALLOWED_TOOL_CALLS <= set(names))

        sx = self.call_tool("sxbet_health", access)
        self.check("tools_call_sxbet_health_read_only",
                   sx.get("network_calls_made") == 0 and sx.get("mode") == "READ_ONLY"
                   and (sx.get("available_modes") or {}).get("LIVE_TRADING") == "UNAVAILABLE"
                   and sx.get("write_tools") == [])
        af = self.call_tool("apifootball_health", access)
        self.check("tools_call_apifootball_health_no_provider_call", af.get("network_calls_made") == 0)
        self.facts["apifootball_quota_coordinator"] = af.get("quota_coordinator")
        # FCB-WP-009 (R2): the database identity the deployed runtime uses (role names only).
        ident = af.get("database_identity") if isinstance(af.get("database_identity"), dict) else {}
        self.facts["database_identity"] = {k: ident.get(k) for k in ("status", "current_user", "session_user")
                                           if isinstance(ident.get(k), str) and len(ident.get(k)) <= 63}
        if self.expected_db_identity:
            self.check("database_identity_is_expected",
                       ident.get("status") == "OK" and ident.get("current_user") == self.expected_db_identity
                       and ident.get("session_user") == self.expected_db_identity,
                       expected=self.expected_db_identity, observed=self.facts["database_identity"])

        if self.suite != "health":
            self.run_apifootball(access, af)

        # Refresh: wrong client refused (and does not burn), rotation, replay refused.
        r = self.token({"grant_type": "refresh_token", "refresh_token": refresh, "client_id": "fcbdcr1_" + "z" * 32})
        self.check("refresh_wrong_client_refused", r.status_code == 400)
        r = self.token({"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id})
        rot = r.json() if r.status_code == 200 else {}
        access2 = self.secret(str(rot.get("access_token") or ""))
        refresh2 = rot.get("refresh_token")
        refresh2 = self.secret(refresh2) if isinstance(refresh2, str) else ""
        self.check("refresh_rotation_200", r.status_code == 200 and bool(ISSUED_TOKEN_RE.fullmatch(access2))
                   and access2 != access and bool(refresh2) and refresh2 != refresh, http=r.status_code)
        r = self.token({"grant_type": "refresh_token", "refresh_token": refresh, "client_id": client_id})
        self.check("refresh_replay_refused", r.status_code == 400)
        r = self.rpc("tools/list", {}, refresh)
        self.check("refresh_token_is_not_a_bearer", r.status_code == 401, http=r.status_code)
        r = self.rpc("tools/list", {}, access2)
        self.check("refreshed_access_token_works", self.rpc_result(r) is not None, http=r.status_code)

    def call_tool(self, name: str, token: str) -> dict[str, Any]:
        if name not in ALLOWED_TOOL_CALLS:
            raise Fail("tool_not_allowlisted")
        r = self.rpc("tools/call", {"name": name, "arguments": {}}, token)
        res = self.rpc_result(r) or {}
        if res.get("isError"):
            return {}
        payload = res.get("structuredContent")
        if payload is None:
            try:
                payload = json.loads(res["content"][0]["text"])
            except (KeyError, IndexError, TypeError, ValueError):
                payload = {}
        return payload if isinstance(payload, dict) else {}

    def call_tool_payload(self, name: str, arguments: dict[str, Any], token: str) -> dict[str, Any] | None:
        """The tool's structured payload, or None when the call itself failed (no reply, isError)."""
        if name not in self.allowed_tools:
            raise Fail("tool_not_allowlisted")
        try:
            r = self.rpc("tools/call", {"name": name, "arguments": arguments}, token)
            res = self.rpc_result(r)
        except (OSError, ValueError, http.client.HTTPException):  # timeouts, resets: never the message
            return None
        if not isinstance(res, dict) or res.get("isError"):
            return None
        payload = res.get("structuredContent")
        if payload is None:
            try:
                payload = json.loads(res["content"][0]["text"])
            except (KeyError, IndexError, TypeError, ValueError):
                payload = None
        return payload if isinstance(payload, dict) else None

    def run_apifootball(self, token: str, health: dict[str, Any]) -> None:
        """The opt-in provider suite; it never raises on provider outcomes, it records them."""
        ready = (health.get("status") == "LIVE_ENABLED" and health.get("provider_requests_authorized") is True
                 and health.get("quota_coordinator") == "READY")
        self.af_suite = ApiFootballSuite(lambda tool, args: self.call_tool_payload(tool, args, token),
                                         self.af_budget, plan=self.suite,
                                         now=self._now, sleep=self._sleep)
        self.af_suite.run(None if ready else "BLOCKED_PROVIDER_NOT_LIVE_OR_COORDINATOR_NOT_READY")

    def apifootball_report(self) -> dict[str, Any] | None:
        if self.suite == "health":
            return None
        if self.af_suite is None:  # the OAuth/MCP part failed first: no provider call was made
            return {"suite": self.suite, "verdict": "BLOCKED", "stop_reason": "NOT_REACHED",
                    "provider_requests_charged": self.af_budget.committed, "cases": []}
        rep = self.af_suite.report()
        if len(json.dumps(rep, sort_keys=True).encode("utf-8")) > SUITE_REPORT_MAX_BYTES:
            # Shapes and counts only: an oversized section means something raw slipped in.
            rep = {"suite": self.suite, "verdict": "FAIL", "stop_reason": "SUITE_REPORT_TOO_LARGE",
                   "provider_requests_charged": self.af_budget.committed, "cases": []}
        return rep

    def report(self, error: str | None) -> dict[str, Any]:
        fails = [r["check"] for r in self.results if r["outcome"] == "FAIL"]
        out = {"target": self.base, "verdict": "PASS" if not error and not fails else "FAIL",
               "failed_check": error, "checks_passed": sum(r["outcome"] == "PASS" for r in self.results),
               "checks": self.results, "facts": self.facts, "suite": self.suite,
               "tools_called": list(self.tools_called),
               # Upper bound of real provider HTTP attempts: every provider call charged its worst case.
               "provider_or_order_calls_attempted": self.af_budget.committed if self.af_budget else 0,
               # The OAuth flow itself writes rows in production (DCR client, codes, tokens):
               "oauth_rows_written_per_run": "1 DCR client, 2 codes, 2 token pairs (hashes only)"}
        af = self.apifootball_report()
        if af is not None:
            out["apifootball_suite"] = af
        return out

    def leak_free(self, text: str) -> bool:
        # Raw and JSON-escaped forms: a value with quotes, backslashes or non-ASCII characters
        # appears escaped inside json.dumps output.
        forms = {f for s in self.sensitive if s for f in (s, json.dumps(s)[1:-1])}
        return not any(f in text for f in forms)


# ==== API-FOOTBALL real-data suite (owner decision "path B": opt-in, budgeted, read-only) ========
#
# Selected only by `--suite apifootball` (prioritized plan), `--suite apifootball_full` (one call
# per provider tool) or `--suite apifootball_completion` (only what the production runs on 37e74fd
# left unproven, reusing their PASS evidence); the default `health` suite is the run above,
# unchanged. Every provider call
# goes through the deployed MCP tools, hence through the server's shared PostgreSQL coordinator:
# the runner itself never contacts the provider and holds no API key and no database URL.
#
# Budget (owner decision "API-FOOTBALL REAL E2E COMPLETION"): one global, fail-closed ceiling of
# provider HTTP attempts per campaign (= one workflow run). Before each tools/call the runner
# reserves the WORST CASE that call can cost on the server, derived from the code:
#   bridge/api_football/resilience.py RetryPolicy: max_attempts <= 3 (validated), one coordinator
#   lease and at most one provider request per attempt; tools.py: one page per call, no automatic
#   pagination, no follow-up request for the odds manifest.  =>  3 per tools/call.
# A call is refused when charged + 3 would exceed the budget. Conservative counting: a call that
# can reach the provider is ALWAYS charged the full 3, so the charge bounds the real HTTP attempts,
# retries included. The charge is 0 only on code paths that provably make no request: the tool
# statuses INVALID_INPUT, KEY_NOT_CONFIGURED, LIVE_NOT_AUTHORIZED, PROVIDER_CLIENT_UNAVAILABLE
# (returned by tools.py before the client is called) and a cache HIT (resilience.py returns the
# cached copy before any lease or request). The provider's own daily counter (quota_remaining.day)
# is recorded as information only: real runs showed it is not monotonic between consecutive calls
# (eventually consistent), so it never lowers a charge and its jumps never stop the run.
# network_calls_made is recorded but never used either: it now reports the server's real attempts
# (retries included), but it is the server's own claim, so the worst case stays charged.
APIFOOTBALL_BUDGET_CEILING = 45  # owner-approved max HTTP attempts per campaign; raised only by an owner-merged PR
APIFOOTBALL_DEFAULT_BUDGET = 45
# The owner approved 45 for the `apifootball_completion` campaign only; the earlier suites keep
# their owner-approved 20 (now charged at the worst case on every call).
SUITE_BUDGET_CEILING = {"apifootball": 20, "apifootball_full": 20, "apifootball_completion": 45}
APIFOOTBALL_MAX_ATTEMPTS_PER_CALL = 3  # resilience.RetryPolicy upper bound
APIFOOTBALL_PAGES_PER_CALL = 1  # no automatic pagination in tools.py
APIFOOTBALL_WORST_CASE_PER_CALL = APIFOOTBALL_MAX_ATTEMPTS_PER_CALL * APIFOOTBALL_PAGES_PER_CALL
# Runtime coordinator default: 8 requests per rolling minute. 9 s between calls keeps the run at
# <= 7 per minute without retries; with retries the coordinator refuses and the run stops.
APIFOOTBALL_CALL_SPACING_SECONDS = 9.0
SUITES = ("health", "apifootball", "apifootball_full", "apifootball_completion")
APIFOOTBALL_SUITES = SUITES[1:]
# The 40 read-only provider tools (GET only; bridge/api_football/contracts.py ENDPOINTS, pinned by
# a test). apifootball_health and apifootball_quota_report make no provider request.
APIFOOTBALL_PROVIDER_TOOLS: dict[str, str] = {
    "apifootball_status": "/status", "apifootball_countries": "/countries",
    "apifootball_leagues": "/leagues", "apifootball_seasons": "/leagues/seasons",
    "apifootball_fixtures": "/fixtures", "apifootball_fixture_statistics": "/fixtures/statistics",
    "apifootball_fixture_events": "/fixtures/events", "apifootball_fixture_lineups": "/fixtures/lineups",
    "apifootball_teams": "/teams", "apifootball_team_statistics": "/teams/statistics",
    "apifootball_players": "/players", "apifootball_injuries": "/injuries",
    "apifootball_predictions": "/predictions", "apifootball_odds": "/odds",
    "apifootball_live_odds": "/odds/live", "apifootball_bookmakers": "/odds/bookmakers",
    "apifootball_bets": "/odds/bets", "apifootball_coverage_report": "/leagues",
    "apifootball_timezones": "/timezone", "apifootball_team_seasons": "/teams/seasons",
    "apifootball_team_countries": "/teams/countries", "apifootball_venues": "/venues",
    "apifootball_standings": "/standings", "apifootball_fixture_rounds": "/fixtures/rounds",
    "apifootball_fixture_headtohead": "/fixtures/headtohead", "apifootball_fixture_players": "/fixtures/players",
    "apifootball_coaches": "/coachs", "apifootball_player_seasons": "/players/seasons",
    "apifootball_player_profiles": "/players/profiles", "apifootball_player_squads": "/players/squads",
    "apifootball_player_teams": "/players/teams", "apifootball_top_scorers": "/players/topscorers",
    "apifootball_top_assists": "/players/topassists", "apifootball_top_yellow_cards": "/players/topyellowcards",
    "apifootball_top_red_cards": "/players/topredcards", "apifootball_transfers": "/transfers",
    "apifootball_trophies": "/trophies", "apifootball_sidelined": "/sidelined",
    "apifootball_odds_mapping": "/odds/mapping", "apifootball_live_bets": "/odds/live/bets",
}
# League of the repository's existing route inventory (scripts/api_football_39_route_inventory.py).
# Every other id (season, fixtures, teams, bet, player, venue) is read from a response of this run.
PROBE_LEAGUE = 39
# PASS evidence of the production runs on main 37e74fd (suites apifootball / apifootball_full), reused
# by `apifootball_completion` instead of repeating those calls: /leagues id=39 current=true -> season
# 2026 (unique current season); /fixtures id=1557464 -> FT 4-4, HT 2-2, with events and statistics.
EVIDENCE_SEASON = 2026
EVIDENCE_FINISHED_FIXTURE = 1557464
UPCOMING_WINDOW_DAYS = 28
# Negative case: the contract's own upper bound for an id (contracts.py _positive_int).
NONEXISTENT_FIXTURE_ID = 2_000_000_000
OVER_UNDER_BET_NAME = "Goals Over/Under"  # exact catalog name, never a fuzzy match
FINISHED = frozenset({"FT", "AET", "PEN"})
NO_NETWORK_STATUSES = frozenset({"INVALID_INPUT", "KEY_NOT_CONFIGURED", "LIVE_NOT_AUTHORIZED",
                                 "PROVIDER_CLIENT_UNAVAILABLE"})
COORDINATOR_CODES = frozenset({"LOCAL_CONCURRENCY_BUSY", "QUOTA_COORDINATOR_UNAVAILABLE",
                               "QUOTA_RELEASE_FAILED", "QUOTA_RECONCILIATION_FAILED"})
# Known and non-stopping: a body above the transport's read limit (F-03); that page is lost.
KNOWN_SIZE_CODES = frozenset({"PROVIDER_RESPONSE_TOO_LARGE", "PROVIDER_RESPONSE_TOO_LARGE_FOR_SCAN"})
SUITE_REPORT_MAX_BYTES = 131_072
_NAME_RE = re.compile(r"[^\w .'&()/-]")
_CODE_RE = re.compile(r"^[A-Z0-9_]{1,64}$")
_RAW_TIME_RE = re.compile(r"^[0-9T:+.Z-]{10,32}$")


class BudgetExceeded(Exception):
    """A reservation would exceed the per-campaign provider budget."""


class ProviderBudget:
    """Global, fail-closed ceiling of provider HTTP attempts: every call that can reach the
    provider is charged its worst case; 0 only on a code path that provably makes no request."""

    def __init__(self, budget: int, ceiling: int = APIFOOTBALL_BUDGET_CEILING):
        if type(ceiling) is not int or not 1 <= ceiling <= APIFOOTBALL_BUDGET_CEILING:
            raise ValueError("ceiling above the owner-approved maximum")
        if type(budget) is not int or not 1 <= budget <= ceiling:
            raise ValueError("budget outside the owner-approved ceiling")
        self.budget, self.ceiling = budget, ceiling
        self.committed = 0
        self.open: tuple[str, int] | None = None
        # Information only (never used to lower a charge or to stop): the provider's daily counter
        # as read after each charged call, in call order.
        self.day_readings: list[int] = []

    def can_reserve(self, worst: int = APIFOOTBALL_WORST_CASE_PER_CALL) -> bool:
        return self.open is None and self.committed + worst <= self.budget

    def reserve(self, tool: str, worst: int = APIFOOTBALL_WORST_CASE_PER_CALL) -> int:
        if not self.can_reserve(worst):
            raise BudgetExceeded(tool)
        self.open = (tool, worst)
        return worst

    def settle(self, payload: dict[str, Any] | None) -> tuple[int, str]:
        """Close the open reservation: (charged, basis). Never below the worst case unless the
        code path provably made no request."""
        if self.open is None:
            raise RuntimeError("no open reservation")
        _, worst = self.open
        self.open = None
        status = payload.get("status") if isinstance(payload, dict) else None
        if payload is None:
            charged, basis = worst, "WORST_CASE_NO_REPLY"
        elif status in NO_NETWORK_STATUSES:
            charged, basis = 0, "CODE_PATH_NO_REQUEST"
        elif payload.get("cache_status") == "HIT" and status in ("REAL_API_RESPONSE", "PARTIAL_ODDS_MANIFEST",
                                                                 "PARTIAL_ID_MANIFEST"):
            charged, basis = 0, "CACHE_HIT"
        else:
            charged, basis = worst, "WORST_CASE"
        self.committed += charged
        quota = payload.get("quota_remaining") if isinstance(payload, dict) else None
        day = quota.get("day") if isinstance(quota, dict) else None
        if charged and type(day) is int and day >= 0:
            self.day_readings.append(day)
        return charged, basis

    def counter_report(self) -> dict[str, Any]:
        r = self.day_readings
        return {"use": "INFORMATIONAL_ONLY_NEVER_USED_FOR_ACCOUNTING", "day_readings": r[:64],
                "monotonic_non_increasing": all(a >= b for a, b in zip(r, r[1:])) if len(r) > 1 else None}


def _code(value: Any) -> str | None:
    """Fixed server codes only (A-Z, 0-9, _): nothing free-form reaches the report or summary."""
    return value if isinstance(value, str) and _CODE_RE.fullmatch(value) else None


def _error_code(payload: dict[str, Any] | None) -> str | None:
    err = payload.get("error") if isinstance(payload, dict) else None
    return _code(err.get("code")) if isinstance(err, dict) else None


def _safe_name(value: Any) -> str | None:
    """Team/league names are untrusted provider text: bounded and markdown-inert in the summary."""
    return _NAME_RE.sub("", value)[:64] if isinstance(value, str) else None


def _is_int(v: Any) -> bool:
    return type(v) is int


def _sha256(obj: Any) -> str:
    return hashlib.sha256(json.dumps(obj, sort_keys=True, separators=(",", ":"),
                                     ensure_ascii=False).encode("utf-8")).hexdigest()


class Case:
    def __init__(self, cid: str, tool: str, args: Callable[[dict[str, Any]], dict[str, Any] | str],
                 expect: str, check: Callable[..., list[str]] | None = None, what: str = "",
                 coverage: str | None = None):
        if tool not in APIFOOTBALL_PROVIDER_TOOLS or expect not in ("data", "data_or_empty", "empty", "invalid_input"):
            raise ValueError("invalid case")
        self.cid, self.tool, self.args, self.expect, self.check, self.what = cid, tool, args, expect, check, what
        # The /leagues coverage flag that governs this data (fixtures.events, odds, standings...):
        # when it is false and the reply is empty, the verdict is PROVIDER_UNAVAILABLE with that
        # flag as evidence. An empty reply on its own is never evidence of unavailability.
        self.coverage = coverage


def _args(*keys: str, **build: Callable[[dict[str, Any]], Any]):
    """Arguments built from derived ids only; a missing id makes the case NOT_TESTED (S7)."""
    def make(ctx: dict[str, Any]) -> dict[str, Any] | str:
        missing = [k for k in keys if ctx.get(k) is None]
        if missing:
            return "NOT_DERIVABLE_" + "_".join(missing).upper()
        return {k: f(ctx) for k, f in build.items()}
    return make


def _response(payload: dict[str, Any]) -> list[Any]:
    data = payload.get("data")
    resp = data.get("response") if isinstance(data, dict) else None
    return resp if isinstance(resp, list) else []


# ---- per-case checks and deterministic selections (shapes, counts and ids only) ----------------

def _check_status(p, ctx, facts):
    data = p.get("data") if isinstance(p.get("data"), dict) else {}
    sub, req = data.get("subscription"), data.get("requests")
    if not isinstance(sub, dict) or not isinstance(req, dict):
        return ["STATUS_SHAPE_INVALID"]
    facts.update({"active": sub.get("active") if isinstance(sub.get("active"), bool) else None,
                  "requests_current": req.get("current") if _is_int(req.get("current")) else None,
                  "requests_limit_day": req.get("limit_day") if _is_int(req.get("limit_day")) else None})
    if not (_is_int(req.get("current")) and _is_int(req.get("limit_day"))):
        return ["STATUS_COUNTERS_NOT_INTEGERS"]
    ctx["provider_day_left"] = req["limit_day"] - req["current"]
    return []


def _check_league(p, ctx, facts):
    resp = _response(p)
    if len(resp) != 1 or not isinstance(resp[0], dict) or not isinstance(resp[0].get("league"), dict):
        return ["LEAGUE_NOT_UNIQUE"]
    league = resp[0]["league"]
    if league.get("id") != ctx["league"]:
        return ["LEAGUE_ID_MISMATCH"]
    seasons = [s for s in resp[0].get("seasons") or [] if isinstance(s, dict) and s.get("current") is True]
    facts.update({"league_name": _safe_name(league.get("name")), "current_seasons": len(seasons)})
    if len(seasons) != 1 or not _is_int(seasons[0].get("year")):
        return ["CURRENT_SEASON_NOT_UNIQUE"]  # ambiguity = no selection (CLAUDE.md 2.3bis)
    ctx["season"] = seasons[0]["year"]
    cov = seasons[0].get("coverage") if isinstance(seasons[0].get("coverage"), dict) else {}
    fx = cov.get("fixtures") if isinstance(cov.get("fixtures"), dict) else {}
    ctx["coverage"] = {k: v if isinstance(v, bool) else None for k, v in (
        ("events", fx.get("events")), ("statistics_fixtures", fx.get("statistics_fixtures")),
        ("standings", cov.get("standings")), ("odds", cov.get("odds")))}
    facts.update({"season": ctx["season"], "coverage": dict(ctx["coverage"])})
    return []


def _fixture_shape(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    fx, teams, goals = item.get("fixture"), item.get("teams"), item.get("goals")
    return (isinstance(fx, dict) and _is_int(fx.get("id")) and _is_int(fx.get("timestamp"))
            and isinstance(fx.get("status"), dict) and isinstance(fx["status"].get("short"), str)
            and isinstance(teams, dict)
            and all(isinstance(teams.get(s), dict) and _is_int(teams[s].get("id")) for s in ("home", "away"))
            and isinstance(goals, dict)
            and all(goals.get(s) is None or _is_int(goals.get(s)) for s in ("home", "away")))


def select_fixtures(resp: list[Any], now_ts: int) -> tuple[dict | None, dict | None]:
    """X: finished (FT/AET/PEN), most recent kickoff, ties -> lowest id.
    U: not started (NS), kickoff >= now + 2 h, nearest, ties -> lowest id."""
    ok = [f for f in resp if _fixture_shape(f)]
    done = [f for f in ok if f["fixture"]["status"]["short"] in FINISHED]
    todo = [f for f in ok if f["fixture"]["status"]["short"] == "NS" and f["fixture"]["timestamp"] >= now_ts + 7200]
    x = min(done, key=lambda f: (-f["fixture"]["timestamp"], f["fixture"]["id"]), default=None)
    u = min(todo, key=lambda f: (f["fixture"]["timestamp"], f["fixture"]["id"]), default=None)
    return x, u


def _fixture_fact(f: dict[str, Any]) -> dict[str, Any]:
    date = f["fixture"].get("date")
    return {"fixture": f["fixture"]["id"], "home": _safe_name(f["teams"]["home"].get("name")),
            "away": _safe_name(f["teams"]["away"].get("name")),
            # Raw provider time kept as published (UTC requested); anything else is dropped.
            "kickoff_raw": date if isinstance(date, str) and _RAW_TIME_RE.fullmatch(date) else None}


def _check_fixture_window(p, ctx, facts):
    resp = _response(p)
    x, u = select_fixtures(resp, ctx["now_ts"])
    facts.update({"fixtures": len(resp),
                  "finished": sum(_fixture_shape(f) and f["fixture"]["status"]["short"] in FINISHED for f in resp)})
    if x:
        venue = x["fixture"].get("venue")
        ctx.update(fixture_x=x["fixture"]["id"], team_home=x["teams"]["home"]["id"],
                   team_away=x["teams"]["away"]["id"],
                   venue=venue.get("id") if isinstance(venue, dict) and _is_int(venue.get("id")) else None)
        facts["X"] = _fixture_fact(x)
    if u:
        ctx["fixture_u"] = u["fixture"]["id"]
        facts["U"] = _fixture_fact(u)
    return ["FIXTURE_SHAPE_INVALID"] if any(not _fixture_shape(f) for f in resp) else []


def _check_upcoming(p, ctx, facts):
    """Completion: U only (earliest NS >= now + 2 h, ties -> lowest id); X is the fixed evidence id."""
    resp = _response(p)
    _, u = select_fixtures(resp, ctx["now_ts"])
    facts.update({"fixtures": len(resp),
                  "not_started": sum(_fixture_shape(f) and f["fixture"]["status"]["short"] == "NS" for f in resp)})
    if u:
        ctx["fixture_u"] = u["fixture"]["id"]
        facts["U"] = _fixture_fact(u)
    return ["FIXTURE_SHAPE_INVALID"] if any(not _fixture_shape(f) for f in resp) else []


def _check_fixture_detail(p, ctx, facts):
    resp = _response(p)
    if len(resp) != 1 or not _fixture_shape(resp[0]) or resp[0]["fixture"]["id"] != ctx["fixture_x"]:
        return ["FIXTURE_DETAIL_NOT_THE_SELECTED_FIXTURE"]
    f = resp[0]
    score = f.get("score")
    if not isinstance(score, dict) or not all(isinstance(score.get(k), dict) for k in ("halftime", "fulltime")):
        return ["SCORE_FIELDS_MISSING"]
    for k in ("halftime", "fulltime"):  # missing stays missing: null or an integer, never coerced
        if not all(score[k].get(s) is None or _is_int(score[k].get(s)) for s in ("home", "away")):
            return ["SCORE_VALUE_TYPE_INVALID"]
    facts.update({"status": _safe_name(f["fixture"]["status"]["short"])[:8],"goals": [f["goals"]["home"], f["goals"]["away"]],
                  "halftime_as_published": [score["halftime"].get("home"), score["halftime"].get("away")]})
    return []


def _check_events(p, ctx, facts):
    resp = _response(p)
    if not all(isinstance(e, dict) and isinstance(e.get("type"), str) and isinstance(e.get("time"), dict)
               and isinstance(e.get("team"), dict) for e in resp):
        return ["EVENT_SHAPE_INVALID"]
    types: dict[str, int] = {}
    for e in resp:
        key = _safe_name(e["type"])[:24]
        types[key] = types.get(key, 0) + 1
    facts.update({"events": len(resp), "by_type": dict(sorted(types.items())[:12]),
                  "goal_events": sum(e["type"] == "Goal" for e in resp)})
    return []


def _check_statistics(p, ctx, facts):
    resp = _response(p)
    nulls = values = 0
    for team in resp:
        stats = team.get("statistics") if isinstance(team, dict) else None
        if not isinstance(stats, list) or not isinstance(team.get("team"), dict):
            return ["STATISTICS_SHAPE_INVALID"]
        for s in stats:
            if not isinstance(s, dict) or not isinstance(s.get("type"), str) or "value" not in s:
                return ["STATISTIC_FIELD_MISSING"]
            v = s["value"]
            if not (v is None or isinstance(v, str) or (isinstance(v, (int, float)) and not isinstance(v, bool))):
                return ["STATISTIC_VALUE_TYPE_INVALID"]
            values += 1
            nulls += v is None
    facts.update({"teams": len(resp), "values": values, "null_values_kept_null": nulls})
    return []


def _check_catalog_single_page(p, ctx, facts):
    """Catalog rows are {id, name}. The provider publishes rows whose name is null (real run
    41242779991: /odds/bookmakers 33 rows, one with name null; /odds/bets 338, one null): a null
    stays null and is counted, it is not a shape defect (CLAUDE.md 2.3). A row without an integer
    id, without a `name` key (missing is not a published null), or with a name that is neither a
    string nor null, is."""
    resp = _response(p)
    rows_ok = [r for r in resp if isinstance(r, dict) and _is_int(r.get("id"))
               and "name" in r and (r["name"] is None or isinstance(r["name"], str))]
    total = (p.get("pagination") or {}).get("total")
    # Facts first: a defect must never leave the case without its counts.
    facts.update({"entries": len(resp), "paging_total": total,
                  "null_names_kept_null": sum(r.get("name") is None for r in rows_ok),
                  "rows_shape_invalid": len(resp) - len(rows_ok)})
    if len(rows_ok) != len(resp):
        return ["CATALOG_SHAPE_INVALID"]
    # No `page` parameter is documented for these catalogs: a second page would be unreachable.
    return ["CATALOG_HAS_UNREACHABLE_PAGES"] if total != 1 else []


def _check_bets(p, ctx, facts):
    defects = _check_catalog_single_page(p, ctx, facts)
    exact = sorted(r["id"] for r in _response(p)
                   if isinstance(r, dict) and r.get("name") == OVER_UNDER_BET_NAME and _is_int(r.get("id")))
    facts["over_under_bet_ids"] = exact[:5]
    if len(exact) == 1:  # exact, unique name only; ambiguity = no selection
        ctx["bet_ou"] = exact[0]
    return defects


def _odd_ok(v: Any) -> bool:
    """Selection label and decimal price are distinct published fields; the price stays a string."""
    if not isinstance(v, dict) or "value" not in v or not isinstance(v.get("odd"), str):
        return False
    try:
        return float(v["odd"]) >= 1.0
    except ValueError:
        return False


# A provider decimal price as published: digits with an optional fraction (no sign, exponent, nan).
_DECIMAL_RE = re.compile(r"^[0-9]{1,6}(?:\.[0-9]{1,6})?$")


def _live_odd(v: Any) -> str:
    """Live selection: OK, NULL (an explicitly published null `odd`, kept null), SUSPENDED_ZERO
    (odd "0" on a selection flagged suspended, as in the repo's live fixture
    tests/test_api_football_five_point_evidence.py) or INVALID. A missing `odd` or `value` key, or
    a non-null odd that is not a decimal string, stays INVALID."""
    if not isinstance(v, dict) or "value" not in v or "odd" not in v:
        return "INVALID"
    odd = v["odd"]
    if odd is None:
        return "NULL"
    if not isinstance(odd, str) or not _DECIMAL_RE.fullmatch(odd):
        return "INVALID"
    if float(odd) >= 1.0:
        return "OK"
    return "SUSPENDED_ZERO" if float(odd) == 0 and v.get("suspended") is True else "INVALID"


def _check_manifest(p, facts) -> list[str]:
    m = p.get("data") if isinstance(p.get("data"), dict) else {}
    ok = (m.get("partial") is True and m.get("prices_included") is False and isinstance(m.get("paging"), dict)
          and p.get("pagination") == m.get("paging") and _is_int(m.get("fixtures_in_page")))
    facts.update({"manifest": True, "fixtures_in_page": m.get("fixtures_in_page") if _is_int(m.get("fixtures_in_page")) else None,
                  "fixture_list_complete": m.get("fixture_list_complete") if isinstance(m.get("fixture_list_complete"), bool) else None})
    return [] if ok else ["ODDS_MANIFEST_NOT_EXPLICIT"]


def _check_odds(p, ctx, facts, *, live: bool = False, fixture_key: str | None = None):
    if p.get("status") == "PARTIAL_ODDS_MANIFEST":
        return _check_manifest(p, facts)
    resp = _response(p)
    over = under = selections = 0
    live_counts = {"null_odds_kept_null": 0, "suspended": 0, "suspended_zero_odds": 0, "open_priced": 0}

    def scan() -> str | None:
        nonlocal over, under, selections
        for item in resp:
            if not isinstance(item, dict) or not isinstance(item.get("fixture"), dict) or not _is_int(item["fixture"].get("id")):
                return "ODDS_FIXTURE_SHAPE_INVALID"
            if fixture_key and item["fixture"]["id"] != ctx[fixture_key]:
                return "ODDS_FOR_ANOTHER_FIXTURE"
            books = item.get("bookmakers")
            if live:
                markets = item.get("odds")
            elif isinstance(books, list) and all(isinstance(b, dict) and isinstance(b.get("bets"), list) for b in books):
                markets = [bet for b in books for bet in b["bets"]]
            else:
                markets = None
            if not isinstance(markets, list):
                return "ODDS_MARKETS_SHAPE_INVALID"
            for bet in markets:
                if not isinstance(bet, dict) or not isinstance(bet.get("values"), list):
                    return "ODDS_SELECTIONS_SHAPE_INVALID"
                for v in bet["values"]:
                    if live:
                        kind = _live_odd(v)
                        if kind == "INVALID":
                            return "ODDS_SELECTION_NOT_DISTINCT_DECIMAL_FIELDS"
                        live_counts["null_odds_kept_null"] += kind == "NULL"
                        live_counts["suspended_zero_odds"] += kind == "SUSPENDED_ZERO"
                        live_counts["suspended"] += v.get("suspended") is True
                        # Open AND priced only when the provider says so: a missing flag is not open.
                        live_counts["open_priced"] += v.get("suspended") is False and kind == "OK"
                    elif not _odd_ok(v):  # pre-match: unchanged strictness
                        return "ODDS_SELECTION_NOT_DISTINCT_DECIMAL_FIELDS"
                    selections += 1
                    if not live and ctx.get("bet_ou") is not None and bet.get("id") == ctx["bet_ou"]:
                        over += str(v["value"]).startswith("Over ")
                        under += str(v["value"]).startswith("Under ")
        return None

    defect = scan()
    # Facts before any defect: a failing case still shows what was counted up to it.
    facts.update({"fixtures": len(resp), "selections": selections})
    if live:
        facts.update(live_counts)
        if defect is None:
            facts["live_state"] = live_state(selections, live_counts)
    if not live and ctx.get("bet_ou") is not None and defect is None:
        # Both sides counted as PUBLISHED; an absent side is never rebuilt from the other.
        facts.update({"over_published": over, "under_published": under})
    return [defect] if defect else []


# FIX-09 (PR #185 review P3): a live reply proves live odds only if at least one selection is
# explicitly open (suspended false) with a published price. All-suspended, all-null or never
# explicitly open replies are PARTIAL, never PASS.
LIVE_PARTIAL_STATES = frozenset({"ALL_SUSPENDED", "ALL_NULL_ODDS", "NO_OPEN_PRICED_SELECTION", "NO_SELECTIONS"})


def live_state(selections: int, counts: dict[str, int]) -> str:
    if selections == 0:
        return "NO_SELECTIONS"
    if counts["open_priced"]:
        return "ACTIVE"
    if counts["suspended"] == selections:
        return "ALL_SUSPENDED"
    if counts["null_odds_kept_null"] == selections:
        return "ALL_NULL_ODDS"
    return "NO_OPEN_PRICED_SELECTION"


def _check_odds_page1(p, ctx, facts):
    defects = _check_odds(p, ctx, facts)
    paging = p.get("pagination") if isinstance(p.get("pagination"), dict) else {}
    ctx["odds_pages"] = paging.get("total") if _is_int(paging.get("total")) else None
    facts["paging_total"] = ctx["odds_pages"]
    return defects


def _check_odds_page2(p, ctx, facts):
    defects = _check_odds(p, ctx, facts)
    paging = p.get("pagination") if isinstance(p.get("pagination"), dict) else {}
    return defects + (["PAGE_2_NOT_RETURNED"] if paging.get("current") != 2 else [])


def _check_live(p, ctx, facts):
    resp = _response(p)
    if not all(_fixture_shape(f) for f in resp):
        return ["FIXTURE_SHAPE_INVALID"]
    facts["live_fixtures"] = len(resp)
    if resp:
        ctx["fixture_live"] = min(f["fixture"]["id"] for f in resp)
        facts["Z"] = ctx["fixture_live"]
    return []


def _check_standings(p, ctx, facts):
    resp = _response(p)
    try:
        groups = resp[0]["league"]["standings"]
        rows = [r for g in groups for r in g]
    except (IndexError, KeyError, TypeError):
        return ["STANDINGS_SHAPE_INVALID"] if resp else []
    if not all(isinstance(r, dict) and _is_int(r.get("rank")) and _is_int(r.get("points"))
               and isinstance(r.get("team"), dict) and _is_int(r["team"].get("id")) for r in rows):
        return ["STANDINGS_ROW_INVALID"]
    facts.update({"groups": len(groups), "rows": len(rows)})
    return []


def _check_players(p, ctx, facts):
    ids = sorted(r["player"]["id"] for r in _response(p)
                 if isinstance(r, dict) and isinstance(r.get("player"), dict) and _is_int(r["player"].get("id")))
    if ids:
        ctx["player"] = ids[0]
        facts["P"] = ids[0]
    return []


NEGATIVE_TIMEZONE = "Mars/Olympus_Mons"


def _day(ctx: dict[str, Any], delta_days: int = 0) -> str:
    return (datetime.fromtimestamp(ctx["now_ts"], timezone.utc).date() + timedelta(days=delta_days)).isoformat()


def _bet(c):  # optional filter: None is dropped, never replaced by a guessed id
    return c.get("bet_ou")


def _odds_page2(c):
    if c.get("odds_pages") == 1:
        return "NOT_APPLICABLE_SINGLE_PAGE"
    return _args("league", "season", "odds_pages", league=lambda c: c["league"], season=lambda c: c["season"],
                 bet=_bet, page=lambda c: 2)(c)


def _live_odds_args(c):
    return "NO_LIVE_FIXTURE" if c.get("fixture_live") is None else {"fixture": c["fixture_live"]}


def _completion_plan() -> list[Case]:
    """What the runs on 37e74fd left unproven, in the owner's priority order; 12 provider calls,
    36 HTTP attempts at the worst case (<= 45). League 39, season 2026 and the finished fixture
    1557464 are the PASS evidence of those runs (no status, leagues or finished-fixture call)."""
    L, Y = (lambda c: c["league"]), (lambda c: c["season"])
    return [
        Case("C01", "apifootball_fixtures", _args("league", "season", league=L, season=Y,
                                                  from_date=lambda c: _day(c), to_date=lambda c: _day(c, UPCOMING_WINDOW_DAYS),
                                                  timezone=lambda c: "UTC"),
             "data_or_empty", _check_upcoming, "upcoming fixture discovery (U)"),
        Case("C02", "apifootball_bookmakers", lambda c: {}, "data", _check_catalog_single_page, "bookmakers"),
        Case("C03", "apifootball_bets", lambda c: {}, "data", _check_bets, "bets incl. Over/Under by exact name"),
        Case("C04", "apifootball_odds", _args("fixture_u", "bet_ou", fixture=lambda c: c["fixture_u"], bet=_bet),
             "data", lambda p, c, f: _check_odds(p, c, f, fixture_key="fixture_u"),
             "pre-match odds for U, bet=Over/Under", coverage="odds"),
        Case("C05", "apifootball_odds", _args("league", "season", league=L, season=Y, bet=_bet), "data",
             _check_odds_page1, "odds league page 1", coverage="odds"),
        Case("C06", "apifootball_odds", _odds_page2, "data_or_empty", _check_odds_page2, "odds league page 2",
             coverage="odds"),
        Case("C07", "apifootball_fixtures", lambda c: {"live": "all"}, "data", _check_live, "live fixtures (empty = PARTIAL)"),
        Case("C08", "apifootball_live_odds", _live_odds_args, "data_or_empty",
             lambda p, c, f: _check_odds(p, c, f, live=True, fixture_key="fixture_live"), "live odds for Z"),
        Case("C09", "apifootball_standings", _args("league", "season", league=L, season=Y), "data",
             _check_standings, "standings", coverage="standings"),
        Case("C10", "apifootball_odds_mapping", lambda c: {}, "data_or_empty", what="odds mapping (page 1)"),
        Case("C11", "apifootball_live_bets", lambda c: {}, "data", _check_catalog_single_page, "live bets catalog"),
        Case("C12", "apifootball_fixtures", lambda c: {"id": NONEXISTENT_FIXTURE_ID}, "empty",
             what="negative: nonexistent fixture id"),
    ]


def _plan(name: str) -> list[Case]:
    if name not in APIFOOTBALL_SUITES:
        raise ValueError("unknown plan")
    if name == "apifootball_completion":
        return _completion_plan()
    full = name == "apifootball_full"
    L, Y = (lambda c: c["league"]), (lambda c: c["season"])

    bet, odds_page2 = _bet, _odds_page2

    cases = [
        # FIX-06: Europe/Rome and every IANA zone are accepted now; the negative uses a zone that
        # exists in no catalog, refused before any request.
        Case("N00", "apifootball_fixtures", lambda c: {"date": _day(c), "timezone": NEGATIVE_TIMEZONE},
             "invalid_input", what="negative: timezone outside the IANA catalog refused before any request"),
        Case("A01", "apifootball_status", lambda c: {}, "data", _check_status, "provider auth/status"),
        Case("A02", "apifootball_leagues", _args("league", id=L, current=lambda c: True), "data", _check_league,
             "league and current season"),
        Case("A03", "apifootball_fixtures", _args("league", "season", league=L, season=Y,
                                                  from_date=lambda c: _day(c, -7), to_date=lambda c: _day(c, 7),
                                                  timezone=lambda c: "UTC"),
             "data", _check_fixture_window, "real fixtures by date"),
        Case("A04", "apifootball_fixtures", _args("fixture_x", id=lambda c: c["fixture_x"]), "data",
             _check_fixture_detail, "fixture details/result"),
        Case("A05", "apifootball_fixture_events", _args("fixture_x", fixture=lambda c: c["fixture_x"]),
             "data_or_empty", _check_events, "events and goals", coverage="events"),
        Case("A06", "apifootball_fixture_statistics", _args("fixture_x", fixture=lambda c: c["fixture_x"]),
             "data_or_empty", _check_statistics, "statistics", coverage="statistics_fixtures"),
        Case("A07", "apifootball_bookmakers", lambda c: {}, "data", _check_catalog_single_page, "bookmakers"),
        Case("A08", "apifootball_bets", lambda c: {}, "data", _check_bets, "bets/markets incl. Over/Under"),
        Case("A09", "apifootball_odds", _args("fixture_u", fixture=lambda c: c["fixture_u"], bet=bet),
             "data_or_empty", lambda p, c, f: _check_odds(p, c, f, fixture_key="fixture_u"),
             "pre-match odds, real fixture", coverage="odds"),
        Case("A10", "apifootball_odds", _args("league", "season", league=L, season=Y, bet=bet), "data_or_empty",
             _check_odds_page1, "odds page 1", coverage="odds"),
        Case("A11", "apifootball_odds", odds_page2, "data_or_empty", _check_odds_page2, "odds page 2", coverage="odds"),
        Case("A12", "apifootball_fixtures", lambda c: {"live": "all"}, "data", _check_live, "live fixtures (empty = PARTIAL)"),
        Case("A13", "apifootball_live_odds", _live_odds_args,
             "data_or_empty", lambda p, c, f: _check_odds(p, c, f, live=True, fixture_key="fixture_live"),
             "live odds"),
        Case("A14", "apifootball_standings", _args("league", "season", league=L, season=Y), "data",
             _check_standings, "standings", coverage="standings"),
        Case("A15", "apifootball_fixtures", lambda c: {"id": NONEXISTENT_FIXTURE_ID}, "empty",
             what="negative: nonexistent fixture id"),
    ]
    if not full:
        return cases
    T1, T2, P = (lambda c: c["team_home"]), (lambda c: c["team_away"]), (lambda c: c["player"])
    X, U = (lambda c: c["fixture_x"]), (lambda c: c["fixture_u"])
    LY = {"league": L, "season": Y}
    extra = [
        ("apifootball_countries", lambda c: {}), ("apifootball_seasons", lambda c: {}),
        ("apifootball_timezones", lambda c: {}), ("apifootball_team_countries", lambda c: {}),
        ("apifootball_coverage_report", _args("league", "season", id=L, season=Y)),
        ("apifootball_fixture_rounds", _args("league", "season", **LY)),
        ("apifootball_teams", _args("league", "season", **LY)),
        ("apifootball_team_statistics", _args("league", "season", "team_home", team=T1, **LY)),
        ("apifootball_team_seasons", _args("team_home", team=T1)),
        ("apifootball_venues", _args("venue", id=lambda c: c["venue"])),
        ("apifootball_fixture_lineups", _args("fixture_x", fixture=X)),
        ("apifootball_fixture_players", _args("fixture_x", fixture=X)),
        ("apifootball_fixture_headtohead", _args("team_home", "team_away", h2h=lambda c: f"{T1(c)}-{T2(c)}")),
        ("apifootball_injuries", _args("fixture_x", fixture=X)),
        ("apifootball_predictions", _args("fixture_u", fixture=U)),
        ("apifootball_players", _args("league", "season", **LY)),
        ("apifootball_player_profiles", _args("player", player=P)),
        ("apifootball_player_seasons", _args("player", player=P)),
        ("apifootball_player_squads", _args("team_home", team=T1)),
        ("apifootball_player_teams", _args("player", player=P)),
        ("apifootball_top_scorers", _args("league", "season", **LY)),
        ("apifootball_top_assists", _args("league", "season", **LY)),
        ("apifootball_top_yellow_cards", _args("league", "season", **LY)),
        ("apifootball_top_red_cards", _args("league", "season", **LY)),
        ("apifootball_transfers", _args("player", player=P)),
        ("apifootball_trophies", _args("player", player=P)),
        ("apifootball_sidelined", _args("player", player=P)),
        ("apifootball_coaches", _args("team_home", team=T1)),
        ("apifootball_odds_mapping", lambda c: {}),
        ("apifootball_live_bets", lambda c: {}),
    ]
    negative = cases.pop()  # the nonexistent-id case stays last
    for i, (tool, args) in enumerate(extra, 1):
        cases.append(Case(f"F{i:02d}", tool, args, "data_or_empty",
                          _check_players if tool == "apifootball_players" else None, "full coverage"))
    return cases + [negative]


def plan_summary(name: str) -> dict[str, Any]:
    cases = _plan(name)
    n = sum(c.expect != "invalid_input" for c in cases)
    w = APIFOOTBALL_WORST_CASE_PER_CALL
    # Every provider call is charged its worst case, so completing the plan needs n * 3.
    return {"cases": len(cases), "provider_calls_nominal": n, "worst_case_http_attempts": n * w,
            "min_budget_to_complete": n * w, "provider_tools_covered": len({c.tool for c in cases})}


def _classify(p: dict[str, Any]) -> str:
    status = p.get("status")
    if status == "REAL_API_RESPONSE":
        data = p.get("data") if isinstance(p.get("data"), dict) else {}
        if "subscription" in data:
            return "HTTP_200"  # /status: account counters, no list to count
        resp = data.get("response")
        return "DATA_PRESENT" if isinstance(resp, list) and resp else "EMPTY_VALID"
    if status in ("PARTIAL_ODDS_MANIFEST", "PARTIAL_ID_MANIFEST"):
        return "PARTIAL"
    return "ERROR"


def check_id_manifest(p: dict[str, Any], facts: dict[str, Any]) -> list[str]:
    """FIX-01: an oversized non-odds reply must be an explicit, data-free id manifest."""
    m = p.get("data") if isinstance(p.get("data"), dict) else {}
    follow = m.get("follow_up") if isinstance(m.get("follow_up"), dict) else {}
    ok = (m.get("partial") is True and m.get("data_included") is False and isinstance(m.get("reply_sha256"), str)
          and _is_int(m.get("rows_in_reply")) and isinstance(follow.get("calls"), list)
          and isinstance(m.get("complete_retrieval_available"), bool))
    facts.update({"id_manifest": True, "rows_in_reply": m.get("rows_in_reply") if _is_int(m.get("rows_in_reply")) else None,
                  "follow_up_calls": len(follow["calls"]) if isinstance(follow.get("calls"), list) else None,
                  "complete_retrieval_available": m.get("complete_retrieval_available")})
    return [] if ok else ["ID_MANIFEST_NOT_EXPLICIT"]


def check_envelope(p: dict[str, Any], tool: str) -> list[str]:
    """API vs MCP correspondence at zero extra quota: the provider envelope travels intact."""
    defects = []
    if p.get("http_status") != 200:
        defects.append("HTTP_STATUS_NOT_200")
    if p.get("endpoint") != APIFOOTBALL_PROVIDER_TOOLS[tool]:
        defects.append("ENDPOINT_MISMATCH")
    if tool == "apifootball_status" or p.get("status") in ("PARTIAL_ODDS_MANIFEST", "PARTIAL_ID_MANIFEST"):
        return defects  # status keeps only non-personal counters; the manifest has its own check
    d = p.get("data")
    if not isinstance(d, dict):
        return defects + ["ENVELOPE_NOT_AN_OBJECT"]
    missing = [k for k in ("get", "parameters", "errors", "results", "paging", "response") if k not in d]
    if missing:
        return defects + ["ENVELOPE_FIELD_MISSING_" + "_".join(missing).upper()]
    if not isinstance(d["get"], str) or d["get"].strip("/") != APIFOOTBALL_PROVIDER_TOOLS[tool].strip("/"):
        defects.append("ENVELOPE_GET_MISMATCH")
    if d["errors"] not in ([], {}):
        defects.append("PROVIDER_ERRORS_NOT_SURFACED")
    if not (_is_int(d["results"]) and isinstance(d["response"], list) and d["results"] == len(d["response"])):
        defects.append("RESULTS_NOT_EQUAL_LEN_RESPONSE")
    pg = d["paging"]
    if not (isinstance(pg, dict) and _is_int(pg.get("current")) and _is_int(pg.get("total"))
            and 1 <= pg["current"] <= pg["total"]):
        defects.append("PAGING_INCONSISTENT")
    elif p.get("pagination") != pg:
        defects.append("MCP_PAGINATION_NOT_PROVIDER_PAGING")
    sent = p.get("parameters") if isinstance(p.get("parameters"), dict) else {}
    echo = d["parameters"] if isinstance(d["parameters"], dict) else {}
    if {k: str(v) for k, v in sent.items()} != {k: str(v) for k, v in echo.items()}:
        defects.append("PARAMETERS_NOT_ECHOED_EXACTLY")
    return defects


# Per-case verdicts: PASS, FAIL, PARTIAL, NOT_TESTED, PROVIDER_UNAVAILABLE.
# PROVIDER_UNAVAILABLE needs concrete evidence recorded in the case's facts: the provider's own
# /leagues coverage flag false for that data, or an HTTP-200 provider `errors` whose only key is
# `plan` (tools.py exposes PROVIDER_DECLARED_ERRORS with its keys and the fixed category
# PLAN_NOT_INCLUDED, never the text; see plan_not_included). Any other declared error stays FAIL
# or an environment stop. An empty reply alone is EMPTY_VALID with PARTIAL or NOT_TESTED, never
# unavailable.
CASE_VERDICTS = ("PASS", "FAIL", "PARTIAL", "NOT_TESTED", "PROVIDER_UNAVAILABLE")
# The verdict of a call that ended in an environment stop: the endpoint was not exercised to a
# reply (provider auth, rate limit, 5xx or open circuit: PROVIDER_ENVIRONMENT_CODES, quota at zero,
# local quota, provider not enabled), so NOT_TESTED, with the stop and error code in the record. Anything that
# is the MCP's fault (coordinator down, a failed MCP call, an unexpected error) stays FAIL.
STOP_VERDICT = {"STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT": "NOT_TESTED",
                "STOP_PROVIDER_QUOTA_ZERO": "NOT_TESTED",
                "STOP_LOCAL_RATE_LIMITED": "NOT_TESTED",
                "STOP_LOCAL_DAILY_QUOTA_EXHAUSTED": "NOT_TESTED",
                "STOP_PROVIDER_NOT_ENABLED": "NOT_TESTED"}


# Provider-side conditions (tools.py fixed codes): the environment, not the MCP, stops the run.
PROVIDER_ENVIRONMENT_CODES = frozenset({"PROVIDER_AUTH_FAILED", "PROVIDER_RATE_LIMITED",
                                        "PROVIDER_SERVER_ERROR", "CIRCUIT_OPEN"})
DECLARED_ENVIRONMENT_CATEGORIES = frozenset({"AUTH", "RATE_LIMIT", "DAILY_QUOTA"})


def _declared_category(p: dict[str, Any]) -> str | None:
    err = p.get("error") if isinstance(p.get("error"), dict) else {}
    return _code(err.get("category")) if err.get("code") == "PROVIDER_DECLARED_ERRORS" else None


def plan_not_included(p: dict[str, Any]) -> bool:
    """Explicit provider evidence that the plan does not include this data: an HTTP-200 reply
    whose only `errors` key is `plan` (tools.py exposes keys and category, never the text)."""
    err = p.get("error") if isinstance(p.get("error"), dict) else {}
    return (p.get("status") == "PROVIDER_ERROR" and _declared_category(p) == "PLAN_NOT_INCLUDED"
            and err.get("keys") == ["plan"])


def stop_reason_for(p: dict[str, Any] | None) -> str | None:
    """S1-S5 of the certification plan, evaluated after every call."""
    if p is None:
        return "STOP_MCP_CALL_FAILED"
    status, code = p.get("status"), _error_code(p)
    quota = p.get("quota_remaining") if isinstance(p.get("quota_remaining"), dict) else {}
    if quota.get("day") == 0 or quota.get("minute") == 0:
        return "STOP_PROVIDER_QUOTA_ZERO"
    if status in ("KEY_NOT_CONFIGURED", "LIVE_NOT_AUTHORIZED", "PROVIDER_CLIENT_UNAVAILABLE"):
        return "STOP_PROVIDER_NOT_ENABLED"
    if status == "PROVIDER_ERROR":
        # The bridge's own coordinator refused (FIX-08): the run stops, nothing was sent.
        if code == "LOCAL_RATE_LIMITED":
            return "STOP_LOCAL_RATE_LIMITED"
        if code == "LOCAL_DAILY_QUOTA_EXHAUSTED":
            return "STOP_LOCAL_DAILY_QUOTA_EXHAUSTED"
        if code in COORDINATOR_CODES:
            return "STOP_COORDINATOR_UNAVAILABLE"
        if code in PROVIDER_ENVIRONMENT_CODES or _declared_category(p) in DECLARED_ENVIRONMENT_CATEGORIES:
            return "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT"
        if code in KNOWN_SIZE_CODES or plan_not_included(p):
            return None  # PLAN_NOT_INCLUDED concerns this endpoint only: the run goes on
        # Everything else, PROVIDER_REQUEST_FAILED included, may be an MCP or request defect.
        return "STOP_UNEXPECTED_PROVIDER_ERROR"
    if status not in ("REAL_API_RESPONSE", "PARTIAL_ODDS_MANIFEST", "PARTIAL_ID_MANIFEST", "PAYLOAD_TOO_LARGE",
                      "INVALID_INPUT"):
        return "STOP_UNEXPECTED_TOOL_STATUS"
    return None


class ApiFootballSuite:
    """Runs a case plan through `call(tool, args) -> payload | None` under a ProviderBudget."""

    def __init__(self, call: Callable[[str, dict[str, Any]], dict[str, Any] | None], budget: ProviderBudget, *,
                 plan: str = "apifootball", now: Callable[[], float] = time.time, sleep=time.sleep,
                 spacing: float = APIFOOTBALL_CALL_SPACING_SECONDS):
        if plan not in APIFOOTBALL_SUITES:
            raise ValueError("unknown plan")
        self.call, self.budget, self.plan = call, budget, plan
        self._now, self._sleep, self.spacing = now, sleep, spacing
        self.ctx: dict[str, Any] = {"league": PROBE_LEAGUE, "now_ts": int(now())}
        if plan == "apifootball_completion":
            # Reused PASS evidence of the 37e74fd runs, never re-fetched.
            self.ctx.update(season=EVIDENCE_SEASON, fixture_x=EVIDENCE_FINISHED_FIXTURE)
        self.records: list[dict[str, Any]] = []
        self.stop_reason: str | None = None
        self._last_call: float | None = None

    def _not_tested(self, case: Case, reason: str) -> None:
        self.records.append({"id": case.cid, "tool": case.tool, "endpoint": APIFOOTBALL_PROVIDER_TOOLS[case.tool],
                             "what": case.what, "outcome": "NOT_TESTED", "verdict": "NOT_TESTED", "reason": reason,
                             "reserved": 0, "charged": 0})

    def run(self, precondition: str | None = None) -> None:
        self.stop_reason = precondition
        for case in _plan(self.plan):
            if self.stop_reason:
                self._not_tested(case, self.stop_reason)
                continue
            args = case.args(self.ctx)
            if isinstance(args, str):
                self._not_tested(case, args)
                continue
            if not self.budget.can_reserve():
                self.stop_reason = "STOP_BUDGET_REACHED"
                self._not_tested(case, self.stop_reason)
                continue
            self._execute(case, {k: v for k, v in args.items() if v is not None})

    def _execute(self, case: Case, args: dict[str, Any]) -> None:
        if self._last_call is not None and self.spacing:
            wait = self.spacing - (self._now() - self._last_call)
            if wait > 0:
                self._sleep(wait)
        reserved = self.budget.reserve(case.tool)
        try:
            payload = self.call(case.tool, args)
        except Exception:
            self.budget.settle(None)  # the reservation stays charged in full
            raise
        finally:
            self._last_call = self._now()
        if not isinstance(payload, dict):
            payload = None
        charged, basis = self.budget.settle(payload)
        rec: dict[str, Any] = {"id": case.cid, "tool": case.tool, "endpoint": APIFOOTBALL_PROVIDER_TOOLS[case.tool],
                               "what": case.what, "arguments": args, "reserved": reserved, "charged": charged,
                               "settlement": basis}
        stop = stop_reason_for(payload)
        if payload is None:
            rec.update(outcome="ERROR", verdict="FAIL", defects=["MCP_CALL_FAILED"])
        else:
            rec.update(self._evaluate(case, payload, stop))
        if not stop and case.cid == "A01" and isinstance(self.ctx.get("provider_day_left"), int) \
                and self.ctx["provider_day_left"] < self.budget.budget:
            stop = "STOP_PROVIDER_QUOTA_LOW"  # the provider plan cannot absorb this run's budget
        if stop:
            self.stop_reason = rec["stop"] = stop
        self.records.append(rec)

    def _evaluate(self, case: Case, p: dict[str, Any], stop: str | None) -> dict[str, Any]:
        status, code = p.get("status"), _error_code(p)
        quota = p.get("quota_remaining") if isinstance(p.get("quota_remaining"), dict) else {}
        out: dict[str, Any] = {
            "tool_status": _code(status) or "UNKNOWN",
            "error_code": code, "http_status": p.get("http_status") if _is_int(p.get("http_status")) else None,
            "network_calls_made_reported": p.get("network_calls_made") if _is_int(p.get("network_calls_made")) else None,
            "quota_remaining": {k: quota.get(k) for k in ("day", "minute") if _is_int(quota.get(k))},
            "payload_sha256": _sha256(p)}
        if case.expect == "invalid_input":
            ok = status == "INVALID_INPUT" and code == "TIMEZONE_NOT_IN_CATALOG"
            return {**out, "outcome": "ERROR", "verdict": "PASS" if ok else "FAIL", "note": "EXPECTED_INVALID_INPUT",
                    "defects": [] if ok else ["NEGATIVE_INPUT_NOT_REFUSED"]}
        outcome = _classify(p)
        out["outcome"] = outcome
        if outcome == "ERROR" and plan_not_included(p):
            return {**out, "verdict": "PROVIDER_UNAVAILABLE", "defects": [], "facts": {"unavailable_evidence": {
                "source": "PROVIDER_DECLARED_ERRORS", "keys": ["plan"], "category": "PLAN_NOT_INCLUDED"}}}
        if outcome == "ERROR":
            known = ("F03_MCP_PAYLOAD_TOO_LARGE" if status == "PAYLOAD_TOO_LARGE" else
                     "F03_PROVIDER_BODY_TOO_LARGE" if code in KNOWN_SIZE_CODES else None)
            return {**out, "verdict": STOP_VERDICT.get(stop, "FAIL"),
                    "defects": [known or "TOOL_ERROR_" + (out["tool_status"] if code is None else code)]}
        d = p.get("data") if isinstance(p.get("data"), dict) else {}
        pg = p.get("pagination") if isinstance(p.get("pagination"), dict) else {}
        out.update({"results": d.get("results") if _is_int(d.get("results")) else None,
                    "paging": {k: pg.get(k) for k in ("current", "total") if _is_int(pg.get(k))},
                    "cache_status": p.get("cache_status") if p.get("cache_status") in ("MISS", "HIT") else None})
        facts: dict[str, Any] = {}
        defects = check_envelope(p, case.tool)
        if p.get("status") == "PARTIAL_ID_MANIFEST":
            defects += check_id_manifest(p, facts)  # FIX-01: real rows, but only ids: PARTIAL at best
        elif case.check and not defects:
            defects += case.check(p, self.ctx, facts)
        if facts:
            out["facts"] = facts
        out["defects"] = defects
        covered = (self.ctx.get("coverage") or {}).get(case.coverage) if case.coverage else None
        if defects == ["CATALOG_HAS_UNREACHABLE_PAGES"]:
            verdict = "PARTIAL"  # real data, but a documented gap: a finding, not a code change
        elif defects:
            verdict = "FAIL"  # a structural or MCP defect is never masked by an absence of data
        elif case.expect == "empty":
            verdict = "PASS" if outcome == "EMPTY_VALID" else "FAIL"
        elif outcome == "EMPTY_VALID" and covered is False:
            # Valid and empty, and the provider itself says the data is not covered: the evidence
            # is that flag, recorded in the facts. Not a PASS, not an MCP fault.
            verdict = "PROVIDER_UNAVAILABLE"
            facts["unavailable_evidence"] = {"source": "/leagues coverage", "flag": case.coverage, "value": False}
            out["facts"] = facts
        elif facts.get("live_state") in LIVE_PARTIAL_STATES:
            verdict = "PARTIAL"  # FIX-09: no open, priced live selection proven
        elif outcome in ("PARTIAL", "EMPTY_VALID"):
            # An empty reply alone proves the envelope, not the data: PARTIAL, never PASS and
            # never PROVIDER_UNAVAILABLE (owner rule: unavailability needs explicit evidence).
            verdict = "PARTIAL"
        else:
            verdict = "PASS"
        return {**out, "verdict": verdict}

    def report(self) -> dict[str, Any]:
        verdicts = [r["verdict"] for r in self.records]
        if "FAIL" in verdicts:
            verdict = "FAIL"
        elif self.stop_reason:
            verdict = "BLOCKED"
        elif all(v == "PASS" for v in verdicts):
            verdict = "PASS"
        else:
            verdict = "PARTIAL"

        def count(key: str) -> dict[str, int]:
            values = [str(r.get(key)) for r in self.records]
            return {v: values.count(v) for v in sorted(set(values))}

        return {"suite": self.plan, "verdict": verdict,
                "stop_reason": self.stop_reason, "budget": self.budget.budget,
                "budget_ceiling": self.budget.ceiling, "worst_case_per_call": APIFOOTBALL_WORST_CASE_PER_CALL,
                "accounting": "WORST_CASE_EVERY_PROVIDER_CALL",
                "provider_requests_charged": self.budget.committed,
                "provider_counter": self.budget.counter_report(),
                "provider_requests_reserved_total": sum(r["reserved"] for r in self.records),
                "direct_provider_calls_from_runner": 0, "probe_league": PROBE_LEAGUE,
                **({"reused_evidence": {"commit": "37e74fd", "league": PROBE_LEAGUE, "season": EVIDENCE_SEASON,
                                        "finished_fixture": EVIDENCE_FINISHED_FIXTURE}}
                   if self.plan == "apifootball_completion" else {}),
                "plan": plan_summary(self.plan), "outcomes": count("outcome"), "verdicts": count("verdict"),
                "cases": self.records}


def finalize(run: Run, error: str | None) -> tuple[dict[str, Any], str]:
    """The report and its text; replaced by a fixed failure if any secret would appear in it."""
    report = run.report(error)
    text = json.dumps(report, indent=2, sort_keys=True)
    if not run.leak_free(text):
        report = {"verdict": "FAIL", "failed_check": "report_leak_check"}
        text = json.dumps(report)
    return report, text


def _actions_mask(value: str) -> None:
    if os.environ.get("GITHUB_ACTIONS") == "true":
        sys.stdout.write(f"::add-mask::{value}\n")
        sys.stdout.flush()


def main(argv: list[str] | None = None, *, client: Any = None,
         base: str = PINNED_BASE, sleep=time.sleep, now: Callable[[], float] = time.time) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--expected-commit", default=None)
    # FCB-WP-009 (R2): fail unless the runtime's database identity is exactly this role.
    ap.add_argument("--expected-db-identity", default=None, choices=["fcb_app"])
    # Opt-in provider suite; `health` (default) is the zero-provider run.
    ap.add_argument("--suite", default="health", choices=list(SUITES))
    ap.add_argument("--apifootball-budget", type=int, default=None)
    ap.add_argument("--apifootball-output", default=None)
    args = ap.parse_args(argv)
    ceiling = SUITE_BUDGET_CEILING.get(args.suite, APIFOOTBALL_BUDGET_CEILING)
    if args.apifootball_budget is None:
        args.apifootball_budget = min(APIFOOTBALL_DEFAULT_BUDGET, ceiling)
    if not 1 <= args.apifootball_budget <= ceiling:
        print(f"apifootball budget outside 1..{ceiling} (owner-approved ceiling for suite {args.suite})")
        return 2
    password = os.environ.get("MCP_OAUTH_PASSWORD", "")
    if not password:
        print("MCP_OAUTH_PASSWORD missing")
        return 2
    if client is None and base != PINNED_BASE:
        print("refusing a non-pinned target")
        return 2
    own = client is None
    client = client or StdlibClient(timeout=20.0)
    run = Run(client, base, password, expected_commit=args.expected_commit, mask=_actions_mask, sleep=sleep,
              expected_db_identity=args.expected_db_identity, suite=args.suite,
              apifootball_budget=args.apifootball_budget, now=now)
    del password
    error = None
    try:
        run.execute()
    except Fail as exc:
        error = str(exc)
    except Exception as exc:  # never the message: it can carry a URL with a code
        error = "unexpected_" + type(exc).__name__
        run.results.append({"check": error, "outcome": "FAIL"})
    finally:
        if own:
            client.close()
    report, text = finalize(run, error)
    with open(args.output, "w", encoding="utf-8") as fh:
        fh.write(text)
    suite = report.get("apifootball_suite") if isinstance(report.get("apifootball_suite"), dict) else None
    if args.apifootball_output and run.suite != "health":
        # A subset of the leak-checked report (or its fixed failure), never anything else.
        with open(args.apifootball_output, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(suite if suite is not None else {"verdict": "FAIL", "failed_check": report.get("failed_check")},
                                indent=2, sort_keys=True))
    print(f"verdict={report['verdict']} checks_passed={report.get('checks_passed')} failed_check={report.get('failed_check')}")
    if run.suite != "health":
        print(f"apifootball_verdict={(suite or {}).get('verdict', 'FAIL')} "
              f"provider_requests_charged={(suite or {}).get('provider_requests_charged')}")
        if (suite or {}).get("verdict") != "PASS":
            return 1  # only a PASS suite exits 0: PARTIAL, BLOCKED and a mostly NOT_TESTED run fail
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
