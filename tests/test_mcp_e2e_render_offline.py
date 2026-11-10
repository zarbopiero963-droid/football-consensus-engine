"""The protected E2E client (scripts/mcp_e2e_render.py), exercised offline against the REAL
bridge app in-process, with the OAuth store replaced by an in-memory model of its contract.

Proves the client completes the whole flow, catches a regression (a reusable refresh token),
never calls a tool outside its zero-cost allowlist, never targets a non-pinned origin, masks
every runtime secret for the Actions log and writes a report free of password, codes and tokens.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import sys
import textwrap

import pytest

from scripts import mcp_e2e_render as e2e

PASSWORD = "LOCAL_E2E_TEST_ONLY_PASSWORD_7d1e"

SCRIPT = textwrap.dedent(r'''
    import asyncio, hashlib, json, os, secrets, sys
    FAKE_TRANSPORT = None
    if os.environ.get("E2E_FAKE_APIFOOTBALL"):
        # Opt-in suite tests: only the provider HTTP transport is replaced (tests/_apifootball_fake_provider.py);
        # the real resilience client, tools.py and the MCP/OAuth server stay in the loop.
        import bridge.api_football.wiring as _wiring
        import _apifootball_fake_provider as _fake
        FAKE_TRANSPORT = _fake.FakeTransport(by_path=json.loads(os.environ.get("E2E_FAKE_BY_PATH", "{}")))
        _wiring.build_client = lambda settings: _fake.build_client(FAKE_TRANSPORT)
    import bridge.server as srv
    from starlette.testclient import TestClient
    from scripts import mcp_e2e_render as e2e

    async def noop(*a, **k):
        return None

    async def idle():
        await asyncio.Event().wait()

    async def stop_all():
        return {"failed": []}

    srv.bridge.prepare_cloak_runtime = noop
    srv.bridge.reaper_loop = idle
    srv.bridge.stop_all = stop_all

    import logging
    # The starlette TestClient (test transport only, not the production stdlib client) logs every
    # request URL via httpx; silence just those loggers so stderr reflects the client under test.
    for _name in ("httpx", "httpx2", "httpcore"):
        logging.getLogger(_name).setLevel(logging.WARNING)

    MODE = os.environ["E2E_TEST_MODE"]
    st = srv.oauth_store
    clients, codes, access, refresh, issued = set(), {}, {}, {}, []

    def register(redirect):
        cid = "fcbdcr1_" + secrets.token_hex(16)
        clients.add(cid); issued.append(cid)
        return cid

    def save_code(code, **kw):
        codes[code] = dict(kw, used=False); issued.append(code)

    def pair(a, r, row):
        access[a] = row; refresh[r] = dict(row, revoked=False); issued.extend([a, r])

    def exchange(code, *, client_id, redirect_uri, verifier_matches, access, refresh, **_):
        row = codes.get(code)
        if not row or row["used"]:
            return None
        if MODE not in ("code_not_burned", "code_reusable"):
            row["used"] = True
        verifier_ok = MODE == "verifier_ignored" or verifier_matches(row["code_challenge"])
        if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri or not verifier_ok:
            row["used"] = True if MODE == "code_reusable" else row["used"]
            return None
        pair(access, refresh, {"client_id": client_id, "resource": row["resource"], "scope": row["scope"]})
        return {"resource": row["resource"], "scope": row["scope"]}

    def rotate(token, *, client_id, access, refresh, **_):
        row = refresh_store.get(token)
        if MODE == "wrong_client_burns" and row:
            row["revoked"], was = True, row["revoked"]
            if was or row["client_id"] != client_id:
                return None
            row["revoked"] = True
            pair(access, refresh, row)
            return {"resource": row["resource"], "scope": row["scope"]}
        if not row or (row["client_id"] != client_id and MODE != "refresh_any_client") or row["revoked"]:
            return None
        if MODE == "refresh_any_client" and row["client_id"] != client_id:
            pair(access, refresh, row)  # accepted for a foreign client, owner's token NOT burned
            return {"resource": row["resource"], "scope": row["scope"]}
        if MODE != "reusable_refresh":
            row["revoked"] = True
        pair(access, refresh, row)
        return {"resource": row["resource"], "scope": row["scope"]}

    refresh_store = refresh
    st.register_claude_dcr_client = register
    st.valid_claude_dcr_client = lambda cid, redirect: cid in clients
    st.save_code = save_code
    st.exchange_code = exchange
    st.refresh_and_issue = rotate
    st.valid_access_token = lambda t, res, scope: (
        "2099-01-01" if MODE == "any_bearer" or (t in access and access[t]["resource"] == res
                                                 and scope in access[t]["scope"].split())
        or (MODE == "refresh_as_bearer" and t in refresh)
        or (MODE == "malformed_bearer" and t == "not-an-issued-token") else None)

    import re as _re
    if MODE in ("refresh_as_bearer", "malformed_bearer"):
        srv.oauth.ISSUED_TOKEN_RE = _re.compile(r"^.+$")
    if MODE == "dcr_foreign_accepted":
        srv.oauth._claude_dcr_registration_valid = lambda data: True
    if MODE == "weak_consent_headers":
        srv.oauth.CONSENT_HEADERS = {**srv.oauth.NO_STORE, "X-Frame-Options": "DENY"}
    if MODE in ("redirect_other_target", "redirect_wrong_state", "redirect_wrong_iss"):
        _Redirect = srv.oauth.RedirectResponse
        def _tampered(url, status_code=302, **kw):
            if MODE == "redirect_other_target":
                url = url.replace("https://claude.ai/", "https://claude.ai.evil.example/", 1)
            elif MODE == "redirect_wrong_state":
                url = _re.sub(r"state=[^&]+", "state=other", url)
            else:
                url = _re.sub(r"iss=[^&]+", "iss=https%3A%2F%2Fevil.example", url)
            return _Redirect(url, status_code=status_code, **kw)
        srv.oauth.RedirectResponse = _tampered
    if MODE == "tools_count_mismatch":
        _counts = srv._tool_registry_counts
        async def _more():
            total, by = await _counts()
            return total + 1, by
        srv._tool_registry_counts = _more

    # Server-side record of every tools/call the bridge actually executed (review of #179, P2-1).
    server_tools_called = []
    _call_tool = srv.mcp.call_tool
    async def _recording_call_tool(name, arguments, context=None):
        server_tools_called.append(name)
        result = await _call_tool(name, arguments, context)
        tamper = {"sxbet_live": ("sxbet_health", {"available_modes": {"READ_ONLY": "AVAILABLE",
                                                                    "SIMULATION": "AVAILABLE",
                                                                    "LIVE_TRADING": "AVAILABLE"}}),
                  "sxbet_mode": ("sxbet_health", {"mode": "LIVE_TRADING"}),
                  "sxbet_write_tools": ("sxbet_health", {"write_tools": ["sxbet_place_order"]}),
                  "apifootball_network": ("apifootball_health", {"network_calls_made": 1}),
                  # FCB-WP-009 (R2): the runtime's database identity as the real tool reports it.
                  "db_identity_app": ("apifootball_health", {"database_identity": {
                      "status": "OK", "current_user": "fcb_app", "session_user": "fcb_app"}}),
                  "db_identity_admin": ("apifootball_health", {"database_identity": {
                      "status": "OK", "current_user": "avnadmin", "session_user": "avnadmin"}}),
                  "db_identity_set_role": ("apifootball_health", {"database_identity": {
                      "status": "OK", "current_user": "fcb_app", "session_user": "avnadmin"}})}.get(MODE)
        if tamper and tamper[0] == name and getattr(result, "structured_content", None) is not None:
            data = {**result.structured_content, **tamper[1]}
            result.structured_content = data
            for item in result.content or []:
                if getattr(item, "text", None):
                    item.text = json.dumps(data)
        return result
    srv.mcp.call_tool = _recording_call_tool

    if MODE == "wrong_password_accepted":
        import hmac as _hmac
        class _H:
            @staticmethod
            def compare_digest(a, b):
                return True if isinstance(a, bytes) else _hmac.compare_digest(a, b)
        srv.oauth.hmac = _H
    if MODE == "rotation_without_refresh":
        original, calls = srv.oauth._token_response, {"n": 0}
        def _token_response(a, r, scope):
            calls["n"] += 1
            resp = original(a, r, scope)
            if calls["n"] >= 2:
                body = json.loads(resp.body); body.pop("refresh_token")
                resp = srv.oauth.JSONResponse(body, headers=srv.oauth.NO_STORE)
            return resp
        srv.oauth._token_response = _token_response

    out_path = os.environ["E2E_OUT"]
    with TestClient(srv.app, base_url=e2e.PINNED_BASE, follow_redirects=False) as c:
        argv = ["--output", out_path]
        if os.environ.get("E2E_EXPECTED_COMMIT"):
            argv += ["--expected-commit", os.environ["E2E_EXPECTED_COMMIT"]]
        if os.environ.get("E2E_EXPECTED_DB_IDENTITY"):
            argv += ["--expected-db-identity", os.environ["E2E_EXPECTED_DB_IDENTITY"]]
        argv += json.loads(os.environ.get("E2E_EXTRA_ARGV", "[]"))
        for _name in ("httpx", "httpx2", "httpcore"):  # after lifespan logging setup
            logging.getLogger(_name).setLevel(logging.WARNING)
        clock = {}
        if FAKE_TRANSPORT is not None:
            clock = {"now": lambda: _fake.NOW}
        rc = e2e.main(argv, client=c, sleep=lambda s: None, **clock)
    print("RESULT_JSON=" + json.dumps({"rc": rc, "issued": issued, "server_tools_called": server_tools_called,
                                       "provider_requests": len(FAKE_TRANSPORT.requests) if FAKE_TRANSPORT else 0}))
''')


def _run(tmp_path, mode="normal", github_actions=False, extra_env=None):
    out = tmp_path / "report.json"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("APIFOOTBALL_", "SXBET_", "RENDER_GIT_COMMIT", "BROWSER_", "CLOAK"))}
    env.pop("DATABASE_URL", None)
    env.update({"PYTHONPATH": os.getcwd(), "MCP_OAUTH_PASSWORD": PASSWORD, "E2E_TEST_MODE": mode,
                "E2E_OUT": str(out), "BROWSER_TOOLS_ENABLED": "false",
                "APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_LIVE_AUTHORIZED": "false",
                "SXBET_ENABLED": "true", "SXBET_API_KEY": "FAKE_SX_KEY_E2E_TEST_ONLY",
                "GITHUB_ACTIONS": "true" if github_actions else "false", **(extra_env or {})})
    proc = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=180)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON=")), None)
    assert line, proc.stdout[-3000:] + proc.stderr[-3000:]
    res = json.loads(line[len("RESULT_JSON="):])
    # Review of #179, P2-3: nothing secret on stderr either (logging, tracebacks).
    issued = res["issued"]
    # Re-review of #179: log handlers (e.g. Rich) wrap and pad long values, so also check the
    # whitespace-stripped stream.
    flat_err = re.sub(r"\s+", "", proc.stderr)
    assert PASSWORD not in proc.stderr and PASSWORD not in flat_err
    assert not any(v in proc.stderr or v in flat_err for v in issued), "a runtime secret reached stderr"
    return res, out.read_text(encoding="utf-8"), proc.stdout


def test_full_flow_passes_and_the_report_holds_no_secret(tmp_path):
    res, report_text, stdout = _run(tmp_path, github_actions=True)
    report = json.loads(report_text)
    assert res["rc"] == 0, report
    assert report["verdict"] == "PASS" and report["failed_check"] is None
    passed = {c["check"] for c in report["checks"] if c["outcome"] == "PASS"}
    for must in ("mcp_without_token_401", "mcp_unknown_wellformed_bearer_401", "dcr_foreign_redirect_refused",
                 "consent_page_headers", "consent_wrong_password_401", "token_wrong_verifier_invalid_grant",
                 "token_code_burned_after_failed_binding", "token_exchange_200", "token_code_replay_refused",
                 "mcp_initialize", "mcp_tools_list_matches_healthz", "tools_call_sxbet_health_read_only",
                 "tools_call_apifootball_health_no_provider_call", "refresh_wrong_client_refused",
                 "refresh_rotation_200", "refresh_replay_refused", "refresh_token_is_not_a_bearer",
                 "refreshed_access_token_works"):
        assert must in passed, must
    assert report["facts"]["tools_listed"] > 0 and report["provider_or_order_calls_attempted"] == 0
    # Review of #179, P2-1: what the SERVER executed, not what the client says it called.
    assert sorted(res["server_tools_called"]) == ["apifootball_health", "sxbet_health"]
    assert sorted(report["tools_called"]) == ["apifootball_health", "sxbet_health"]
    # Nothing secret in the report; every runtime secret was masked for the Actions log.
    assert PASSWORD not in report_text and PASSWORD not in stdout
    assert res["issued"]
    for value in res["issued"]:
        assert value not in report_text
        assert f"::add-mask::{value}" in stdout
        # The only stdout lines that carry a runtime secret are its own mask commands.
        assert all(l == f"::add-mask::{value}" for l in stdout.splitlines()
                   if value in l and not l.startswith("RESULT_JSON=")), value


@pytest.mark.parametrize("mode,extra_env,failed", [
    ("code_not_burned", None, "token_code_burned_after_failed_binding"),
    ("verifier_ignored", None, "token_wrong_verifier_invalid_grant"),
    ("wrong_client_burns", None, "refresh_rotation_200"),
    ("rotation_without_refresh", None, "refresh_rotation_200"),
    ("wrong_password_accepted", None, "consent_wrong_password_401"),
    ("any_bearer", None, "mcp_unknown_wellformed_bearer_401"),
    ("normal", {"BROWSER_TOOLS_ENABLED": "true"}, "browser_and_cloak_off"),
    ("normal", {"RENDER_GIT_COMMIT": "a" * 40, "E2E_EXPECTED_COMMIT": "b" * 40}, "deployed_commit_is_main_head"),
    # Review of #179, P2-2: one RED control per remaining core check.
    ("code_reusable", None, "token_code_replay_refused"),
    ("refresh_any_client", None, "refresh_wrong_client_refused"),
    ("refresh_as_bearer", None, "refresh_token_is_not_a_bearer"),
    ("malformed_bearer", None, "mcp_malformed_bearer_401"),
    ("dcr_foreign_accepted", None, "dcr_foreign_redirect_refused"),
    ("weak_consent_headers", None, "consent_page_headers"),
    ("redirect_other_target", None, "authorize_redirect_target_is_registered_callback"),
    ("redirect_wrong_state", None, "authorize_redirect_state_and_iss"),
    ("redirect_wrong_iss", None, "authorize_redirect_state_and_iss"),
    ("sxbet_live", None, "tools_call_sxbet_health_read_only"),
    ("apifootball_network", None, "tools_call_apifootball_health_no_provider_call"),
    ("tools_count_mismatch", None, "mcp_tools_list_matches_healthz"),
    ("sxbet_mode", None, "tools_call_sxbet_health_read_only"),
    ("sxbet_write_tools", None, "tools_call_sxbet_health_read_only"),
    ("normal", {"RENDER_GIT_COMMIT": "abcdef1", "E2E_EXPECTED_COMMIT": "abcdef1" + "0" * 33},
     "deployed_commit_is_main_head"),
])
def test_each_server_regression_fails_the_run_at_its_own_check(tmp_path, mode, extra_env, failed):
    """RED controls (review: checks that could be made vacuous); none leaks into the report."""
    res, report_text, _ = _run(tmp_path, mode=mode, extra_env=extra_env)
    report = json.loads(report_text)
    assert res["rc"] == 1 and report["verdict"] == "FAIL"
    assert report["failed_check"] == failed
    assert PASSWORD not in report_text and not any(v in report_text for v in res["issued"])


def test_the_matching_deployed_commit_passes(tmp_path):
    res, report_text, _ = _run(tmp_path, extra_env={"RENDER_GIT_COMMIT": "c" * 40, "E2E_EXPECTED_COMMIT": "c" * 40})
    assert res["rc"] == 0 and json.loads(report_text)["verdict"] == "PASS"


def test_a_reusable_refresh_token_fails_the_run(tmp_path):
    """RED control: the client is not vacuous; a replayable refresh token is caught."""
    res, report_text, _ = _run(tmp_path, mode="reusable_refresh")
    report = json.loads(report_text)
    assert res["rc"] == 1 and report["verdict"] == "FAIL"
    assert report["failed_check"] == "refresh_replay_refused"
    assert PASSWORD not in report_text and not any(v in report_text for v in res["issued"])


def test_only_allowlisted_zero_cost_tools_can_be_called():
    run = e2e.Run(client=None, base=e2e.PINNED_BASE, password="p", expected_commit=None, mask=lambda v: None)
    for name in ("browser_create", "sxbet_balance", "apifootball_odds", "sxbet_simulate_order"):
        with pytest.raises(e2e.Fail, match="tool_not_allowlisted"):
            run.call_tool(name, "t")
    assert e2e.ALLOWED_TOOL_CALLS == {"sxbet_health", "apifootball_health"}


def test_the_target_is_pinned_and_the_password_is_required(tmp_path, monkeypatch):
    out = str(tmp_path / "r.json")
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", "x")
    assert e2e.main(["--output", out], base="https://attacker.example") == 2
    monkeypatch.delenv("MCP_OAUTH_PASSWORD")
    assert e2e.main(["--output", out]) == 2


def test_a_leaking_report_is_replaced_by_a_fixed_failure():
    run = e2e.Run(client=None, base=e2e.PINNED_BASE, password="SEKRET-pass", expected_commit=None,
                  mask=lambda v: None)
    run.secret("tok-123")
    assert not run.leak_free('{"x": "tok-123"}') and not run.leak_free("SEKRET-pass")
    assert run.leak_free('{"x": "clean"}')


def test_the_workflow_is_owner_manual_main_only_and_uploads_only_the_opt_in_suite_evidence():
    """Intentional change (owner decision, path B): the default `health` suite still uploads
    nothing; only the opt-in API-FOOTBALL suites upload their own small, leak-checked JSON."""
    import yaml
    from pathlib import Path
    wf = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "fcb-mcp-e2e.yml"
    data = yaml.safe_load(wf.read_text(encoding="utf-8"))
    assert set(data.get("on", data.get(True))) == {"workflow_dispatch"}
    job = data["jobs"]["e2e"]
    assert job["environment"] == "mcp-e2e" and job["if"] == "github.ref == 'refs/heads/main'"
    text = wf.read_text(encoding="utf-8")
    assert "cache" not in text.lower().replace("no-store", "")
    uploads = [s for s in job["steps"] if "upload-artifact" in str(s.get("uses", ""))]
    assert text.count("upload-artifact") == 1 and len(uploads) == 1
    up = uploads[0]
    assert up["with"]["path"] == "${{ runner.temp }}/apifootball_suite.json"
    assert up["if"] == "${{ !cancelled() && inputs.suite != 'health' && inputs.suite != '' }}"
    assert "secrets." not in str(up) and "mcp_e2e.json" not in str(up)
    secret_steps = [s for s in job["steps"] if "secrets." in str(s)]
    # WP-009 (R2): the expected database identity is a fixed-choice input, not a secret; so are
    # the suite and the budget (validated in bash, capped again by the script's ceiling).
    assert len(secret_steps) == 1 and set(secret_steps[0]["env"]) == {
        "MCP_OAUTH_PASSWORD", "EXPECTED_COMMIT", "EXPECTED_DB_IDENTITY", "E2E_SUITE", "E2E_APIFOOTBALL_BUDGET"}
    assert secret_steps[0]["env"]["EXPECTED_DB_IDENTITY"] == "${{ inputs.expected_db_identity }}"
    assert secret_steps[0]["env"]["E2E_SUITE"] == "${{ inputs.suite }}"
    assert secret_steps[0]["env"]["E2E_APIFOOTBALL_BUDGET"] == "${{ inputs.apifootball_budget }}"
    assert "${{" not in secret_steps[0]["run"]  # inputs reach bash only through env, never inlined
    inputs = data.get("on", data.get(True))["workflow_dispatch"]["inputs"]
    assert inputs["expected_db_identity"]["options"] == ["", "fcb_app"]
    assert inputs["suite"]["options"] == ["health", "apifootball", "apifootball_full"]
    assert inputs["suite"]["default"] == "health" and inputs["apifootball_budget"]["default"] == "20"
    assert data["run-name"] == ("FCB MCP E2E (expected database identity: "
                                "${{ inputs.expected_db_identity || 'none' }})")
    assert "scripts/mcp_e2e_render.py" in secret_steps[0]["run"] and "set -x" not in text


def test_a_report_that_would_carry_a_secret_becomes_a_fixed_failure():
    run = e2e.Run(client=None, base=e2e.PINNED_BASE, password='pa"ss\\wörd', expected_commit=None,
                  mask=lambda v: None)
    tok = run.secret("tok-" + "x" * 40)
    run.facts["echo"] = tok
    report, text = e2e.finalize(run, None)
    assert report == {"verdict": "FAIL", "failed_check": "report_leak_check"} and tok not in text
    run.facts["echo"] = 'pa"ss\\wörd'  # appears JSON-escaped in the report text
    report, text = e2e.finalize(run, None)
    assert report["failed_check"] == "report_leak_check"
    run.facts["echo"] = "clean"
    assert e2e.finalize(run, None)[0]["verdict"] == "PASS"


def test_the_production_client_never_follows_redirects_nor_uses_a_proxy(monkeypatch):
    """Review P2-2: the consent 302 to claude.ai must be parsed, never followed."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
    hits = []

    class Target(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            hits.append(self.path)
            self.send_response(200); self.send_header("Content-Length", "0"); self.end_headers()

    target = ThreadingHTTPServer(("127.0.0.1", 0), Target)
    seen = {}

    class Origin(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_POST(self):
            seen["body"] = self.rfile.read(int(self.headers["Content-Length"])).decode()
            seen["ctype"] = self.headers["Content-Type"]
            self.send_response(302)
            self.send_header("Location", f"http://127.0.0.1:{target.server_address[1]}/cb?code=SECRET")
            self.send_header("Content-Length", "0")
            self.end_headers()

        def do_GET(self):
            body = b'{"ok": true}'
            self.send_response(401); self.send_header("Content-Length", str(len(body))); self.end_headers()
            self.wfile.write(body)

    origin = ThreadingHTTPServer(("127.0.0.1", 0), Origin)
    for srv in (target, origin):
        threading.Thread(target=srv.serve_forever, daemon=True).start()
    monkeypatch.setenv("http_proxy", "http://127.0.0.1:9")  # would fail if a proxy were used
    monkeypatch.setenv("HTTP_PROXY", "http://127.0.0.1:9")
    monkeypatch.delenv("no_proxy", raising=False); monkeypatch.delenv("NO_PROXY", raising=False)
    try:
        c = e2e.StdlibClient(timeout=5)
        assert c.follow_redirects is False
        base = f"http://127.0.0.1:{origin.server_address[1]}"
        r = c.post(base + "/oauth/authorize", data={"password": "p w&x"})
        assert r.status_code == 302 and "code=SECRET" in r.headers.get("location")
        assert hits == []  # the redirect target was never contacted
        assert seen["ctype"] == "application/x-www-form-urlencoded" and "password=p+w%26x" in seen["body"]
        r = c.get(base + "/healthz")
        assert r.status_code == 401 and r.json() == {"ok": True}
    finally:
        for srv in (target, origin):
            srv.shutdown(); srv.server_close()


def test_the_e2e_client_needs_no_third_party_package():
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    src = (root / "scripts" / "mcp_e2e_render.py").read_text(encoding="utf-8")
    assert "import httpx" not in src and "import requests" not in src
    wf = (root / ".github" / "workflows" / "fcb-mcp-e2e.yml").read_text(encoding="utf-8")
    assert "pip install" not in wf and "python -I scripts/mcp_e2e_render.py" in wf



def test_tools_call_outside_the_allowlist_is_refused_on_every_path():
    """Review of #179, P2-1: rpc() itself refuses, not only call_tool()."""
    run = e2e.Run(client=None, base=e2e.PINNED_BASE, password="p", expected_commit=None, mask=lambda v: None)
    for name in ("apifootball_status", "sxbet_balance", "browser_create", None):
        with pytest.raises(e2e.Fail, match="tool_not_allowlisted"):
            run.rpc("tools/call", {"name": name, "arguments": {}}, "t")
    assert run.tools_called == []


def test_an_unexpected_exception_reports_its_type_only(tmp_path, monkeypatch):
    """Review of #179, P3: the exception message (which may carry a URL with a code) never
    reaches the report."""
    out = tmp_path / "r.json"
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", PASSWORD)

    def boom(self):
        raise ValueError("https://claude.ai/api/mcp/auth_callback?code=SECRETCODE " + PASSWORD)

    monkeypatch.setattr(e2e.Run, "execute", boom)
    assert e2e.main(["--output", str(out)], client=object(), base=e2e.PINNED_BASE) == 1
    text = out.read_text()
    assert json.loads(text)["failed_check"] == "unexpected_ValueError"
    assert "SECRETCODE" not in text and PASSWORD not in text


def test_server_values_are_masked_only_when_url_safe():
    """Review of #179, P3: a server value with a newline can never inject a workflow command."""
    masked = []
    run = e2e.Run(client=None, base=e2e.PINNED_BASE, password="p", expected_commit=None, mask=masked.append)
    assert run.secret("abc-DEF_123.~") == "abc-DEF_123.~" and masked == ["abc-DEF_123.~"]
    for bad in ("x\n::warning::pwned", "a b", "x" * 600):
        with pytest.raises(e2e.Fail, match="unexpected_secret_format"):
            run.secret(bad)
    assert masked == ["abc-DEF_123.~"] and "x\n::warning::pwned" in run.sensitive



def test_the_e2e_client_has_no_logging_or_traceback_channel():
    """Re-review of #179: nothing in the client can write a code or token to stderr through the
    logging/traceback machinery (only fixed lines are printed)."""
    import ast
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts" / "mcp_e2e_render.py").read_text(encoding="utf-8")
    tree = ast.parse(src)
    imported = {a.name.split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.Import) for a in n.names}
    imported |= {(n.module or "").split(".")[0] for n in ast.walk(tree) if isinstance(n, ast.ImportFrom)}
    assert not imported & {"logging", "traceback", "pprint", "warnings"}, imported
    assert "print_exc" not in src and "stderr" not in src


# ---- FCB-WP-009 (R2): the runtime's database identity, proven through an authenticated MCP call

def test_without_an_expected_identity_the_fact_is_only_recorded(tmp_path):
    res, report_text, _ = _run(tmp_path)
    report = json.loads(report_text)
    assert res["rc"] == 0 and report["facts"]["database_identity"] == {"status": "NOT_CONFIGURED"}
    assert "database_identity_is_expected" not in {c["check"] for c in report["checks"]}


@pytest.mark.parametrize("mode", ["normal", "db_identity_admin", "db_identity_set_role"])
def test_an_unexpected_database_identity_fails_the_run(tmp_path, mode):
    """RED: no database, the administrator, or a SET ROLE session (session_user differs)."""
    res, report_text, _ = _run(tmp_path, mode=mode, extra_env={"E2E_EXPECTED_DB_IDENTITY": "fcb_app"})
    report = json.loads(report_text)
    assert res["rc"] == 1 and report["failed_check"] == "database_identity_is_expected"
    assert PASSWORD not in report_text


def test_the_expected_database_identity_passes(tmp_path):
    """GREEN: current_user and session_user both fcb_app."""
    res, report_text, _ = _run(tmp_path, mode="db_identity_app", extra_env={"E2E_EXPECTED_DB_IDENTITY": "fcb_app"})
    report = json.loads(report_text)
    assert res["rc"] == 0 and report["verdict"] == "PASS"
    assert report["facts"]["database_identity"] == {"status": "OK", "current_user": "fcb_app", "session_user": "fcb_app"}


# ---- Opt-in API-FOOTBALL suite (owner decision, path B), through the REAL server and OAuth ------
# Only the provider HTTP transport is a fake (tests/_apifootball_fake_provider.py); retries, the
# circuit breaker, tools.py and the MCP/OAuth stack are the production code.

FAKE_KEY = "LOCAL_FAKE_APIFOOTBALL_KEY_E2E_ONLY"
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))


def _af_env(tmp_path, suite="apifootball", budget="20", by_path=None, fake=True):
    env = {"E2E_EXTRA_ARGV": json.dumps(["--suite", suite, "--apifootball-budget", budget,
                                         "--apifootball-output", str(tmp_path / "af.json")]),
           "PYTHONPATH": os.pathsep.join([os.getcwd(), TESTS_DIR])}
    if fake:
        env.update({"E2E_FAKE_APIFOOTBALL": "1", "APIFOOTBALL_API_KEY": FAKE_KEY,
                    "APIFOOTBALL_LIVE_AUTHORIZED": "true", "E2E_FAKE_BY_PATH": json.dumps(by_path or {})})
    return env


def test_apifootball_suite_proves_real_tool_outputs_within_the_budget(tmp_path):
    import _apifootball_fake_provider as fake
    res, report_text, stdout = _run(tmp_path, github_actions=True, extra_env=_af_env(tmp_path))
    report = json.loads(report_text)
    af = report["apifootball_suite"]
    assert res["rc"] == 0 and report["verdict"] == "PASS" and af["verdict"] == "PASS", af
    # The OAuth/refresh checks after the suite still ran and passed.
    assert {"refresh_rotation_200", "refreshed_access_token_works"} <= {c["check"] for c in report["checks"]}
    # Hard budget: what reached the (fake) provider never exceeds 20, and the charge is an upper bound.
    assert res["provider_requests"] == 15 and af["provider_requests_charged"] == 17 <= 20
    assert report["provider_or_order_calls_attempted"] == 17
    assert {c["outcome"] for c in af["cases"]} <= {"HTTP_200", "DATA_PRESENT", "EMPTY_VALID", "ERROR"}
    called = res["server_tools_called"]
    assert set(called) <= {"sxbet_health", "apifootball_health"} | set(e2e.APIFOOTBALL_PROVIDER_TOOLS)
    assert called.count("sxbet_health") == 1 and not any(n.startswith(("browser_", "cloak_")) for n in called)
    # Public evidence: no price, no key, no personal account field, no runtime secret.
    artifact = (tmp_path / "af.json").read_text()
    assert json.loads(artifact) == af
    plain_stdout = "\n".join(l for l in stdout.splitlines() if not l.startswith(("::add-mask::", "RESULT_JSON=")))
    assert "apifootball_verdict=PASS provider_requests_charged=17" in plain_stdout
    for text in (report_text, artifact, plain_stdout):
        assert fake.PRICE_SENTINEL not in text and FAKE_KEY not in text and "PRIVATE" not in text
        assert PASSWORD not in text and not any(v in text for v in res["issued"])


def test_apifootball_suite_stops_on_an_auth_error_and_marks_the_rest_not_tested(tmp_path):
    res, report_text, _ = _run(tmp_path, extra_env=_af_env(tmp_path, by_path={"/leagues": ["http_401"]}))
    af = json.loads(report_text)["apifootball_suite"]
    assert res["rc"] == 1 and af["verdict"] == "BLOCKED"
    assert af["stop_reason"] == "STOP_PROVIDER_HTTP_AUTH_RATE_OR_CIRCUIT"
    assert res["provider_requests"] == 2  # status + the refused leagues call, nothing after
    after = af["cases"][[c["id"] for c in af["cases"]].index("A02") + 1:]
    assert after and all(c["outcome"] == "NOT_TESTED" and c["reserved"] == 0 for c in after)


def test_apifootball_suite_makes_no_call_when_live_is_not_authorized(tmp_path):
    res, report_text, _ = _run(tmp_path, extra_env=_af_env(tmp_path, fake=False))
    report = json.loads(report_text)
    af = report["apifootball_suite"]
    assert res["rc"] == 1 and report["verdict"] == "PASS"  # OAuth/MCP fine, provider suite blocked
    assert af["stop_reason"] == "BLOCKED_PROVIDER_NOT_LIVE_OR_COORDINATOR_NOT_READY"
    assert sorted(res["server_tools_called"]) == ["apifootball_health", "sxbet_health"]
    assert af["provider_requests_charged"] == 0 and all(c["outcome"] == "NOT_TESTED" for c in af["cases"])


def test_the_budget_input_cannot_exceed_the_owner_ceiling(tmp_path, monkeypatch):
    monkeypatch.setenv("MCP_OAUTH_PASSWORD", "x")
    out = str(tmp_path / "r.json")
    for bad in ("21", "0", "-1", "100"):
        assert e2e.main(["--output", out, "--suite", "apifootball", "--apifootball-budget", bad],
                        client=object()) == 2
    assert e2e.APIFOOTBALL_BUDGET_CEILING == 20 and e2e.APIFOOTBALL_DEFAULT_BUDGET == 20
