#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f .github/workflows/ssm-deploy.yml ]]; then
  echo 'FAIL: reusable SSM deploy workflow is missing' >&2
  exit 1
fi

decision_jq=$(docker run --rm -v "$PWD:/w" -w /w mikefarah/yq:4.44.3 \
  '.jobs.ci-gate.steps[] | select(.id == "decide") | .env.DECISION_JQ' \
  .github/workflows/ssm-deploy.yml)
if [[ -z "$decision_jq" || "$decision_jq" == null ]]; then
  echo 'FAIL: decide.env.DECISION_JQ is missing' >&2
  exit 1
fi

passed=0
check() {
  local fixture=$1 expected=$2 runs=$3 actual
  actual=$(jq -er "$decision_jq" <<< "$runs")
  if [[ "$actual" != "$expected" ]]; then
    printf 'FAIL: %s expected %s, got %s\n' "$fixture" "$expected" "$actual" >&2
    exit 1
  fi
  passed=$((passed + 1))
}

check success pass '{"workflow_runs":[{"status":"completed","conclusion":"success"}]}'
check 'failure + in_progress' wait '{"workflow_runs":[{"status":"completed","conclusion":"failure"},{"status":"in_progress","conclusion":null}]}'
check 'failure only' fail '{"workflow_runs":[{"status":"completed","conclusion":"failure"}]}'
check empty dispatch '{"workflow_runs":[]}'

printf '%s passed\n' "$passed"
