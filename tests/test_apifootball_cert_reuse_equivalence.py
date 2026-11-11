"""AF-01: deterministic proof for reusing the 2026-11-11 MCP evidence (848c007) on current code.

A row of docs/certification/apifootball/matrix.csv is REUSED_EQUIVALENT only if
  (1) the API-FOOTBALL code path changed since 848c007 exactly in the files the matrix records, and
  (2) the recorded provider envelope, replayed through the CURRENT bridge (real resilience client and
      transport over httpx.MockTransport), gives the recorded MCP reply on every recorded field.
No network: the provider is httpx.MockTransport.
"""
from __future__ import annotations

import asyncio
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import httpx
import pytest

from bridge.api_football.config import OFFICIAL_BASE_URL, Settings
from bridge.api_football.quota import QuotaLease
from bridge.api_football.resilience import ResilientFootballClient, RetryPolicy
from bridge.api_football.tools import register_tools

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "apifootball_cert_20261111"
KEY = "LOCAL_FAKE_KEY_REUSE_EQUIVALENCE_ONLY"
COMPARED = ("status", "parameters", "pagination", "pagination_state", "warnings", "network_calls_made")

_SPEC = importlib.util.spec_from_file_location("apifootball_cert_matrix_reuse", ROOT / "scripts" / "apifootball_cert_matrix.py")
matrix = importlib.util.module_from_spec(_SPEC)
sys.modules["apifootball_cert_matrix_reuse"] = matrix
_SPEC.loader.exec_module(matrix)


class _Quota:
    async def acquire(self, secret):
        return QuotaLease("l", "h")

    async def release(self, lease):
        return None

    async def mark_exhausted(self, secret, kind):
        return None


class _Server:
    def __init__(self):
        self.tools = {}

    def tool(self):
        def deco(fn):
            self.tools[fn.__name__] = fn
            return fn
        return deco


def _replay(name: str):
    case = json.loads((FIXTURES / name).read_text(encoding="utf-8"))
    body, observed = case["envelope"], case["observed_mcp_reply"]
    seen = []

    def handler(request):
        seen.append(request.url.path)
        return httpx.Response(200, json=body, headers={"x-ratelimit-requests-remaining": "7497",
                                                       "x-ratelimit-remaining": "297"})
    from bridge.api_football.transport import InjectedHTTPXTransport
    settings = Settings(enabled=True, api_key=KEY, live_authorized=True, cache_enabled=False)
    http = httpx.AsyncClient(base_url=OFFICIAL_BASE_URL, transport=httpx.MockTransport(handler))
    client = ResilientFootballClient(settings, InjectedHTTPXTransport(http, api_key=KEY), quota=_Quota(),
                                     retry=RetryPolicy(max_attempts=1, base_delay=0.0, max_delay=0.0))
    server = _Server()
    register_tools(server, settings, client=client)
    out = asyncio.run(server.tools[observed["tool"]](**observed["parameters"]))
    assert len(seen) == 1
    return out, observed, body


def _recorded_fields_equal(out, observed):
    return {k: (out.get(k), observed[k]) for k in COMPARED if k in observed and out.get(k) != observed[k]}


def test_a09_replay_equals_recorded_reply():
    out, observed, body = _replay("a09_player_profiles_page101.json")
    assert _recorded_fields_equal(out, observed) == {}
    assert out["data"] == body  # provider data unchanged


def test_b06f_replay_equals_recorded_reply():
    out, observed, body = _replay("b06f_odds_fixture1557464_two_bookmakers.json")
    assert _recorded_fields_equal(out, observed) == {}
    assert out["data"] == body


def test_a10_replay_differs_from_recorded_reply():
    """A10 must stay STALE: e155193 changed exactly this behaviour (LAST_PAGE -> EMPTY_PAGE)."""
    out, observed, _ = _replay("a10_player_profiles_page2771_empty.json")
    assert observed["pagination_state"]["state"] == "LAST_PAGE"
    assert out["pagination_state"]["state"] == "EMPTY_PAGE"


def _has_commit(sha: str) -> bool:
    try:
        return subprocess.run(["git", "cat-file", "-e", sha + "^{commit}"], cwd=ROOT,
                              capture_output=True, timeout=30).returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def test_code_path_changes_since_source_sha_are_the_recorded_ones():
    if not _has_commit(matrix.REUSE_SOURCE_SHA):
        pytest.skip("848c007 not in local history (shallow clone)")
    changed = subprocess.run(["git", "diff", "--name-only", matrix.REUSE_SOURCE_SHA, "HEAD", "--", *matrix.CODE_PATH],
                             cwd=ROOT, capture_output=True, text=True, check=True, timeout=60).stdout.split()
    # A new change on the code path invalidates every REUSED_EQUIVALENT row until re-evaluated.
    assert tuple(sorted(changed)) == tuple(sorted(matrix.CODE_PATH_CHANGES))


def test_every_reuse_decision_has_a_proof_or_stays_stale():
    for key, (status, reason, proof) in matrix._REUSE.items():
        assert status in ("REUSED_EQUIVALENT", "STALE") and reason, key
        if status == "REUSED_EQUIVALENT":
            path, name = proof.split("::")
            assert path == "tests/test_apifootball_cert_reuse_equivalence.py"
            assert name in globals() and name.endswith("_equals_recorded_reply"), key
            assert key[0] not in ("apifootball_fixtures",), "route modified by e155193"
