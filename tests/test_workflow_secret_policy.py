"""G-02: no GitHub Actions secret may reach a job that is not protected.

Static policy over every workflow in .github/workflows (no network):
- a workflow that references any secret other than the built-in GITHUB_TOKEN may only be
  triggered by workflow_dispatch (never push, pull_request, pull_request_target, schedule, ...);
- secrets are never referenced at workflow level, only inside jobs;
- every job that references a secret runs in a named GitHub Environment and only from main;
- every job that references a DATABASE credential (privileged: it reaches Aiven, where the
  only role today is the administrative one) runs the trusted-dispatch gate and the
  protected-environment gate for its own environment BEFORE any step that receives the
  secret, and receives it only at step level.

Plus behaviour tests of the two gate scripts, including their fail-closed paths.
"""
from __future__ import annotations

import os
import re
import subprocess
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = sorted((ROOT / ".github" / "workflows").glob("*.yml"))
SECRET_RE = re.compile(r"secrets\.([A-Za-z0-9_]+)")
DB_SECRET_RE = re.compile(r"(DATABASE|_DB_|DB_URL|DEFAULTDB|PG18|DSN)")
ALLOWED_SECRET_TRIGGERS = {"workflow_dispatch"}
GATE_ENV = "scripts/require_protected_environment.sh"
GATE_DISPATCH = "scripts/require_owner_main_dispatch.sh"


def _load(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = data.get("on", data.get(True))
    if isinstance(triggers, str):
        triggers = {triggers: None}
    elif isinstance(triggers, list):
        triggers = {t: None for t in triggers}
    return data, set(triggers or {})


def _secrets(obj) -> set[str]:
    return {s for s in SECRET_RE.findall(yaml.safe_dump(obj)) if s != "GITHUB_TOKEN"}


def _secret_jobs():
    for path in WORKFLOWS:
        data, triggers = _load(path)
        for job_id, job in (data.get("jobs") or {}).items():
            names = _secrets(job)
            if names:
                yield path, triggers, job_id, job, names


def test_the_policy_sees_the_known_privileged_workflows():
    found = {(p.name, j) for p, _, j, _, n in _secret_jobs() if any(DB_SECRET_RE.search(x) for x in n)}
    assert ("fcb-aiven-pg18-test.yml", "pg18-test") in found
    assert ("fcb-aiven-defaultdb-restore.yml", "restore") in found
    assert ("api-football-live-smoke.yml", "live") in found


@pytest.mark.parametrize("path", WORKFLOWS, ids=lambda p: p.name)
def test_no_secret_at_workflow_level_and_secret_workflows_are_manual_only(path):
    data, triggers = _load(path)
    top = {k: v for k, v in data.items() if k != "jobs"}
    assert not _secrets(top), f"{path.name}: secrets referenced at workflow level"
    if any(_secrets(job) for job in (data.get("jobs") or {}).values()):
        assert triggers <= ALLOWED_SECRET_TRIGGERS, f"{path.name}: secret workflow triggered by {sorted(triggers)}"


@pytest.mark.parametrize("item", list(_secret_jobs()), ids=lambda t: f"{t[0].name}:{t[2]}")
def test_every_secret_job_runs_in_an_environment_from_main_only(item):
    path, _, job_id, job, _ = item
    env = job.get("environment")
    env_name = env.get("name") if isinstance(env, dict) else env
    assert isinstance(env_name, str) and env_name and "${{" not in env_name, f"{path.name}:{job_id}"
    assert job.get("if", "").replace('"', "'").strip() == "github.ref == 'refs/heads/main'", f"{path.name}:{job_id}"


def _db_jobs():
    return [t for t in _secret_jobs() if any(DB_SECRET_RE.search(n) for n in t[4])]


@pytest.mark.parametrize("item", _db_jobs(), ids=lambda t: f"{t[0].name}:{t[2]}")
def test_database_secrets_only_after_both_gates_and_only_at_step_level(item):
    path, _, job_id, job, _ = item
    env = job["environment"]
    env_name = env.get("name") if isinstance(env, dict) else env
    job_level = {k: v for k, v in job.items() if k != "steps"}
    assert not {n for n in _secrets(job_level) if DB_SECRET_RE.search(n)}, \
        f"{path.name}:{job_id}: database secret exposed at job level"
    gates_done = set()
    for step in job["steps"]:
        run = step.get("run", "")
        if GATE_DISPATCH in run or ("refs/heads/main" in run and "REPO_OWNER" in run):
            gates_done.add("dispatch")
        if GATE_ENV in run and env_name in run:
            gates_done.add("environment")
        if any(DB_SECRET_RE.search(n) for n in _secrets(step)):
            assert gates_done == {"dispatch", "environment"}, \
                f"{path.name}:{job_id}: step {step.get('name')!r} gets a database secret before the gates"


def test_pull_request_workflows_never_reference_secrets():
    for path in WORKFLOWS:
        data, triggers = _load(path)
        if triggers & {"pull_request", "pull_request_target", "push"}:
            assert not _secrets(data), path.name


# ---- gate scripts -----------------------------------------------------------------------

def _run_dispatch(**env):
    base = {"PATH": os.environ["PATH"]}
    return subprocess.run(["bash", str(ROOT / GATE_DISPATCH)], env={**base, **env}, capture_output=True, text=True)


GOOD = dict(GH_EVENT_NAME="workflow_dispatch", GIT_REF="refs/heads/main", ACTOR="own",
            TRIGGERING_ACTOR="own", REPO_OWNER="own")


def test_dispatch_gate_accepts_owner_on_main():
    assert _run_dispatch(**GOOD).returncode == 0


@pytest.mark.parametrize("change", [
    {"GH_EVENT_NAME": "pull_request"}, {"GH_EVENT_NAME": "push"}, {"GH_EVENT_NAME": ""},
    {"GIT_REF": "refs/heads/feature"}, {"GIT_REF": "refs/pull/1/merge"}, {"GIT_REF": ""},
    {"ACTOR": "someone"}, {"TRIGGERING_ACTOR": "someone"}, {"REPO_OWNER": ""},
    {"ACTOR": "", "TRIGGERING_ACTOR": "", "REPO_OWNER": ""},
])
def test_dispatch_gate_fails_closed(change):
    out = _run_dispatch(**{**GOOD, **change})
    assert out.returncode != 0 and out.stdout == ""


def _env_gate(tmp_path, env_body, policies_body='{"branch_policies":[]}', rc=0):
    stub = tmp_path / "bin"
    stub.mkdir(exist_ok=True)
    (tmp_path / "env.json").write_text(env_body)
    (tmp_path / "pol.json").write_text(policies_body)
    (stub / "gh").write_text(
        "#!/usr/bin/env bash\n"
        f"case \"$2\" in *deployment-branch-policies) cat '{tmp_path}/pol.json';; *) cat '{tmp_path}/env.json';; esac\n"
        f"exit {rc}\n")
    (stub / "gh").chmod(0o755)
    env = {**os.environ, "PATH": f"{stub}:{os.environ['PATH']}"}
    return subprocess.run(["bash", str(ROOT / GATE_ENV), "o/r", "envx"], env=env, capture_output=True, text=True)


REVIEWER = '"protection_rules":[{"type":"required_reviewers","reviewers":[{"type":"User"}]}]'


def test_environment_gate_accepts_reviewer_and_protected_branches(tmp_path):
    body = "{" + REVIEWER + ',"deployment_branch_policy":{"protected_branches":true,"custom_branch_policies":false}}'
    out = _env_gate(tmp_path, body)
    assert out.returncode == 0 and out.stdout.strip() == "1"


def test_environment_gate_accepts_custom_policy_main_only(tmp_path):
    body = "{" + REVIEWER + ',"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
    out = _env_gate(tmp_path, body, '{"branch_policies":[{"name":"main","type":"branch"}]}')
    assert out.returncode == 0


@pytest.mark.parametrize("policy,policies", [
    ("null", "{}"),                                                       # any branch may deploy
    ('{"protected_branches":false,"custom_branch_policies":false}', "{}"),
    ('{"protected_branches":true,"custom_branch_policies":true}', "{}"),
    ('{"protected_branches":false,"custom_branch_policies":true}', '{"branch_policies":[]}'),
    ('{"protected_branches":false,"custom_branch_policies":true}', '{"branch_policies":[{"name":"*","type":"branch"}]}'),
    ('{"protected_branches":false,"custom_branch_policies":true}',
     '{"branch_policies":[{"name":"main","type":"branch"},{"name":"dev","type":"branch"}]}'),
    ('{"protected_branches":false,"custom_branch_policies":true}', '{"branch_policies":[{"name":"main","type":"tag"}]}'),
    ('{"protected_branches":false,"custom_branch_policies":true}', "not json"),
])
def test_environment_gate_refuses_a_missing_or_wide_branch_policy(tmp_path, policy, policies):
    body = "{" + REVIEWER + ',"deployment_branch_policy":' + policy + "}"
    out = _env_gate(tmp_path, body, policies)
    assert out.returncode != 0 and out.stdout == ""
