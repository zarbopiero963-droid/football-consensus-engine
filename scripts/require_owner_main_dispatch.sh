#!/usr/bin/env bash
# G-02: a job that receives a privileged secret runs only when ALL of these hold:
#   - it was started manually (workflow_dispatch), not by push, pull_request or schedule;
#   - from the reviewed workflow on main;
#   - by the repository owner, who also started any re-run (triggering_actor).
# Inputs come from the environment so nothing user-controlled is interpolated into the shell.
# Fail closed on any missing or mismatching value.
set -euo pipefail
event="${GH_EVENT_NAME:-}"; ref="${GIT_REF:-}"; actor="${ACTOR:-}"
trig="${TRIGGERING_ACTOR:-}"; owner="${REPO_OWNER:-}"
if [ "$event" != "workflow_dispatch" ]; then
  echo "::error::privileged job refused: event is not workflow_dispatch" >&2; exit 1
fi
if [ "$ref" != "refs/heads/main" ]; then
  echo "::error::privileged job refused: runs only from main" >&2; exit 1
fi
if [ -z "$owner" ] || [ "$actor" != "$owner" ] || [ "$trig" != "$owner" ]; then
  echo "::error::privileged job refused: only the repository owner may start or re-run it" >&2; exit 1
fi
echo "trusted dispatch: main, owner"
