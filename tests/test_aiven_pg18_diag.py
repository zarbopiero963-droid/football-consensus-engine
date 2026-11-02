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


def test_workflow_writes_only_with_explicit_apply_mode():
    import yaml
    wf = yaml.safe_load(WORKFLOW.read_text())
    on = wf.get("on", wf.get(True))
    assert set(on) == {"workflow_dispatch"}  # manual only
    mode = on["workflow_dispatch"]["inputs"]["mode"]
    assert mode["default"] == "diagnose" and mode["options"] == ["diagnose", "apply"]
    steps = wf["jobs"]["pg18-test"]["steps"]
    writers = [s for s in steps if '-f "$SCRIPT"' in s.get("run", "")]
    assert writers, "apply step not found"
    for s in writers:
        assert s.get("if") == "${{ inputs.mode == 'apply' }}"
    # every step before the apply step that uses the secret is read-only (no -f script, no -1)
    for s in steps[: steps.index(writers[0])]:
        assert '-f "$SCRIPT"' not in s.get("run", "")


def test_pgdg_install_retries_but_keeps_pinned_fingerprint():
    wf = WORKFLOW.read_text()
    assert "--retry 5" in wf and "--retry-all-errors" in wf and "--max-time 60" in wf
    assert "--insecure" not in wf and " -k " not in wf
    assert "Acquire::Retries=5" in wf
    assert 'test "$fpr" = "B97B0AFCAA1A47F044F244A07FCC7D46ACCC4CF8"' in wf


# --- target_check: pinned host, exact database, port from pinned config (synthetic values) ---
_T_HOST = "svc-target-0000.e.aivencloud.com"


def _tenv(**over):
    env = {
        "EXPECTED_DB": "fcb_pg18_test",
        "EXPECTED_HOST_SHA256": _hl.sha256(_T_HOST.encode()).hexdigest(),
        "EXPECTED_PORT": "14274",
        "EXPECTED_PORT_SHA256": _hl.sha256(b"14274").hexdigest(),
        "PGPORT": "14274",
    }
    env.update(over)
    return {k: v for k, v in env.items() if v is not None}


_SPECIAL_PW = [
    "S3cr3t",
    "p%40ss%3Aw%2Fo%3Frd%23",        # encoded @ : / ? #
    "%25%26%2B%3D%24%21%2A%27%28%29",  # encoded % & + = $ ! * ' ( )
    "a~b_c-d.e",                     # unreserved punctuation
    "%C3%A8%E2%82%AC",               # UTF-8 (è €)
]


@pytest.mark.parametrize("pw", _SPECIAL_PW)
@pytest.mark.parametrize("hostport,source", [
    (f"{_T_HOST}:14274", "uri"),
    (f"{_T_HOST}:", "config(empty)"),
    (_T_HOST, "config(absent)"),
])
def test_target_accepts_pinned_host_db_and_port_policy(pw, hostport, source):
    uri = f"postgres://avnadmin:{pw}@{hostport}/fcb_pg18_test?sslmode=require"
    ok, msg = diag.target_check(uri, _tenv())
    assert ok, msg
    assert msg == f"host=expected_service database=fcb_pg18_test port=14274 port_source={source}"
    assert pw not in msg and _T_HOST not in msg.lower()


@pytest.mark.parametrize("uri,reason", [
    (f"postgres://u:p@{_T_HOST}:5432/fcb_pg18_test", "port_mismatch_explicit"),
    (f"postgres://u:p@{_T_HOST}:14275/fcb_pg18_test", "port_mismatch_explicit"),
    (f"postgres://u:p@{_T_HOST}:0/fcb_pg18_test", "port_mismatch_explicit"),
    (f"postgres://u:p@{_T_HOST}:99999/fcb_pg18_test", "port_mismatch_explicit"),
    (f"postgres://u:p@{_T_HOST}:014274/fcb_pg18_test", "port_mismatch_explicit"),
    (f"postgres://u:p@{_T_HOST}:+14274/fcb_pg18_test", "port_invalid"),
    # host must be the pinned lowercase ASCII name, byte for byte
    (f"postgres://u:p@{_T_HOST.upper()}:/fcb_pg18_test", "host_not_expected_service"),
    (f"postgres://u:p@{_T_HOST}.:/fcb_pg18_test", "host_not_expected_service"),
    (f"postgres://u:p@[::1]:/fcb_pg18_test", "host_not_expected_service"),
    # P0 (review of #163): '#' hides query keys from Python's urlsplit but not from libpq
    (f"postgres://u:s3cr%23et@{_T_HOST}/fcb_pg18_test?application_name=x#&host=127.0.0.9&port=6666&sslmode=disable",
     "fragment_not_allowed"),
    (f"postgres://u:p@{_T_HOST}/fcb_pg18_test?sslmode=require#&hostaddr=203.0.113.7", "fragment_not_allowed"),
    (f"postgres://u:p@{_T_HOST}/fcb_pg18_test#&sslmode=disable", "fragment_not_allowed"),
    (f"postgres://u:p@{_T_HOST}/fcb_pg18_test#&dbname=defaultdb", "fragment_not_allowed"),
    # P1 (review of #163): libpq keeps the LAST duplicate; parse_qs decodes key escapes
    (f"postgres://u:p@{_T_HOST}/fcb_pg18_test?sslmode=require&sslmode=disable", "query_not_allowed"),
    (f"postgres://u:p@{_T_HOST}/fcb_pg18_test?sslmode=require&sslm%6Fde=disable", "query_not_allowed"),
    (f"postgres://u:p@{_T_HOST}/fcb_pg18_test?sslm%6Fde=require", "query_not_allowed"),
    (f"postgres://u:p@{_T_HOST}/fcb_pg18_test?application_name=x", "query_not_allowed"),
    (f"postgres://u:p@{_T_HOST}/fcb_pg18_test?", "query_not_allowed"),
    (f"postgres://u:p@{_T_HOST}/fcb_pg18_test?sslmode", "query_not_allowed"),
    (f"postgres://u:p@{_T_HOST}:14274x/fcb_pg18_test", "port_invalid"),
    ("postgres://u:p@evil.example.com:14274/fcb_pg18_test", "host_not_expected_service"),
    ("postgres://u:p@evil.example.com/fcb_pg18_test", "host_not_expected_service"),
    (f"postgres://u:p@{_T_HOST}.evil.com:/fcb_pg18_test", "host_not_expected_service"),
    ("postgres://u:p@203.0.113.7:/fcb_pg18_test", "host_not_expected_service"),
    (f"postgres://u:p@{_T_HOST},evil.example.com/fcb_pg18_test", "netloc_not_single_user_at_host"),
    (f"postgres://u:p@x@{_T_HOST}/fcb_pg18_test", "netloc_not_single_user_at_host"),
    (f"postgres://{_T_HOST}/fcb_pg18_test", "netloc_not_single_user_at_host"),
    (f"postgres://u:p@{_T_HOST}/defaultdb", "database_not_expected"),
    (f"postgres://u:p@{_T_HOST}/fcb_pg18_test/", "database_not_expected"),
    (f"postgres://u:p@{_T_HOST}/FCB_PG18_TEST", "database_not_expected"),
    (f"postgres://u:p@{_T_HOST}", "database_not_expected"),
    (f"mysql://u:p@{_T_HOST}/fcb_pg18_test", "scheme_not_postgres"),
    (f"postgres://u:p@{_T_HOST}:/fcb_pg18_test?host=evil.example.com", "query_not_allowed"),
    (f"postgres://u:p@{_T_HOST}:/fcb_pg18_test?PORT=5432", "query_not_allowed"),
    (f"postgres://u:p@{_T_HOST}:/fcb_pg18_test?hostaddr=203.0.113.7", "query_not_allowed"),
    (f"postgres://u:p@{_T_HOST}:/fcb_pg18_test?dbname=defaultdb", "query_not_allowed"),
    (f"postgres://u:p@{_T_HOST}:/fcb_pg18_test?sslmode=disable", "sslmode_too_weak"),
    (f"postgres://u:p@{_T_HOST}:/fcb_pg18_test?sslmode=prefer", "sslmode_too_weak"),
])
def test_target_rejects(uri, reason):
    ok, msg = diag.target_check(uri, _tenv())
    assert not ok and msg.startswith(reason), msg
    assert "evil" not in msg and _T_HOST not in msg and "203.0.113.7" not in msg


@pytest.mark.parametrize("over,reason", [
    ({"EXPECTED_PORT": "5432"}, "config_error: EXPECTED_PORT"),
    ({"EXPECTED_PORT": None}, "config_error: EXPECTED_PORT"),
    ({"PGPORT": "5432"}, "config_error: PGPORT"),
    ({"PGPORT": None}, "config_error: PGPORT"),
    ({"PGHOST": "evil.example.com"}, "config_error: PGHOST"),
    ({"PGHOSTADDR": "203.0.113.7"}, "config_error: PGHOSTADDR"),
    ({"PGSERVICE": "x"}, "config_error: PGSERVICE"),
    ({"PGDATABASE": "defaultdb"}, "config_error: PGDATABASE"),
    ({"EXPECTED_HOST_SHA256": ""}, "host_not_expected_service"),
])
def test_target_rejects_bad_config(over, reason):
    uri = f"postgres://u:p@{_T_HOST}:/fcb_pg18_test"
    ok, msg = diag.target_check(uri, _tenv(**over))
    assert not ok and msg.startswith(reason), msg


def test_cli_target_fails_closed_and_prints_no_secret():
    env = dict(_tenv(), PATH="/usr/bin:/bin",
               FCB_PG18_TEST_URL=f"postgres://avnadmin:Sup3r%40Secret@{_T_HOST}:5432/fcb_pg18_test")
    r = subprocess.run([sys.executable, "-I", "-S", str(SCRIPT), "target"], env=env,
                       capture_output=True, text=True)
    assert r.returncode == 1 and "port_mismatch_explicit" in r.stderr
    out = r.stdout + r.stderr
    assert "Sup3r" not in out and _T_HOST not in out and "avnadmin" not in out


def test_probe_uses_pgport_when_uri_port_empty(monkeypatch):
    monkeypatch.setenv("EXPECTED_HOST_SHA256", _hl.sha256(b"127.0.0.1").hexdigest())
    monkeypatch.setenv("EXPECTED_PORT_SHA256", _hl.sha256(b"14274").hexdigest())
    monkeypatch.setenv("PGPORT", "14274")
    lines = diag.probe("postgres://u:p@127.0.0.1:/fcb_pg18_test", timeout=0.5)
    assert lines[0] == "host_matches_service=yes port_matches_service=yes"
    assert lines[1] == "port_form=empty public_port_in=none"


@pytest.mark.skipif(shutil.which("psql") is None, reason="psql not installed")
@pytest.mark.parametrize("uri", ["postgres://u:p@127.0.0.1:/x", "postgres://u:p@127.0.0.1/x"])
def test_real_libpq_uses_pgport_for_empty_or_absent_uri_port(tmp_path, uri):
    r = subprocess.run(["psql", uri, "-X", "-c", "select 1"], capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "PGPORT": "1", "PGCONNECT_TIMEOUT": "2"})
    assert "port 1 failed" in r.stderr


@pytest.mark.skipif(shutil.which("psql") is None, reason="psql not installed")
def test_real_libpq_explicit_uri_port_overrides_pgport():
    r = subprocess.run(["psql", "postgres://u:p@127.0.0.1:2/x", "-X", "-c", "select 1"],
                       capture_output=True, text=True,
                       env={"PATH": "/usr/bin:/bin", "PGPORT": "1", "PGCONNECT_TIMEOUT": "2"})
    # explicit port wins in libpq: this is why target_check rejects any explicit non-pinned port
    assert "port 2 failed" in r.stderr


def test_workflow_target_policy_wiring():
    import yaml
    wf = yaml.safe_load(WORKFLOW.read_text())
    job = wf["jobs"]["pg18-test"]
    env = job["env"]
    assert env["EXPECTED_PORT"] == "14274" == env["PGPORT"]
    assert env["EXPECTED_PORT_SHA256"] == _hl.sha256(b"14274").hexdigest()
    assert env["EXPECTED_DB"] == "fcb_pg18_test"
    names = [s.get("name", "") for s in job["steps"]]
    mask = job["steps"][names.index("Mask connection details and enforce TLS")]
    assert "aiven_pg18_diag.py target" in mask["run"]
    # the target check runs before any step that connects with the secret
    first_connect = min(i for i, s in enumerate(job["steps"]) if "$PSQL" in s.get("run", ""))
    assert names.index("Mask connection details and enforce TLS") < first_connect
    for s in job["steps"]:
        if "$PSQL" in s.get("run", ""):
            assert s["env"].get("PGSSLMODE") == "require"


_LIBPQ_ACCEPTED = [
    f"postgres://avnadmin:{pw}@{hp}/fcb_pg18_test{q}"
    for pw in _SPECIAL_PW
    for hp in (f"{_T_HOST}:14274", f"{_T_HOST}:", _T_HOST)
    for q in ("", "?sslmode=require", "?sslmode=verify-full")
]

_LIBPQ_ATTACKS = [
    f"postgres://u:s3cr%23et@{_T_HOST}/fcb_pg18_test?application_name=x#&host=127.0.0.9&port=6666&sslmode=disable",
    f"postgres://u:p@{_T_HOST}/fcb_pg18_test?sslmode=require&sslmode=disable",
    f"postgres://u:p@{_T_HOST}/fcb_pg18_test?sslmode=require&sslm%6Fde=disable",
    f"postgres://u:p@{_T_HOST}/fcb_pg18_test?sslmode=require#&dbname=defaultdb",
    f"postgres://u:p@{_T_HOST}/fcb_pg18_test?host=evil.example.com",
    f"postgres://u:p@{_T_HOST}/fcb_pg18_test?h%6Fst=evil.example.com",
    f"postgres://u:p@{_T_HOST}/fcb_pg18_test?hostaddr=203.0.113.7",
    f"postgres://u:p@{_T_HOST}/fcb_pg18_test?port=5432",
    f"postgres://u:p@{_T_HOST}/fcb_pg18_test?service=x",
    f"postgres://u:p@{_T_HOST},evil.example.com/fcb_pg18_test",
]


def _libpq_view(uri):
    conninfo = pytest.importorskip("psycopg.conninfo")
    return conninfo.conninfo_to_dict(uri)


@pytest.mark.parametrize("uri", _LIBPQ_ACCEPTED)
def test_differential_every_accepted_uri_means_the_same_to_libpq(uri):
    """Whatever target_check accepts, libpq's own parser reads as pinned host/db/port/TLS."""
    assert diag.target_check(uri, _tenv())[0]
    d = _libpq_view(uri)
    assert d["host"] == _T_HOST
    assert d["dbname"] == "fcb_pg18_test"
    assert d.get("port") in (None, "", "14274")  # '' / None -> libpq uses PGPORT=14274
    assert d.get("sslmode", "require") in ("require", "verify-ca", "verify-full")
    assert not {"hostaddr", "service", "options", "target_session_attrs"} & set(d)


@pytest.mark.parametrize("uri", _LIBPQ_ATTACKS)
def test_differential_attacks_libpq_would_follow_are_rejected(uri):
    d = _libpq_view(uri)
    redirected = (d.get("host") != _T_HOST or d.get("dbname") != "fcb_pg18_test"
                  or d.get("port") not in (None, "", "14274")
                  or d.get("sslmode", "require") not in ("require", "verify-ca", "verify-full")
                  or bool({"hostaddr", "service"} & set(d)))
    assert redirected, "attack sample no longer redirects libpq; update the sample"
    assert diag.target_check(uri, _tenv())[0] is False
