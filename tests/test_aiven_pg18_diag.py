"""Offline tests for scripts/aiven_pg18_diag.py (PG18 test workflow diagnostics).

The redaction tests use synthetic URIs only; no real host, user or password.
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "aiven_pg18_diag.py"
WORKFLOW = ROOT / ".github" / "workflows" / "fcb-aiven-pg18-test.yml"

spec = importlib.util.spec_from_file_location("aiven_pg18_diag", SCRIPT)
diag = importlib.util.module_from_spec(spec)
spec.loader.exec_module(diag)

HOST = "svc-proj-1234.d.aivencloud.com"
USER = "synthuser"
PASSWORD = "S3cr3t%2Fpw"  # percent-encoded "/"
GOOD = f"postgres://{USER}:{PASSWORD}@{HOST}:14274/fcb_pg18_test?sslmode=require"


@pytest.mark.parametrize(
    "raw,expected",
    [
        (GOOD, None),
        (GOOD + "\n", "trailing_whitespace_or_newline"),
        (GOOD + "\r\n", "trailing_whitespace_or_newline"),
        (GOOD + " ", "trailing_whitespace_or_newline"),
        (" " + GOOD, "leading_whitespace"),
        (GOOD.replace("@", " @", 1), "embedded_whitespace_or_control_char"),
        (GOOD.replace("?", "\t?"), "embedded_whitespace_or_control_char"),
        (" " + GOOD + "\n", "embedded_whitespace_or_control_char"),
        (GOOD + "\x7f", "trailing_whitespace_or_newline"),
    ],
)
def test_raw_problem(raw, expected):
    assert diag.raw_problem(raw) == expected


def test_sanitize_redacts_every_connection_value():
    msg = (
        f'psql: error: connection to server at "{HOST}" (203.0.113.7), port 14274 failed: '
        f'FATAL:  password authentication failed for user "{USER}" S3cr3t/pw {PASSWORD}\n'
        'connection to server at "x" (2001:db8::1), port 14274 failed: Network is unreachable\n'
        'database "fcb_pg18_test" does not exist'
    )
    out = diag.sanitize(msg, GOOD)
    for secret in (HOST, USER, PASSWORD, "S3cr3t/pw", "203.0.113.7", "2001:db8::1", "14274",
                   "fcb_pg18_test", "aivencloud"):
        assert secret not in out
    assert "password authentication failed" in out
    assert "Network is unreachable" in out
    assert "\n" not in out and len(out) <= diag.MAX_LINE


def test_sanitize_redacts_unknown_hosts_and_ips_even_if_not_in_secret():
    out = diag.sanitize('at "other.host.example.org" (10.1.2.3) and fe80::1:2 port 5432', GOOD)
    assert "other.host" not in out and "10.1.2.3" not in out and "fe80" not in out
    assert "5432" not in out


def test_sanitize_keeps_libpq_reason_for_whitespace_case():
    out = diag.sanitize('psql: error: invalid sslmode value: "require\n"', GOOD + "\n")
    assert out.startswith("psql: error: invalid sslmode value")


def _run(cmd, env_url, *args):
    env = {"FCB_PG18_TEST_URL": env_url, "PATH": "/usr/bin:/bin"}
    return subprocess.run([sys.executable, "-I", "-S", str(SCRIPT), cmd, *args],
                          env=env, capture_output=True, text=True)


def test_cli_check_raw_rejects_trailing_newline_without_leaking():
    r = _run("check-raw", GOOD + "\n")
    assert r.returncode == 1
    assert "trailing_whitespace_or_newline" in r.stderr
    for secret in (HOST, USER, PASSWORD):
        assert secret not in r.stderr + r.stdout


def test_cli_check_raw_accepts_clean_uri():
    assert _run("check-raw", GOOD).returncode == 0


def test_cli_probe_reports_categories_only():
    r = _run("probe", "postgres://u:p@127.0.0.1:1/fcb_pg18_test")
    assert r.returncode == 0
    assert "dns=ok ipv4_addresses=1 ipv6_addresses=0" in r.stdout
    assert "tcp ipv4#0=refused" in r.stdout
    assert "127.0.0.1" not in r.stdout


@pytest.mark.skipif(shutil.which("psql") is None, reason="psql not installed")
def test_real_libpq_message_for_trailing_newline_is_sanitized_and_classified(tmp_path):
    bad = "postgres://u:p@127.0.0.1:1/fcb_pg18_test?sslmode=require\n"
    err = tmp_path / "err.txt"
    with err.open("w") as fh:
        subprocess.run(["psql", bad, "-X", "-At", "-c", "select 1"], stdout=subprocess.DEVNULL,
                       stderr=fh, env={"PATH": "/usr/bin:/bin"})
    r = _run("sanitize", bad, str(err))
    assert "invalid sslmode value" in r.stdout
    assert "127.0.0.1" not in r.stdout


def test_workflow_wires_diagnostics_and_never_prints_raw_psql_stderr():
    wf = WORKFLOW.read_text()
    assert "scripts/aiven_pg18_diag.py check-raw" in wf
    assert "scripts/aiven_pg18_diag.py probe" in wf
    assert wf.count("scripts/aiven_pg18_diag.py sanitize") == 2
    # every psql invocation with the secret redirects stderr to a temp file
    lines = [ln for ln in wf.splitlines() if '"$PSQL" "$FCB_PG18_TEST_URL"' in ln]
    assert lines and all('2>"$err"' in ln for ln in lines)
    # the target check is not loosened
    assert "EXPECTED_DB: fcb_pg18_test" in wf
    assert "if dbpath != expected:" in wf
    assert "0008|signer_ledger" in wf


def test_sanitize_is_case_insensitive_and_covers_single_dot_hosts():
    raw = "postgres://u:pw@MyHost.com:5/fcb_pg18_test"
    out = diag.sanitize('connection to server at "MyHost.com" (::1), port 5 failed', raw)
    assert "myhost" not in out.lower() and "5 failed" not in out


def test_sanitize_covers_underscore_hosts_and_short_values():
    raw = "postgres://a:pw@svc_x.aivencloud.com:5/fcb_pg18_test"
    out = diag.sanitize('at "SVC_X.aivencloud.com" [::1]:5 user "a" failed', raw)
    assert "svc_x" not in out.lower() and 'user "a"' not in out and "]:5" not in out
    assert "failed" in out


def test_sanitize_keeps_pg_hba_conf_readable():
    out = diag.sanitize('FATAL: no pg_hba.conf entry for host "1.2.3.4"', GOOD)
    assert "pg_hba.conf" in out and "1.2.3.4" not in out


def test_probe_reports_host_port_match_without_values(monkeypatch):
    import hashlib
    monkeypatch.setenv("EXPECTED_HOST_SHA256", hashlib.sha256(b"127.0.0.1").hexdigest())
    monkeypatch.setenv("EXPECTED_PORT_SHA256", hashlib.sha256(b"2").hexdigest())
    lines = diag.probe("postgres://u:p@127.0.0.1:1/fcb_pg18_test", timeout=1)
    assert lines[0] == "host_matches_service=yes port_matches_service=no"
    assert not any("127.0.0.1" in ln for ln in lines)


def test_probe_match_unset_without_pins(monkeypatch):
    monkeypatch.delenv("EXPECTED_HOST_SHA256", raising=False)
    monkeypatch.delenv("EXPECTED_PORT_SHA256", raising=False)
    lines = diag.probe("postgres://u:p@127.0.0.1:1/fcb_pg18_test", timeout=1)
    assert lines[0] == "host_matches_service=unset port_matches_service=unset"


def test_workflow_pins_service_host_and_port_hashes():
    wf = WORKFLOW.read_text()
    assert "EXPECTED_HOST_SHA256: " in wf and "EXPECTED_PORT_SHA256: " in wf


# --- Explicit port 14274 (public port of the Aiven service; synthetic host/user/password) ---
import hashlib as _hl  # noqa: E402

_SVC_HOST = "svc-synthetic-0000.e.aivencloud.com"
_PINS = {
    "EXPECTED_HOST_SHA256": _hl.sha256(_SVC_HOST.encode()).hexdigest(),
    "EXPECTED_PORT_SHA256": _hl.sha256(b"14274").hexdigest(),
}


def test_pinned_port_hash_in_workflow_is_sha256_of_14274():
    wf = WORKFLOW.read_text()
    assert f"EXPECTED_PORT_SHA256: {_hl.sha256(b'14274').hexdigest()}" in wf


@pytest.mark.parametrize(
    "uri",
    [
        f"postgres://avnadmin:Synth3tic@{_SVC_HOST}:14274/fcb_pg18_test?sslmode=require",
        f"postgresql://avnadmin:Synth3tic@{_SVC_HOST}:14274/fcb_pg18_test",
        f"postgres://avnadmin:Syn%40th@{_SVC_HOST}:14274/fcb_pg18_test?sslmode=verify-full",
        f"postgres://avnadmin:Synth3tic@{_SVC_HOST.upper()}:14274/fcb_pg18_test",
        f"postgres://avnadmin:Synth3tic@{_SVC_HOST}:014274/fcb_pg18_test",
    ],
)
def test_explicit_port_14274_matches(monkeypatch, uri):
    for k, v in _PINS.items():
        monkeypatch.setenv(k, v)
    first = diag.probe(uri, timeout=0.01)[:2]
    assert first == ["host_matches_service=yes port_matches_service=yes",
                     "port_form=explicit public_port_in=netloc_port"]


@pytest.mark.parametrize(
    "uri,layout",
    [
        (f"postgres://avnadmin:Synth3tic@{_SVC_HOST}/fcb_pg18_test?sslmode=require",
         "port_form=absent public_port_in=none"),
        (f"postgres://avnadmin:Synth3tic@{_SVC_HOST}:/fcb_pg18_test",
         "port_form=empty public_port_in=none"),
        (f"postgres://avnadmin:Synth3tic@{_SVC_HOST}:5432/fcb_pg18_test",
         "port_form=explicit public_port_in=none"),
        (f"postgres://avnadmin:Synth3tic@{_SVC_HOST}:14247/fcb_pg18_test",
         "port_form=explicit public_port_in=none"),
        # 14274 present in the string but not in the port position
        (f"postgres://avnadmin:Synth3tic:14274@{_SVC_HOST}/fcb_pg18_test",
         "port_form=absent public_port_in=userinfo"),
        (f"postgres://avnadmin:Synth3tic@{_SVC_HOST}/fcb_pg18_test:14274",
         "port_form=absent public_port_in=path"),
    ],
)
def test_port_not_14274_does_not_match_and_layout_says_where(monkeypatch, uri, layout):
    for k, v in _PINS.items():
        monkeypatch.setenv(k, v)
    first = diag.probe(uri, timeout=0.01)[:2]
    assert first == ["host_matches_service=yes port_matches_service=no", layout]
    assert all("Synth3tic" not in ln and _SVC_HOST not in ln for ln in diag.probe(uri, timeout=0.01))
