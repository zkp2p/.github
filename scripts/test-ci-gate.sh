#!/usr/bin/env bash
set -euo pipefail

if [[ ! -f .github/workflows/ssm-deploy.yml ]]; then
  echo 'FAIL: reusable SSM deploy workflow is missing' >&2
  exit 1
fi

decision_jq=$(docker run --rm -v "$PWD:/w" -w /w mikefarah/yq:4.44.3 \
  '.jobs.deploy.steps[] | select(.id == "decide") | .env.DECISION_JQ' \
  .github/workflows/ssm-deploy.yml)
if [[ -z "$decision_jq" || "$decision_jq" == null ]]; then
  echo 'FAIL: decide.env.DECISION_JQ is missing' >&2
  exit 1
fi
runner_loss_jq=$(docker run --rm -v "$PWD:/w" -w /w mikefarah/yq:4.44.3 \
  '.jobs.deploy.steps[] | select(.id == "decide") | .env.RUNNER_LOSS_JQ' \
  .github/workflows/ssm-deploy.yml)
if [[ -z "$runner_loss_jq" || "$runner_loss_jq" == null ]]; then
  echo 'FAIL: decide.env.RUNNER_LOSS_JQ is missing' >&2
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

check_runner_loss() {
  local fixture=$1 expected=$2 jobs=$3 actual
  actual=$(jq -r "$runner_loss_jq" <<< "$jobs")
  if [[ "$actual" != "$expected" ]]; then
    printf 'FAIL: %s expected %s, got %s\n' "$fixture" "$expected" "$actual" >&2
    exit 1
  fi
  passed=$((passed + 1))
}

check_runner_loss 'lost runner' true '[{"jobs":[{"conclusion":"failure","steps":[{"conclusion":"cancelled"}]}]}]'
check_runner_loss cancelled true '[{"jobs":[{"conclusion":"cancelled","steps":[]}]}]'
check_runner_loss 'failed step on another page' false '[{"jobs":[{"conclusion":"failure","steps":[]}]},{"jobs":[{"conclusion":"failure","steps":[{"conclusion":"failure"}]}]}]'
check_runner_loss 'no failed jobs' false '[{"jobs":[{"conclusion":"success","steps":[]}]}]'
check_runner_loss 'missing steps' false '[{"jobs":[{"conclusion":"failure"}]}]'
check_runner_loss 'empty jobs' false '[{"jobs":[]}]'

printf '%s passed\n' "$passed"
