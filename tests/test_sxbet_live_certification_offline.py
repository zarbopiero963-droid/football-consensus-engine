"""Offline guarantees of the SX Bet live READ-ONLY certification (no network)."""
import base64
import json
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sxbet_live_readonly_certification as cert  # noqa: E402

WORKFLOW = ROOT / ".github/workflows/sxbet-live-readonly-certification.yml"
SCRIPT = ROOT / "scripts/sxbet_live_readonly_certification.py"


def test_refuses_without_explicit_authorization(tmp_path):
    proc = subprocess.run([sys.executable, str(SCRIPT), "--output", str(tmp_path / "r.json")],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode == 2 and not (tmp_path / "r.json").exists()


def test_script_has_no_write_route_or_signing():
    src = SCRIPT.read_text(encoding="utf-8")
    src = src.split('"""', 2)[2]  # code only: the module docstring lists what is forbidden
    # The only .post( is the in-process MCP harness (Starlette TestClient), never SX Bet.
    assert re.findall(r"\.post\(", src) == [".post("] and 'client.post("/mcp/"' in src
    for banned in ('"DELETE"', '"POST"', ".delete(", "/orders-v3\"", "heartbeat", "sign_typed", "private_key", "eth_account"):
        assert banned not in src, banned
    assert "/orders-v3" not in src.replace("GET /orders-v3", "")


def test_leak_guard_and_shape_never_carry_values():
    assert cert.leak_free('{"a": 1}', ["SECRETKEY123"]) and not cert.leak_free("x SECRETKEY123 y", ["SECRETKEY123"])
    s = cert.shape({"balance": "123456789", "items": [{"amount": 5, "id": "x"}]})
    assert s == {"type": "object", "fields": ["balance", "items"], "items_items": 1}
    assert "123456789" not in json.dumps(s) and "amount" not in json.dumps(s)
    assert cert.shape([{"amount": 1, "odds": 2}]) == {"type": "list", "items": 1, "item_fields": ["amount", "odds"]}


def test_jwt_sub_and_centrifugo_frames():
    addr = "0x" + "aB" * 20
    payload = base64.urlsafe_b64encode(json.dumps({"sub": addr}).encode()).decode().rstrip("=")
    assert cert.jwt_sub(f"h.{payload}.s") == addr
    assert cert.jwt_sub("garbage") is None
    frames = cert.centrifugo_frames('{"id":1,"connect":{}}\n{}\n{"push":{"channel":"c"}}\nnot-json\n')
    assert frames == [{"id": 1, "connect": {}}, {}, {"push": {"channel": "c"}}]


def test_workflow_is_manual_main_only_and_secret_scoped():
    text = WORKFLOW.read_text(encoding="utf-8")
    wf = yaml.safe_load(text)
    triggers = wf.get(True, wf.get("on"))  # PyYAML parses bare `on` as True
    assert list(triggers) == ["workflow_dispatch"]
    assert wf["permissions"] == {"contents": "read"}
    job = wf["jobs"]["certify"]
    assert job["if"] == "github.ref == 'refs/heads/main'" and job["timeout-minutes"] <= 15
    assert "SXBET_API_KEY" not in json.dumps(job.get("env", {}))
    for step in job["steps"]:
        run = step.get("run", "")
        assert "secrets." not in run and "${{" not in run  # never inline secrets/expressions in shell
        if "secrets.SXBET_API_KEY" in json.dumps(step):
            assert step["env"]["SXBET_API_KEY"] == "${{ secrets.SXBET_API_KEY }}"
    assert job["steps"][0]["with"]["persist-credentials"] is False


def test_empty_pages_and_case_insensitive_leak_guard():
    assert cert.is_empty_payload({"markets": [], "nextKey": None})
    assert cert.is_empty_payload([]) and cert.is_empty_payload({})
    assert not cert.is_empty_payload({"markets": [{"a": 1}], "nextKey": None})
    assert not cert.is_empty_payload({"balance": "0"})
    addr = "0xAbCdEf0123456789aBcDeF0123456789AbCdEf01"
    assert not cert.leak_free("seen " + addr.lower(), [addr])
    assert not cert.leak_free("seen " + addr, [addr.lower()])


def test_job_uses_environment_and_fails_on_fail_rows():
    wf = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    assert wf["jobs"]["certify"]["environment"] == "sxbet-live-readonly"
    src = SCRIPT.read_text(encoding="utf-8")
    assert 'return 1 if final_counts.get("FAIL") else 0' in src


def test_local_rate_limit_is_respected_by_waiting_not_by_raising_the_limit():
    src = SCRIPT.read_text(encoding="utf-8")
    assert '"SXBET_MAX_RPM": "30"' in src            # the audit keeps the conservative local limit
    assert 'err.get("code") == "SXBET_LOCAL_RATE_LIMITED"' in src and "time.sleep(wait)" in src
    # Composite tools need several GETs: the retry waits the full 60 s sliding window (+1 s), not just
    # the time until one slot frees (run 10: hedge/close previews refused again after a short wait).
    assert "wait = 61" in src


def test_sxbet_secret_is_only_used_by_the_environment_scoped_audit_job():
    """No other workflow may read SXBET_API_KEY; the audit job must stay in the main-only environment."""
    users = [p.name for p in (ROOT / ".github/workflows").glob("*.y*ml")
             if "secrets.SXBET_API_KEY" in p.read_text(encoding="utf-8")]
    assert users == [WORKFLOW.name]
    job = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))["jobs"]["certify"]
    assert job["environment"] == "sxbet-live-readonly" and job["if"] == "github.ref == 'refs/heads/main'"



def test_certification_harness_token_passes_the_mcp_bearer_format_check(monkeypatch):
    """Review of #177: /mcp refuses bearers not shaped like issued tokens before any storage
    call; the certification harness's local token must pass that check, else every live
    certification call would get 401 (a false negative)."""
    import bridge.server as srv
    from bridge import oauth
    assert oauth.ISSUED_TOKEN_RE.fullmatch(cert.CI_LOCAL_TEST_TOKEN)
    for attr in ("prepare_cloak_runtime", "reaper_loop", "stop_all"):
        monkeypatch.setattr(srv.bridge, attr, getattr(srv.bridge, attr))
    monkeypatch.setattr(srv.oauth_store, "valid_access_token", srv.oauth_store.valid_access_token)
    for key in ("SXBET_ENABLED", "SXBET_NETWORK", "SXBET_MAX_RPM", "SXBET_API_KEY", "DATABASE_URL"):
        monkeypatch.setenv(key, "")
    from starlette.testclient import TestClient
    srv2, _client, headers = cert._mcp_harness("LOCAL_TEST_ONLY_KEY")
    # No lifespan here: anything but 401 means the bearer got past the middleware.
    client = TestClient(srv2.app, base_url="https://" + srv2.public_host, raise_server_exceptions=False)
    r = client.post("/mcp/", headers=headers, json={"jsonrpc": "2.0", "id": 1, "method": "ping"})
    assert r.status_code != 401
