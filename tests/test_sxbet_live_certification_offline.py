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
