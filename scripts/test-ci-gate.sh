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
tree_reuse_jq=$(docker run --rm -v "$PWD:/w" -w /w mikefarah/yq:4.44.3 \
  '.jobs.deploy.steps[] | select(.id == "decide") | .env.TREE_REUSE_JQ' \
  .github/workflows/ssm-deploy.yml)
if [[ -z "$tree_reuse_jq" || "$tree_reuse_jq" == null ]]; then
  echo 'FAIL: decide.env.TREE_REUSE_JQ is missing' >&2
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
support_runner_loss=$(jq -nc '
  [{jobs: ([range(1; 4) | {name: "support-api tests (\(.)/4)", conclusion: "failure",
      steps: [{conclusion: "success"}, {conclusion: "cancelled"}]}]
    + [{name: "support-api tests (4/4)", conclusion: "success", steps: [{conclusion: "success"}]}])},
   {jobs: [{name: "support / support-api", conclusion: "failure",
     steps: [{name: "Require support-api build and every test shard", conclusion: "failure"}]}]}]
')
check_runner_loss 'three lost support shards + failed aggregator' true "$support_runner_loss"
check_runner_loss 'failed aggregator before lost support shards' true "$(jq 'reverse' <<< "$support_runner_loss")"
check_runner_loss cancelled true '[{"jobs":[{"conclusion":"cancelled","steps":[{"conclusion":"cancelled"}]}]}]'
check_runner_loss 'cancelled runner + failed step' true '[{"jobs":[{"conclusion":"cancelled","steps":[{"conclusion":"cancelled"}]},{"conclusion":"failure","steps":[{"conclusion":"failure"}]}]}]'
check_runner_loss 'failed step on another page' false '[{"jobs":[{"conclusion":"failure","steps":[]}]},{"jobs":[{"conclusion":"failure","steps":[{"conclusion":"failure"}]}]}]'
check_runner_loss 'no failed jobs' false '[{"jobs":[{"conclusion":"success","steps":[{"conclusion":"success"}]}]}]'
check_runner_loss 'empty steps' false '[{"jobs":[{"conclusion":"failure","steps":[]}]}]'
check_runner_loss 'cancelled with empty steps' false '[{"jobs":[{"conclusion":"cancelled","steps":[]}]}]'
check_runner_loss 'missing steps' false '[{"jobs":[{"conclusion":"failure"}]}]'
check_runner_loss 'null steps' false '[{"jobs":[{"conclusion":"failure","steps":null}]}]'
check_runner_loss 'invalid steps' false '[{"jobs":[{"conclusion":"failure","steps":{}}]}]'
check_runner_loss 'empty jobs' false '[{"jobs":[]}]'

check_tree() {
  local fixture=$1 expected=$2 runs=$3 actual
  actual=$(jq -r --arg tree "$tree" "$tree_reuse_jq" <<< "$runs")
  if [[ "$actual" != "$expected" ]]; then
    printf 'FAIL: %s expected %s, got %s\n' "$fixture" "$expected" "$actual" >&2
    exit 1
  fi
  passed=$((passed + 1))
}

tree=eeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeeee
head=bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb
success=$(jq -nc --arg tree "$tree" --arg head "$head" '
  {workflow_runs: [{status: "completed", conclusion: "success", head_commit: {tree_id: $tree},
    html_url: "https://example.invalid/actions/runs/99", head_sha: $head}]}
')
check_tree match "$(printf 'https://example.invalid/actions/runs/99\t%s' "$head")" "$success"
check_tree mismatch '' "$(jq '.workflow_runs[0].head_commit.tree_id = "ffffffffffffffffffffffffffffffffffffffff"' <<< "$success")"
check_tree 'failed run' '' "$(jq '.workflow_runs[0].conclusion = "failure"' <<< "$success")"
check_tree 'unfinished run' '' "$(jq '.workflow_runs[0].status = "in_progress"' <<< "$success")"
check_tree 'missing tree' '' "$(jq '.workflow_runs[0].head_commit = null' <<< "$success")"
check_tree 'missing evidence URL' '' "$(jq 'del(.workflow_runs[0].html_url)' <<< "$success")"
check_tree 'no successful runs' '' '{"workflow_runs":[]}'

printf '%s passed\n' "$passed"
