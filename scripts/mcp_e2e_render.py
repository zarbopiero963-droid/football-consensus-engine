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

Production leftovers per run (no revocation endpoint yet, WP-022): one DCR client row (730-day
TTL), two used authorization codes, two access tokens (1 h) and one unrevoked refresh token
(30 days) whose plaintext existed only in the ephemeral runner's memory. One consent failure is
recorded against the runner's address (5 per 15 min per address, 50 per hour globally) and two
registrations (10 per hour per address, 100 globally).
"""
from __future__ import annotations

import argparse
import base64
import hashlib
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
                 expected_commit: str | None, mask: Callable[[str], None], sleep=time.sleep):
        self.c, self.base, self._password = client, base.rstrip("/"), password
        self.expected_commit = expected_commit
        self._mask, self._sleep = mask, sleep
        self.sensitive: list[str] = [password]
        self.results: list[dict[str, Any]] = []
        self.facts: dict[str, Any] = {}
        self._rpc_id = 0

    # ---- bookkeeping ---------------------------------------------------------------------
    def secret(self, value: str) -> str:
        if value:
            self.sensitive.append(value)
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
                       bool(h.get("deploy_commit")) and self.expected_commit.startswith(str(h.get("deploy_commit"))),
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

    def report(self, error: str | None) -> dict[str, Any]:
        fails = [r["check"] for r in self.results if r["outcome"] == "FAIL"]
        return {"target": self.base, "verdict": "PASS" if not error and not fails else "FAIL",
                "failed_check": error, "checks_passed": sum(r["outcome"] == "PASS" for r in self.results),
                "checks": self.results, "facts": self.facts,
                "tools_called": sorted(ALLOWED_TOOL_CALLS), "write_operations_attempted": 0}

    def leak_free(self, text: str) -> bool:
        # Raw and JSON-escaped forms: a value with quotes, backslashes or non-ASCII characters
        # appears escaped inside json.dumps output.
        forms = {f for s in self.sensitive if s for f in (s, json.dumps(s)[1:-1])}
        return not any(f in text for f in forms)


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
         base: str = PINNED_BASE, sleep=time.sleep) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--output", required=True)
    ap.add_argument("--expected-commit", default=None)
    args = ap.parse_args(argv)
    password = os.environ.get("MCP_OAUTH_PASSWORD", "")
    if not password:
        print("MCP_OAUTH_PASSWORD missing")
        return 2
    if client is None and base != PINNED_BASE:
        print("refusing a non-pinned target")
        return 2
    own = client is None
    client = client or StdlibClient(timeout=20.0)
    run = Run(client, base, password, expected_commit=args.expected_commit, mask=_actions_mask, sleep=sleep)
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
    print(f"verdict={report['verdict']} checks_passed={report.get('checks_passed')} failed_check={report.get('failed_check')}")
    return 0 if report["verdict"] == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
