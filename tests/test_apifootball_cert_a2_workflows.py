"""PATH A2 (owner GO): the two protected A2 workflows and the fail-closed legacy ones, offline.
The real-PostgreSQL proof is tests/integration/test_apifootball_cert_path_a_postgres.py."""
from __future__ import annotations

import hashlib
import re
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
WF = ROOT / ".github" / "workflows"
LEGACY = ("fcb-aiven-defaultdb-apifootball-cert-ledger-0010.yml", "fcb-aiven-defaultdb-apifootball-cert-plan-approve.yml")
INSTALL = WF / "fcb-aiven-apifootball-cert-a2-install.yml"
APPROVE = WF / "fcb-aiven-apifootball-cert-a2-plan-approve.yml"
CODE = "LEGACY_LEDGER_WORKFLOW_DISABLED_PATH_A2"


def _job(path):
    doc = yaml.safe_load(path.read_text(encoding="utf-8"))
    (job,) = doc["jobs"].values()
    return doc, job


def test_legacy_ledger_workflows_fail_closed_first_with_the_fixed_code():
    for name in LEGACY:
        _, job = _job(WF / name)
        first = job["steps"][0]
        assert first["name"] == CODE and "uses" not in first and "env" not in first and "if" not in first
        # nothing before it: no checkout, no secret, no connection
        proc = subprocess.run(["bash", "-e", "-c", first["run"]], capture_output=True, text=True, timeout=30)
        assert proc.returncode == 1 and CODE in proc.stdout, name
        assert "secrets." not in yaml.safe_dump(first)
        assert job.get("if") == "github.ref == 'refs/heads/main'"  # the job keeps its other gates too


def _pins(text):
    block = text.split("EXPECTED_FILE_SHA256: |", 1)[1].split("PGCONNECT_TIMEOUT", 1)[0]
    return dict(line.strip().split("=", 1) for line in block.strip().splitlines())


def test_a2_workflows_are_manual_owner_gated_pinned_and_tls_verify_full():
    for path, phrase, writes in ((INSTALL, "INSTALL API-FOOTBALL CERT LEDGER A2 IN fcb_cert", "apply"),
                                 (APPROVE, "APPROVE API-FOOTBALL CERT PLAN IN fcb_cert", "approve")):
        text = path.read_text(encoding="utf-8")
        doc, job = _job(path)
        assert list(doc[True].keys()) == ["workflow_dispatch"]  # yaml 'on' -> True: manual only
        inputs = doc[True]["workflow_dispatch"]["inputs"]
        assert inputs["mode"]["default"] == "diagnose" and inputs["mode"]["options"] == ["diagnose", writes]
        assert job["environment"] == "aiven-production" and job["if"] == "github.ref == 'refs/heads/main'"
        assert job["env"]["CONFIRM_PHRASE"] == phrase and job["env"]["REQUIRE_SSLMODE_VERIFY_FULL"] == "true"
        assert doc["permissions"] == {"contents": "read", "actions": "read"}
        assert "require_owner_main_dispatch.sh" in text and "require_protected_environment.sh" in text
        pins = _pins(text)
        for rel, sha in pins.items():
            assert hashlib.sha256((ROOT / rel).read_bytes()).hexdigest() == sha, rel
        assert f'test "$n" -eq {len(pins)}' in text
        writers = [s for s in job["steps"] if "apifootball_cert_path_a.py " + ("install" if writes == "apply" else "approve") in s.get("run", "")]
        assert len(writers) == 1 and writers[0]["if"] == f"${{{{ inputs.mode == '{writes}' && inputs.confirm == '{phrase}' }}}}"
        assert writers[0]["env"]["PGSSLMODE"] == "verify-full"
        code = "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))
        assert not re.search(r"\bDROP\b", code)
        assert "FCB_MIGRATOR_DB_URL" not in text  # no WP-009 identity: the existing administrator, as approved


def test_a2_install_runs_twice_and_requires_a_no_op_second_run():
    run = next(s["run"] for s in _job(INSTALL)[1]["steps"] if "apifootball_cert_path_a.py install" in s.get("run", ""))
    assert run.count("apifootball_cert_path_a.py install FCB_DEFAULTDB_URL FCB_CERT_RUNNER_DB_PASSWORD") == 2
    assert "database=ALREADY_PRESENT" in run and 'test "$first" = "$second"' in run
    assert "bridge_versions=1,2,3,4,5,6,7,8" in run


def test_a2_approval_binds_the_committed_plan_and_takes_the_budget_from_the_file():
    doc, job = _job(APPROVE)
    assert "budget" not in doc[True]["workflow_dispatch"]["inputs"]  # never an input
    text = APPROVE.read_text(encoding="utf-8")
    assert "--check-plan" in text and "--plan-approval-request" in text
    assert 'test "$sha" = "$PLAN_SHA256_INPUT"' in text and "plan file hash differs" in text
    sql = (ROOT / "sql/aiven/apifootball_cert_path_a_plan_approve.sql").read_text(encoding="utf-8")
    assert "ON CONFLICT (plan_sha256) DO NOTHING" in sql and "FCB_CERT_PLAN_CONFLICT" in sql
