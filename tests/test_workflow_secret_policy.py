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
WORKFLOWS = sorted([*(ROOT / ".github" / "workflows").glob("*.yml"),
                    *(ROOT / ".github" / "workflows").glob("*.yaml")])  # GitHub runs both
EXPR_RE = re.compile(r"\$\{\{(.*?)\}\}", re.S)
NAME_DOT_RE = re.compile(r"\bsecrets\.([A-Za-z0-9_]+)", re.I)
NAME_IDX_RE = re.compile(r"""\bsecrets\[\s*['"]([A-Za-z0-9_]+)['"]\s*\]""", re.I)
WHOLE = "<ALL_SECRETS>"      # toJSON(secrets), secrets[expr], secrets: inherit, ...
# Default-closed classification (review of #176): every secret is treated as a privileged
# database credential unless it is on this explicit list of non-database secrets.
NON_DB_SECRETS = {"APIFOOTBALL_API_KEY", "SXBET_API_KEY", "FCB_AIVEN_CA_PEM", "FCB_AIVEN_CA_SHA256"}
ALLOWED_SECRET_TRIGGERS = {"workflow_dispatch"}
GATE_ENV = "scripts/require_protected_environment.sh"
GATE_DISPATCH = "scripts/require_owner_main_dispatch.sh"
STATUS_FN_RE = re.compile(r"\b(always|failure|cancelled|success)\s*\(", re.I)


def _load(path):
    data = yaml.safe_load(path.read_text(encoding="utf-8"))
    triggers = data.get("on", data.get(True))
    if isinstance(triggers, str):
        triggers = {triggers: None}
    elif isinstance(triggers, list):
        triggers = {t: None for t in triggers}
    return data, set(triggers or {})


def _inherits(obj) -> bool:
    if isinstance(obj, dict):
        return any(k == "secrets" or _inherits(v) for k, v in obj.items())
    if isinstance(obj, list):
        return any(_inherits(v) for v in obj)
    return False


def _secrets(obj) -> set[str]:
    """Every secret an object can reach, from any expression form; WHOLE when unbounded."""
    found: set[str] = set()
    for expr in EXPR_RE.findall(yaml.safe_dump(obj, width=10**9)):
        # Expression contexts are case-insensitive in GitHub Actions (SECRETS.X, toJSON(Secrets)).
        if not re.search(r"\bsecrets\b", expr, re.I):
            continue
        names = {n.upper() for n in (*NAME_DOT_RE.findall(expr), *NAME_IDX_RE.findall(expr))}
        bare = re.sub(r"""\bsecrets(\.[A-Za-z0-9_]+|\[\s*['"][A-Za-z0-9_]+['"]\s*\])""", "", expr, flags=re.I)
        if re.search(r"\bsecrets\b", bare, re.I):
            names.add(WHOLE)
        found |= names
    if _inherits(obj):
        found.add(WHOLE)
    return found - {"GITHUB_TOKEN"}


def _privileged(names) -> bool:
    return any(n not in NON_DB_SECRETS for n in names)


def _secret_jobs():
    for path in WORKFLOWS:
        data, triggers = _load(path)
        for job_id, job in (data.get("jobs") or {}).items():
            names = _secrets(job)
            if names:
                yield path, triggers, job_id, job, names


def test_secret_detection_sees_every_expression_form():
    job = {"steps": [
        {"run": "x", "env": {"A": "${{ secrets.FCB_PG18_TEST_URL }}"}},
        {"run": "x", "env": {"B": "${{ secrets['OTHER_URL'] }}"}},
        {"run": "x", "env": {"C": "${{ toJSON(secrets) }}"}},
        {"run": "x", "env": {"D": "${{ secrets[format('{0}', 'X')] }}"}},
        {"run": "x", "env": {"E": "${{ secrets.GITHUB_TOKEN }}"}},
    ]}
    assert _secrets(job) == {"FCB_PG18_TEST_URL", "OTHER_URL", WHOLE}
    assert _secrets({"uses": "./.github/workflows/x.yml", "secrets": "inherit"}) == {WHOLE}
    assert _secrets({"env": {"A": "${{ SECRETS.fcb_pg18_test_url }}", "B": "${{ toJSON(Secrets) }}"}}) \
        == {"FCB_PG18_TEST_URL", WHOLE}
    assert _privileged({"POSTGRES_URL"}) and _privileged({WHOLE}) and not _privileged({"SXBET_API_KEY"})


def test_no_workflow_hands_out_secrets_wholesale():
    for path in WORKFLOWS:
        data, _ = _load(path)
        assert WHOLE not in _secrets(data), f"{path.name}: toJSON(secrets) / secrets[expr] / secrets: inherit"


def test_the_policy_sees_the_known_privileged_workflows():
    found = {(p.name, j) for p, _, j, _, n in _secret_jobs() if _privileged(n)}
    assert ("fcb-aiven-pg18-test.yml", "pg18-test") in found
    assert ("fcb-aiven-defaultdb-restore.yml", "restore") in found
    assert ("fcb-aiven-defaultdb-roles.yml", "roles") in found  # WP-009: admin URI + role passwords
    assert ("api-football-live-smoke.yml", "live") in found
    assert ("fcb-mcp-e2e.yml", "e2e") in found  # MCP_OAUTH_PASSWORD is privileged (default-closed)
    # OAuth persistent throttle: 0009 apply and emergency unlock (fcb_migrator URI).
    assert ("fcb-aiven-defaultdb-oauth-throttle-0009.yml", "throttle0009") in found
    assert ("fcb-aiven-defaultdb-oauth-throttle-unlock.yml", "unlock") in found


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
    return [t for t in _secret_jobs() if _privileged(t[4])]


def _gate_lines(step) -> list[str]:
    return [line.strip() for line in str(step.get("run", "")).splitlines() if line.strip()]


GATE_LINE_RE = re.compile(
    r'^(bash scripts/require_owner_main_dispatch\.sh'
    r'|reviewers="\$\(bash scripts/require_protected_environment\.sh "\$REPO" [A-Za-z0-9_-]+\)")$')


# The gate step's inputs must come from GitHub's own context, never from literals or from
# values an earlier step could inject (review of #176: hardcoded actor/repo, BASH_ENV, PATH).
GATE_ENV_REQUIRED = {
    "GH_TOKEN": "${{ github.token }}",
    "REPO": "${{ github.repository }}",
    "GH_EVENT_NAME": "${{ github.event_name }}",
    "GIT_REF": "${{ github.ref }}",
    "ACTOR": "${{ github.actor }}",
    "TRIGGERING_ACTOR": "${{ github.triggering_actor }}",
    "REPO_OWNER": "${{ github.repository_owner }}",
}
# The restore workflow's combined mode gate also reads its own dispatch inputs.
GATE_ENV_OPTIONAL = {"MODE": "${{ inputs.mode }}", "CONFIRM": "${{ inputs.confirm }}"}


def _gate_env_ok(step) -> bool:
    env = step.get("env") or {}
    for key, value in env.items():
        expected = GATE_ENV_REQUIRED.get(key, GATE_ENV_OPTIONAL.get(key))
        if expected is None or str(value).strip() != expected:
            return False
    return True


def _writes_runner_env(step) -> bool:
    run = str(step.get("run", ""))
    return "GITHUB_ENV" in run or "GITHUB_PATH" in run


def _is_strict_gate(step, needle: str) -> bool:
    """A gate counts only if it cannot be skipped or neutralised: no `if:`, no
    continue-on-error, no `shell:`/`working-directory:` override, `set -euo pipefail` first,
    and the gate command inside the LEADING block of gate commands (comments aside): nothing
    else can run before it, so `exit 0`, `|| true` or any other command first disqualifies it."""
    if any(k in step for k in ("if", "continue-on-error", "shell", "working-directory")):
        return False
    if not _gate_env_ok(step):
        return False
    lines = [line for line in _gate_lines(step) if not line.startswith("#")]
    if len(lines) < 2 or lines[0] != "set -euo pipefail":
        return False
    leading = []
    for line in lines[1:]:
        if not GATE_LINE_RE.fullmatch(line):
            break
        leading.append(line)
    return needle in leading


@pytest.mark.parametrize("item", _db_jobs(), ids=lambda t: f"{t[0].name}:{t[2]}")
def test_database_secrets_only_after_both_gates_and_only_at_step_level(item):
    path, _, job_id, job, _ = item
    env = job["environment"]
    env_name = env.get("name") if isinstance(env, dict) else env
    job_level = {k: v for k, v in job.items() if k != "steps"}
    assert not _privileged(_secrets(job_level)), f"{path.name}:{job_id}: privileged secret at job level"
    env_line = f'reviewers="$(bash {GATE_ENV} "$REPO" {env_name})"'
    dispatch_line = f"bash {GATE_DISPATCH}"
    gates_done = set()
    for step in job["steps"]:
        if gates_done != {"dispatch", "environment"}:
            # Nothing before the gates may rewrite the runner environment (PATH, BASH_ENV, ...).
            assert not _writes_runner_env(step), \
                f"{path.name}:{job_id}: step {step.get('name')!r} writes GITHUB_ENV/GITHUB_PATH before the gates"
        if _is_strict_gate(step, dispatch_line):
            gates_done.add("dispatch")
        if _is_strict_gate(step, env_line):
            gates_done.add("environment")
        names = _secrets(step)
        if _privileged(names):
            assert gates_done == {"dispatch", "environment"}, \
                f"{path.name}:{job_id}: step {step.get('name')!r} gets a privileged secret before the gates"
            # GitHub adds an implicit success() only when an `if:` names NO status function; any
            # status function (always(), failure(), cancelled(), even `!success() || true`) can
            # re-enable the step after a failed gate, so none is allowed here.
            cond = str(step.get("if", ""))
            assert not STATUS_FN_RE.search(cond), \
                f"{path.name}:{job_id}: step {step.get('name')!r} could run after a failed gate"
            assert not step.get("continue-on-error"), f"{path.name}:{job_id}: {step.get('name')!r}"
    assert gates_done == {"dispatch", "environment"}, f"{path.name}:{job_id}: gates missing"


def test_a_neutralised_gate_is_not_a_gate():
    good = {"run": "set -euo pipefail\nbash scripts/require_owner_main_dispatch.sh\n"}
    assert _is_strict_gate(good, "bash scripts/require_owner_main_dispatch.sh")
    for bad in (
        {**good, "continue-on-error": True},
        {**good, "if": "always()"},
        {"run": "set -euo pipefail\nbash scripts/require_owner_main_dispatch.sh || true\n"},
        {"run": "bash scripts/require_owner_main_dispatch.sh\n"},
        {**good, "shell": 'bash -c "true {0}"'},
        {**good, "working-directory": "/tmp"},
        {"run": "set -euo pipefail\nexit 0\nbash scripts/require_owner_main_dispatch.sh\n"},
        {**good, "env": {"ACTOR": "x", "TRIGGERING_ACTOR": "x", "REPO_OWNER": "x"}},
        {**good, "env": {"REPO": "attacker/decoy"}},
        {**good, "env": {"BASH_ENV": "/tmp/evil"}},
    ):
        assert not _is_strict_gate(bad, "bash scripts/require_owner_main_dispatch.sh"), bad


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


def test_environment_gate_accepts_selected_branch_main_only(tmp_path):
    body = "{" + REVIEWER + ',"deployment_branch_policy":{"protected_branches":false,"custom_branch_policies":true}}'
    out = _env_gate(tmp_path, body, '{"total_count":1,"branch_policies":[{"name":"main","type":"branch"}]}')
    assert out.returncode == 0 and out.stdout.strip() == "1"


@pytest.mark.parametrize("policy,policies", [
    ("null", "{}"),                                                       # any branch may deploy
    ('{"protected_branches":true,"custom_branch_policies":false}', "{}"),  # wider than main
    ('{"protected_branches":false,"custom_branch_policies":true}',
     '{"total_count":2,"branch_policies":[{"name":"main","type":"branch"}]}'),  # more pages exist
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
