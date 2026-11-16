"""S8.2 (issue #187): Toronto certification workflow + preflight script. Offline: fake fetch, throwaway owner key."""
import base64
import json
import pathlib
import subprocess
import sys

import pytest
import yaml

pytest.importorskip("cryptography")

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey  # noqa: E402
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat  # noqa: E402

from signer.risk import risk_config_message  # noqa: E402

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
import sxbet_toronto_certification as cert  # noqa: E402

WORKFLOW = ROOT / ".github/workflows/sxbet-toronto-certification.yml"
SCRIPT = ROOT / "scripts/sxbet_toronto_certification.py"
META = {"status": "success", "data": {
    "chainId": 79479957,
    "domain": {"name": "OBv3 Escrow", "version": "1", "chainId": 79479957,
               "verifyingContract": "0x0Db3aBA7de0BCb41FF99D35306f366cFFEEede38"},
    "activeAsset": {"baseToken": "0x1BC6326EA6aF2aB8E4b6Bc83418044B1923b2956",
                    "escrowAddress": "0x0Db3aBA7de0BCb41FF99D35306f366cFFEEede38", "decimals": 6}}}


def signed_config(owner, networks):
    from signer.risk import RISK_CONFIG_SCHEMA
    body = {"schema": RISK_CONFIG_SCHEMA, "config_version": 1, "networks": networks}
    return {**body, "signature": owner.sign(risk_config_message(body)).hex()}


def env_for(owner=None, networks=None, **kw):
    owner = owner or Ed25519PrivateKey.generate()
    doc = signed_config(owner, networks if networks is not None else {"testnet": _limits_doc()})
    env = {"SXBET_TORONTO_MAKER_ADDRESS": "0x" + "4c" * 20, "SXBET_LIVE_MAKER_ADDRESS": "0x" + "5e" * 20,
           "SXBET_OWNER_ED25519_PUBLIC_KEY_HEX": owner.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex(),
           "SXBET_TORONTO_RISK_CONFIG_B64": base64.b64encode(json.dumps(doc).encode()).decode()}
    env.update(kw)
    return env


def _limits_doc():
    """A valid limits block, taken from the risk-config test kit (never invented here)."""
    import sxbet_signer_kit as kit
    return dict(kit.GENEROUS)


# ---------------------------------------------------------------- preflight
def test_preflight_all_pass_reports_no_order_and_no_secret():
    out = cert.preflight(env_for(), lambda url: META)
    assert {k: v["outcome"] for k, v in out["results"].items()} == {
        "toronto_metadata": "PASS", "makers": "PASS", "risk_config": "PASS"}
    assert out["ready_for_send"] is True and out["orders_sent"] == 0 and out["signatures"] == 0


def test_preflight_fetches_only_the_toronto_metadata_url():
    seen = []
    cert.preflight(env_for(), lambda url: seen.append(url) or META)
    assert seen == ["https://api.toronto.sx.bet/metadata/obv3"]


@pytest.mark.parametrize("mutate, reason", [
    (lambda m: m["data"].update(chainId=4162), "METADATA_CHAIN_NOT_TORONTO"),
    (lambda m: m["data"]["activeAsset"].update(baseToken="0x6629Ce1Cf35Cc1329ebB4F63202F3f197b3F050B"),
     "METADATA_TOKEN_NOT_TORONTO_USDC"),
    (lambda m: m.update(status="failure"), "METADATA_REPLY_NOT_SUCCESS"),
])
def test_preflight_fails_on_any_non_toronto_metadata(mutate, reason):
    meta = json.loads(json.dumps(META))
    mutate(meta)
    out = cert.preflight(env_for(), lambda url: meta)
    assert out["results"]["toronto_metadata"] == {"outcome": "FAIL", "reason": reason}
    assert out["ready_for_send"] is False


def test_unreachable_metadata_is_fail_with_type_only():
    def boom(url):
        raise ConnectionError("secret-ish detail")
    out = cert.preflight(env_for(), boom)
    assert out["results"]["toronto_metadata"]["reason"] == "METADATA_UNREACHABLE"
    assert "secret-ish" not in json.dumps(out)


@pytest.mark.parametrize("kw, outcome, reason", [
    ({"SXBET_TORONTO_MAKER_ADDRESS": ""}, "NOT_CONFIGURED", "MAKER_VARIABLES_MISSING"),
    ({"SXBET_LIVE_MAKER_ADDRESS": "0x" + "4c" * 20}, "FAIL", "TESTNET_MAKER_IS_THE_LIVE_MAKER"),
    ({"SXBET_TORONTO_MAKER_ADDRESS": "0x123"}, "FAIL", "MAKER_ADDRESS_MALFORMED"),
])
def test_makers(kw, outcome, reason):
    out = cert.check_makers(env_for(**kw))
    assert (out["outcome"], out["reason"]) == (outcome, reason)


def test_risk_config_must_be_owner_signed_testnet_only():
    owner = Ed25519PrivateKey.generate()
    assert cert.check_risk_config(env_for(owner))["outcome"] == "PASS"
    other = Ed25519PrivateKey.generate()
    env = env_for(owner)
    env["SXBET_OWNER_ED25519_PUBLIC_KEY_HEX"] = other.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw).hex()
    assert cert.check_risk_config(env)["reason"] == "RISK_CONFIG_SIGNATURE_INVALID"
    both = env_for(owner, networks={"testnet": _limits_doc(), "mainnet": _limits_doc()})
    assert cert.check_risk_config(both)["reason"] == "RISK_CONFIG_ALSO_CONFIGURES_MAINNET"
    assert cert.check_risk_config(env_for(owner, SXBET_TORONTO_RISK_CONFIG_B64=""))["outcome"] == "NOT_CONFIGURED"
    assert cert.check_risk_config(env_for(owner, SXBET_TORONTO_RISK_CONFIG_B64="!!"))["reason"] == \
        "RISK_CONFIG_NOT_BASE64"


def test_missing_configuration_is_never_ready():
    out = cert.preflight({}, lambda url: META)
    assert out["ready_for_send"] is False and out["results"]["makers"]["outcome"] == "NOT_CONFIGURED"


def test_send_always_refuses_today(tmp_path):
    proc = subprocess.run([sys.executable, str(SCRIPT), "--send", "--output", str(tmp_path / "x.json")],
                          capture_output=True, text=True, timeout=60,
                          env={"PYTHONPATH": str(ROOT), "SXBET_SIGNER_PROCESS": "1", "PATH": "/usr/bin:/bin"})
    assert proc.returncode == 3 and "SEND_PATH_NOT_ENABLED_PENDING_OWNER_GO" in proc.stdout


def test_script_sends_nothing_and_reads_no_key():
    src = SCRIPT.read_text(encoding="utf-8").split('"""', 2)[2]
    for banned in ("PRIVATE_KEY", "TorontoRestSender(", "sign_order", ".post(", ".delete(", "secrets",
                   "api.sx.bet\"", "writes_enabled"):
        assert banned not in src, banned
    assert "follow_redirects=False" in src


# ---------------------------------------------------------------- workflow
def test_workflow_is_manual_main_only_protected_and_secret_free():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    triggers = data.get("on", data.get(True))
    assert set(triggers) == {"workflow_dispatch"}
    text = WORKFLOW.read_text(encoding="utf-8")
    assert "secrets." not in text.lower().replace("no secret", "")
    for name, job in data["jobs"].items():
        assert job["environment"] == "sxbet-toronto", name
        assert "github.ref == 'refs/heads/main'" in str(job["if"]), name
    send_if = str(data["jobs"]["send"]["if"])
    assert "SEND TESTNET ORDERS" in send_if and "vars.SXBET_TORONTO_ORDERS_ENABLED == 'true'" in send_if
    assert data["jobs"]["send"]["needs"] == "preflight"
    assert "--send" in yaml.safe_dump(data["jobs"]["send"]["steps"])


# ---------------------------------------------------------------- S8.2 review P2-1 (RED before the fix: exit 0)
def test_exit_code_is_zero_only_when_every_check_passes():
    ok = cert.preflight(env_for(), lambda url: META)
    assert cert.exit_code(ok) == 0
    assert cert.exit_code(cert.preflight({}, lambda url: META)) == 2          # NOT_CONFIGURED is not ready
    bad = json.loads(json.dumps(META))
    bad["data"]["chainId"] = 4162
    assert cert.exit_code(cert.preflight(env_for(), lambda url: bad)) == 1
    forged = dict(ok, ready_for_send=True,
                  results=dict(ok["results"], makers={"outcome": "NOT_CONFIGURED", "reason": "X"}))
    assert cert.exit_code(forged) != 0


def test_unconfigured_preflight_script_exits_non_zero(tmp_path):
    proc = subprocess.run([sys.executable, str(SCRIPT), "--output", str(tmp_path / "p.json")],
                          capture_output=True, text=True, timeout=60,
                          env={"PYTHONPATH": str(ROOT), "SXBET_SIGNER_PROCESS": "1", "PATH": "/usr/bin:/bin",
                               "HTTPS_PROXY": "http://127.0.0.1:9"})        # unreachable: no network in tests
    assert proc.returncode != 0


def test_send_job_requires_a_successful_ready_preflight_and_both_gates_run_first():
    data = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    pre = data["jobs"]["preflight"]
    assert pre["outputs"]["ready_for_send"] == "${{ steps.ready.outputs.ready_for_send }}"
    send_if = str(data["jobs"]["send"]["if"])
    assert "needs.preflight.outputs.ready_for_send == 'true'" in send_if
    assert "needs.preflight.result == 'success'" in send_if
    for name, job in data["jobs"].items():
        runs = [str(s.get("run", "")) for s in job["steps"]]
        gate = next(i for i, r in enumerate(runs) if "require_owner_main_dispatch.sh" in r)
        assert "require_protected_environment.sh" in runs[gate] and "sxbet-toronto" in runs[gate], name
        python_steps = [i for i, r in enumerate(runs) if "sxbet_toronto_certification.py" in r]
        assert python_steps and all(i > gate for i in python_steps), name


@pytest.mark.parametrize("maker", ["0x" + "00" * 20, "0x1BC6326EA6aF2aB8E4b6Bc83418044B1923b2956",
                                   "0x0Db3aBA7de0BCb41FF99D35306f366cFFEEede38"])
def test_maker_cannot_be_zero_or_a_toronto_contract(maker):
    assert cert.check_makers(env_for(SXBET_TORONTO_MAKER_ADDRESS=maker))["reason"] == "MAKER_IS_NOT_AN_EOA"


@pytest.mark.parametrize("value", [79479957.0, "79479957", True])
def test_metadata_chain_id_must_be_an_exact_int(value):
    meta = json.loads(json.dumps(META))
    meta["data"]["chainId"] = value
    assert cert.preflight(env_for(), lambda url: meta)["results"]["toronto_metadata"]["outcome"] == "FAIL"
    meta = json.loads(json.dumps(META))
    meta["data"]["domain"]["chainId"] = value
    assert cert.preflight(env_for(), lambda url: meta)["results"]["toronto_metadata"]["outcome"] == "FAIL"



def test_exit_code_requires_the_exact_check_set():
    """Review b7dc624 P3-7: a 'ready' report with missing checks is never 0."""
    assert cert.exit_code({"ready_for_send": True, "results": {}}) != 0
    ok = cert.preflight(env_for(), lambda url: META)
    partial = dict(ok, results={k: v for k, v in ok["results"].items() if k != "risk_config"})
    assert cert.exit_code(partial) != 0
    wf = WORKFLOW.read_text(encoding="utf-8")
    assert '{"toronto_metadata","makers","risk_config"}' in wf
