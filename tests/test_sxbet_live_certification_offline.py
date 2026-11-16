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
    # Exactly two .post(: the in-process MCP harness (Starlette TestClient) and the read-only JSON-RPC call to the
    # official SX Network RPC (R2 on-chain balance, owner directive 16-11-2026: declared change of this guard).
    # Never SX Bet. The RPC methods are only eth_chainId / eth_call with the read selectors decimals and balanceOf.
    assert re.findall(r"\.post\(", src) == [".post(", ".post("] and 'client.post("/mcp/"' in src
    assert "client.post(RPC_ENDPOINT" in src and 'RPC_ENDPOINT = "https://rpc-rollup.sx.technology"' in src
    assert set(re.findall(r'_rpc\("([a-zA-Z_]+)"', src)) == {"eth_chainId", "eth_call"}
    assert set(re.findall(r'"0x([0-9a-f]{8})', src)) <= {"70a08231", "313ce567"}   # balanceOf, decimals
    for banned_rpc in ("eth_sendRawTransaction", "eth_sendTransaction", "eth_sign", "personal_"):
        assert banned_rpc not in src, banned_rpc
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


# ---------------------------------------------------------------- R2 consistency probe (offline, faked I/O)
PROXY = "0x" + "aa" * 20
EOA = "0x" + "bb" * 20
TOKEN = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiJ4In0.sig-for-tests-only"


def _fake_io(monkeypatch, *, recovered=True, rest_trades=("t1", "t2"), proxy_usdc=14140000):
    calls = {"rest": [], "rpc": []}

    async def token(api_key):
        return 200, TOKEN

    sessions = []

    async def ws(tok, subs, seconds):
        sessions.append(subs)
        trades = "recent_trades_v3:global"
        if len(sessions) == 1:
            return {"connected": True,
                    "results": {trades: {"subscribed": True, "epoch": "e1", "offset": 10},
                                **{c: {"subscribed": True} for c in subs if c != trades}},
                    "pubs": {trades: [{"offset": 11, "message_id": "m11", "data": {"trade": {"tradeId": "t1"}}}],
                             **{c: [{"offset": 1, "message_id": "b1",
                                     "data": {"version": "0001", "outcomeOne": [], "outcomeTwo": []}}]
                                for c in subs if c != trades}}}
        return {"connected": True,
                "results": {trades: {"subscribed": True, "was_recovering": True, "recovered": recovered,
                                     "replayed": 1}},
                "pubs": {trades: [{"offset": 12, "message_id": "m12", "data": {"trade": {"tradeId": "t2"}}}]}}

    async def rest(path, params=None, api_key=None):
        calls["rest"].append(path)
        return {"/trades-v3/public": {"trades": [{"tradeId": t, "betTime": "2000-01-01T00:00:00Z"}
                                                 for t in rest_trades], "nextKey": None},
                "/orderbook-v3/snapshot/market": {"version": "0002", "outcomeOne": [], "outcomeTwo": []},
                "/user/proxy": {"obv3ProxyWalletAddress": PROXY, "deployed": True},
                "/user/balance-v3": {"balances": [{"userAddress": EOA, "wallet": PROXY,
                                                   "tokenAddress": "0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B",
                                                   "escrowAddress": "0x890482680C3a0116aBB003B17e7D694D13a6c1eB",
                                                   "availableAmount": "14140000", "escrowedAmount": "0",
                                                   "pendingAvailableAmount": "0", "pendingEscrowAmount": "0"}]}}[path]

    async def rpc(method, params):
        calls["rpc"].append(method)
        if method == "eth_chainId":
            return "0x1042"
        data = params[0]["data"]
        if data == "0x313ce567":
            return hex(6)
        return hex(proxy_usdc if PROXY[2:] in data else 0)

    async def no_sleep(_):
        return None
    monkeypatch.setattr(cert, "fetch_realtime_token", token)
    monkeypatch.setattr(cert, "_ws_session", ws)
    monkeypatch.setattr(cert, "_rest_get", rest)
    monkeypatch.setattr(cert, "_rpc", rpc)
    monkeypatch.setattr(cert.asyncio, "sleep", no_sleep)
    return calls, sessions


def _run_probe(market="0x" + "ab" * 32):
    import asyncio
    rec, secrets = cert.Recorder(), ["KEY-123456"]
    asyncio.run(cert.consistency_probe(rec, "KEY-123456", market, 5, secrets))
    return {r["test"]: r for r in rec.rows}, secrets, rec


def test_consistency_probe_reports_each_r2_check_from_real_shaped_data(monkeypatch):
    sys.path.insert(0, str(ROOT))
    calls, sessions = _fake_io(monkeypatch)
    rows, secrets, rec = _run_probe()
    assert rows["realtime_stream_integrity recent_trades_v3:global"]["outcome"] == "PASS"
    assert rows["realtime_recovery recent_trades_v3:global"]["outcome"] == "PASS"
    assert rows["rest_vs_realtime trades (tradeId)"]["outcome"] == "PASS"
    assert rows["rest_vs_realtime orderbook (version)"]["outcome"] == "PASS"
    onchain = rows["onchain_balance usdc (rpc vs /user/balance-v3)"]
    assert onchain["outcome"] == "PASS" and onchain["relation"] == "PROXY_TOKEN_EQUALS_AVAILABLE"
    # The reconnect asks for recovery from the last stream position.
    assert sessions[1]["recent_trades_v3:global"] == {"recoverable": True, "recover": True, "epoch": "e1",
                                                      "offset": 11}
    # Read-only: GET routes and eth_chainId / eth_call only.
    assert set(calls["rpc"]) <= {"eth_chainId", "eth_call"}
    # Nothing account-identifying or monetary in the report; addresses and token joined the leak list.
    text = json.dumps(rec.rows)
    assert cert.leak_free(text, secrets) and "14140000" not in text and PROXY not in text and EOA not in text
    assert TOKEN in secrets and PROXY in secrets and EOA in secrets


def test_consistency_probe_unrecovered_and_missing_ids_are_never_pass(monkeypatch):
    sys.path.insert(0, str(ROOT))
    _fake_io(monkeypatch, recovered=False, rest_trades=("t9",), proxy_usdc=1)
    rows, _, _ = _run_probe(market=None)
    assert rows["realtime_recovery recent_trades_v3:global"]["outcome"] == "PARTIAL"
    assert rows["rest_vs_realtime trades (tradeId)"]["outcome"] == "FAIL"
    assert rows["rest_vs_realtime orderbook (version)"]["outcome"] == "NOT_TESTED"
    assert rows["onchain_balance usdc (rpc vs /user/balance-v3)"]["outcome"] == "PARTIAL"


def test_consistency_probe_without_key_is_not_tested(monkeypatch):
    import asyncio
    rec = cert.Recorder()
    asyncio.run(cert.consistency_probe(rec, "", None, 5, []))
    assert {r["outcome"] for r in rec.rows} == {"NOT_TESTED"} and len(rec.rows) == 4
