"""FIX 4: the signer is a separate service and never runs inside the MCP / Render process.

(a) `import signer` fails closed with RENDER set, or without SXBET_SIGNER_PROCESS=1;
(b) bridge.server imported with RENDER=true never loads signer.* or eth_account, and no MCP tool
    accepts keys, approvals or signatures;
(c) the Render runtime requirements never contain signing libraries;
(d) no bridge/ module imports signer (AST + text, including dynamic imports).
"""
import ast
import json
import os
import pathlib
import re
import subprocess
import sys
import textwrap

ROOT = pathlib.Path(__file__).resolve().parents[1]


def _env(**extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("APIFOOTBALL_", "SXBET_", "RENDER")) and k != "DATABASE_URL"}
    env.update({"PYTHONPATH": str(ROOT), **extra})
    return env


def _import_signer(env):
    return subprocess.run([sys.executable, "-c", "import signer"], env=env, capture_output=True, text=True,
                          timeout=60, cwd=str(ROOT))


# ---------------------------------------------------------------- (a)
def test_signer_import_refused_on_render_even_with_opt_in():
    proc = _import_signer(_env(RENDER="true", SXBET_SIGNER_PROCESS="1"))
    assert proc.returncode != 0 and "SXBET_SIGNER_IMPORT_REFUSED" in proc.stderr


def test_signer_import_refused_without_explicit_process_opt_in():
    for value in (None, "0", "true", "yes", ""):
        env = _env() if value is None else _env(SXBET_SIGNER_PROCESS=value)
        proc = _import_signer(env)
        assert proc.returncode != 0 and "SXBET_SIGNER_IMPORT_REFUSED" in proc.stderr, value


def test_signer_import_refused_for_any_render_value():
    proc = _import_signer(_env(RENDER="", SXBET_SIGNER_PROCESS="1"))
    assert proc.returncode != 0


def test_signer_submodules_cannot_bypass_the_guard():
    for mod in ("signer.core", "signer.gateway", "signer.orders", "signer.risk", "signer.eip712"):
        proc = subprocess.run([sys.executable, "-c", f"import {mod}"], env=_env(RENDER="true"),
                              capture_output=True, text=True, timeout=60, cwd=str(ROOT))
        assert proc.returncode != 0 and "SXBET_SIGNER_IMPORT_REFUSED" in proc.stderr, mod


def test_signer_import_allowed_only_in_signer_process():
    proc = _import_signer(_env(SXBET_SIGNER_PROCESS="1"))
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------- (b)
SCRIPT = textwrap.dedent(r'''
    import asyncio, json, sys
    import bridge.server as srv
    from starlette.testclient import TestClient

    async def noop(*a, **k):
        return None

    async def idle():
        await asyncio.Event().wait()

    async def stop_all():
        return {"failed": []}

    srv.bridge.prepare_cloak_runtime = noop
    srv.bridge.reaper_loop = idle
    srv.bridge.stop_all = stop_all
    srv.oauth_store.valid_access_token = lambda token, resource, scope: token == "TEST_TOKEN_ONLY"
    host = srv.public_host
    auth = {"Accept": "application/json, text/event-stream", "Content-Type": "application/json",
            "Host": host, "Authorization": "Bearer TEST_TOKEN_ONLY"}
    with TestClient(srv.app, base_url="https://" + host) as c:
        r = c.post("/mcp/", headers=auth, json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}})
        tools = r.json()["result"]["tools"]
    loaded = sorted(m for m in sys.modules if m == "signer" or m.startswith(("signer.", "eth_account", "eth_keys",
                                                                                 "web3")))
    print("RESULT_JSON=" + json.dumps({"tools": tools, "loaded": loaded}))
''')

# Keyboard keys (browser_press.key) and pagination/idempotency keys are not credentials.
FORBIDDEN_PARAM = re.compile(r"private|secret|signature|approval|seed|mnemonic|signing|signer|keystore|"
                             r"api_?key|priv_?key|wallet_?key|passphrase|password", re.I)


def test_bridge_on_render_never_loads_signer_and_exposes_no_signing_inputs():
    env = _env(RENDER="true", SXBET_SIGNER_PROCESS="1", APIFOOTBALL_ENABLED="true",
               APIFOOTBALL_LIVE_AUTHORIZED="false", SXBET_ENABLED="true", SXBET_API_KEY="FAKE_KEY_RENDER_ISOLATION")
    proc = subprocess.run([sys.executable, "-c", SCRIPT], env=env, capture_output=True, text=True, timeout=120,
                          cwd=str(ROOT))
    line = next((l for l in proc.stdout.splitlines() if l.startswith("RESULT_JSON=")), None)
    assert line, proc.stdout[-2000:] + proc.stderr[-2000:]
    out = json.loads(line[len("RESULT_JSON="):])
    assert out["loaded"] == []
    names = [t["name"] for t in out["tools"]]
    assert sum(n.startswith("sxbet_") for n in names) == 37  # unchanged catalogue
    offenders = []
    for t in out["tools"]:
        for p in (t.get("inputSchema") or {}).get("properties", {}):
            if FORBIDDEN_PARAM.search(p):
                offenders.append((t["name"], p))
        # Financial-operation names among the SX Bet tools (apifootball_transfers = football player transfers).
        if t["name"].startswith("sxbet_") and re.search(r"sign|submit|place|cancel|approve|withdraw|transfer|deposit",
                                                          t["name"]):
            offenders.append((t["name"], "<name>"))
    assert offenders == []


# ---------------------------------------------------------------- (c)
def test_render_requirements_never_contain_signing_libraries():
    runtime = (ROOT / "requirements.txt").read_text(encoding="utf-8").lower()
    for lib in ("eth-account", "eth_account", "web3", "eth-keys", "eth_keys", "py-evm"):
        assert lib not in runtime, lib
    assert "eth-account" in (ROOT / "signer" / "requirements.txt").read_text(encoding="utf-8")


# ---------------------------------------------------------------- (d)
def test_no_bridge_module_imports_signer():
    files = list((ROOT / "bridge").rglob("*.py")) + list((ROOT / "app").rglob("*.py"))
    assert files
    for p in files:
        src = p.read_text(encoding="utf-8")
        for node in ast.walk(ast.parse(src)):
            if isinstance(node, ast.Import):
                assert not any(a.name.split(".")[0] in ("signer", "eth_account") for a in node.names), p
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in ("signer", "eth_account"), p
        assert not re.search(r"""import_module\(\s*['"]signer|__import__\(\s*['"]signer""", src), p


def test_signer_and_gateway_have_no_network_code():
    banned = {"bridge", "httpx", "requests", "urllib", "urllib3", "http", "socket", "aiohttp", "websockets",
              "ssl", "asyncio", "web3"}
    for p in (ROOT / "signer").rglob("*.py"):
        for node in ast.walk(ast.parse(p.read_text(encoding="utf-8"))):
            mods = [a.name for a in node.names] if isinstance(node, ast.Import) else \
                [node.module] if isinstance(node, ast.ImportFrom) and node.module else []
            for m in mods:
                assert m.split(".")[0] not in banned, (p, m)
