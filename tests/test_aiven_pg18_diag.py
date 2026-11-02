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
