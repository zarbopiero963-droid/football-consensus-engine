"""The protected E2E client (scripts/mcp_e2e_render.py), exercised offline against the REAL
bridge app in-process, with the OAuth store replaced by an in-memory model of its contract.

Proves the client completes the whole flow, catches a regression (a reusable refresh token),
never calls a tool outside its zero-cost allowlist, never targets a non-pinned origin, masks
every runtime secret for the Actions log and writes a report free of password, codes and tokens.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import textwrap

import pytest

from scripts import mcp_e2e_render as e2e

PASSWORD = "LOCAL_E2E_TEST_ONLY_PASSWORD_7d1e"

SCRIPT = textwrap.dedent(r'''
    import asyncio, hashlib, json, os, secrets, sys
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
        row["used"] = True
        if row["client_id"] != client_id or row["redirect_uri"] != redirect_uri or not verifier_matches(row["code_challenge"]):
            return None
        pair(access, refresh, {"client_id": client_id, "resource": row["resource"], "scope": row["scope"]})
        return {"resource": row["resource"], "scope": row["scope"]}

    def rotate(token, *, client_id, access, refresh, **_):
        row = refresh_store.get(token)
        if not row or row["client_id"] != client_id or row["revoked"]:
            return None
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
        "2099-01-01" if t in access and access[t]["resource"] == res and scope in access[t]["scope"].split() else None)

    out_path = os.environ["E2E_OUT"]
    with TestClient(srv.app, base_url=e2e.PINNED_BASE, follow_redirects=False) as c:
        rc = e2e.main(["--output", out_path], client=c, sleep=lambda s: None)
    print("RESULT_JSON=" + json.dumps({"rc": rc, "issued": issued}))
''')


def _run(tmp_path, mode="normal", github_actions=False):
    out = tmp_path / "report.json"
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("APIFOOTBALL_", "SXBET_", "RENDER_GIT_COMMIT", "BROWSER_", "CLOAK"))}
    env.pop("DATABASE_URL", None)
    env.update({"PYTHONPATH": os.getcwd(), "MCP_OAUTH_PASSWORD": PASSWORD, "E2E_TEST_MODE": mode,
                "E2E_OUT": str(out), "BROWSER_TOOLS_ENABLED": "false",
                "APIFOOTBALL_ENABLED": "true", "APIFOOTBALL_LIVE_AUTHORIZED": "false",
                "SXBET_ENABLED": "true", "SXBET_API_KEY": "FAKE_SX_KEY_E2E_TEST_ONLY",
                "GITHUB_ACTIONS": "true" if github_actions else "false"})
    proc = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=180)
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON=")), None)
    assert line, proc.stdout[-3000:] + proc.stderr[-3000:]
    res = json.loads(line[len("RESULT_JSON="):])
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
    assert report["facts"]["tools_listed"] > 0 and report["write_operations_attempted"] == 0
    # Nothing secret in the report; every runtime secret was masked for the Actions log.
    assert PASSWORD not in report_text and PASSWORD not in stdout
    assert res["issued"]
    for value in res["issued"]:
        assert value not in report_text
        assert f"::add-mask::{value}" in stdout


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


def test_the_workflow_is_owner_manual_main_only_and_uploads_nothing():
    import yaml
    from pathlib import Path
    wf = Path(__file__).resolve().parents[1] / ".github" / "workflows" / "fcb-mcp-e2e.yml"
    data = yaml.safe_load(wf.read_text(encoding="utf-8"))
    assert set(data.get("on", data.get(True))) == {"workflow_dispatch"}
    job = data["jobs"]["e2e"]
    assert job["environment"] == "mcp-e2e" and job["if"] == "github.ref == 'refs/heads/main'"
    text = wf.read_text(encoding="utf-8")
    assert "upload-artifact" not in text and "cache" not in text.lower().replace("no-store", "")
    secret_steps = [s for s in job["steps"] if "secrets." in str(s)]
    assert len(secret_steps) == 1 and set(secret_steps[0]["env"]) == {"MCP_OAUTH_PASSWORD", "EXPECTED_COMMIT"}
    assert "scripts/mcp_e2e_render.py" in secret_steps[0]["run"] and "set -x" not in text
