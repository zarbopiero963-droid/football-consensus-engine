#!/usr/bin/env bash
# FCB-WP-005 / G-02: refuse unless the GitHub Environment is protected:
#   1. at least one required reviewer, and
#   2. a deployment-branch policy: either "protected branches only", or custom policies that
#      allow exactly the branch `main` (anything else, or no policy at all, is refused).
# A required reviewer gates every secret release; the branch policy stops a workflow file
# edited on another branch from reaching the environment's secrets at all.
# Fail closed on ANY error: API failure, non-JSON or error body, missing field, non-number.
# Usage: require_protected_environment.sh <owner/repo> <environment>
# Prints the number of required reviewers on success.
set -euo pipefail
repo="${1:?repo}"; env_name="${2:?environment}"
if ! body="$(gh api "repos/$repo/environments/$env_name" 2>/dev/null)"; then
  echo "::error::cannot read environment $env_name protection (API error); refusing" >&2; exit 1
fi
if ! reviewers="$(printf '%s' "$body" | jq -er '[.protection_rules[]? | select(.type=="required_reviewers") | .reviewers[]?] | length' 2>/dev/null)"; then
  echo "::error::environment $env_name protection unreadable; refusing" >&2; exit 1
fi
if ! [[ "$reviewers" =~ ^[0-9]+$ ]] || [ "$reviewers" -lt 1 ]; then
  echo "::error::environment $env_name has no required reviewer; refusing" >&2; exit 1
fi
# Deployment-branch policy: null means every branch may deploy to the environment.
if ! policy="$(printf '%s' "$body" | jq -ec '.deployment_branch_policy | select(type=="object")' 2>/dev/null)"; then
  echo "::error::environment $env_name has no deployment-branch policy (any branch could use it); refusing" >&2; exit 1
fi
protected="$(printf '%s' "$policy" | jq -r '.protected_branches == true')"
custom="$(printf '%s' "$policy" | jq -r '.custom_branch_policies == true')"
if [ "$protected" = "true" ] && [ "$custom" != "true" ]; then
  :
elif [ "$custom" = "true" ] && [ "$protected" != "true" ]; then
  if ! names="$(gh api "repos/$repo/environments/$env_name/deployment-branch-policies" 2>/dev/null \
      | jq -er '[.branch_policies[]? | ((.type // "branch") + ":" + .name)] | if length == 0 then error("empty") else join(",") end' 2>/dev/null)"; then
    echo "::error::environment $env_name branch policies unreadable or empty; refusing" >&2; exit 1
  fi
  if [ "$names" != "branch:main" ]; then
    echo "::error::environment $env_name must allow only the branch main; refusing" >&2; exit 1
  fi
else
  echo "::error::environment $env_name deployment-branch policy is not recognised; refusing" >&2; exit 1
fi
printf '%s\n' "$reviewers"
