#!/usr/bin/env bash
# FCB-WP-005: refuse unless the GitHub Environment has at least one required reviewer.
# Fail closed on ANY error: API failure, non-JSON or error body, missing field, non-number.
# Usage: require_protected_environment.sh <owner/repo> <environment>
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
printf '%s\n' "$reviewers"
