#!/usr/bin/env python3
"""Exercise workflow shell steps with isolated tools; never contact AWS or GitHub."""

import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = json.loads(subprocess.check_output([
    "docker", "run", "--rm", "-v", f"{ROOT}:/w:ro", "-w", "/w",
    "mikefarah/yq:4.44.3", "-o=json", ".github/workflows/ssm-deploy.yml",
], text=True))


def step(job, name):
    return next(item for item in WORKFLOW["jobs"][job]["steps"] if item["name"] == name)


class DeployTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # Only explicitly allowed tools are reachable from the workflow scripts.
        for name in ("bash", "jq", "openssl"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.root), "STATE": str(self.root),
            "GITHUB_OUTPUT": str(self.root / "outputs"),
            "COMMAND_ID": "test-command", "STARTED_AT": "1000000", "LOG_GROUP": "test",
            "ENVIRONMENTS_FILE": "environments.yml", "REF": "releases/staging",
            "SLACK_WEBHOOK_URL": "https://example.invalid/webhook",
            "REPO": "example/repo", "DEPLOY_ENVIRONMENT": "staging", "MODE": "image",
            "SHA": "a" * 40, "RESULT": "success", "RUN_URL": "https://example.invalid/run",
        }
        self.tool("timeout", 'shift\nexec "$@"')
        self.tool("date", 'printf "%s\\n" "${NOW:-1000}"')
        self.tool("sleep", 'echo "unexpected sleep" >&2; exit 97')
        self.tool("docker", 'echo "unexpected Docker fallback" >&2; exit 98')

    def tool(self, name, body):
        path = self.bin / name
        path.write_text("#!/bin/bash\nset -euo pipefail\n" + body + "\n")
        path.chmod(0o755)

    def run_step(self, job, name, **env):
        return subprocess.run(
            [str(self.bin / "bash"), "--noprofile", "--norc", "-e", "-o", "pipefail", "-c",
             step(job, name)["run"]], cwd=self.root, env={**self.env, **env},
            text=True, capture_output=True, timeout=5,
        )

    def assert_exit(self, result, code):
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)

    def config_tool(self, name):
        config = {"region": "test-region", "ssmDocument": "test-document", "logGroup": "test-logs",
                  "environments": {"staging": {"branch": "releases/staging", "instanceId": "i-test",
                                               "roleArn": "test-role", "ci": "skip"}}}
        body = 'printf "%s\\n" \'' + json.dumps(config) + "'"
        if name == "yq":
            body = 'if [[ "$1" == --version ]]; then echo "$YQ_VERSION"; exit 0; fi\n' + body
        else:
            body = '[[ "$*" == *mikefarah/yq:4.44.3* ]]\n' + body
        self.tool(name, body)

    def test_native_yq(self):
        self.config_tool("yq")
        for version in ("yq (https://github.com/mikefarah/yq/) version v4.44.3", "yq version v4.44.3"):
            with self.subTest(version=version):
                result = self.run_step("resolve", "Resolve environment", YQ_VERSION=version)
                self.assert_exit(result, 0)
                self.assertIn("environment=staging", (self.root / "outputs").read_text())

    def test_missing_yq_uses_pinned_docker(self):
        self.config_tool("docker")
        self.assert_exit(self.run_step("resolve", "Resolve environment"), 0)

    def test_incompatible_yq_fails_without_docker(self):
        self.config_tool("yq")
        self.config_tool("docker")
        self.assert_exit(self.run_step("resolve", "Resolve environment", YQ_VERSION="yq 3.4.3"), 1)

    def test_native_yq_read_failure_does_not_fall_back(self):
        self.tool("yq", 'if [[ "$1" == --version ]]; then echo "yq version v4.44.3"; else exit 1; fi')
        self.config_tool("docker")
        result = self.run_step("resolve", "Resolve environment")
        self.assert_exit(result, 1)
        self.assertIn("Could not read environments-file", result.stderr)

    def test_slack_failure_is_warning_only(self):
        self.tool("curl", 'jq -e \' .text | contains(" success ")\' >/dev/null\nexit 22')
        result = self.run_step("deploy", "Notify Slack if configured")
        self.assert_exit(result, 0)
        self.assertIn("::warning::", result.stdout + result.stderr)

    def test_slack_success(self):
        self.tool("curl", 'jq -e \' .text | contains(" success ")\' >/dev/null')
        self.assert_exit(self.run_step("deploy", "Notify Slack if configured"), 0)

    def test_slack_unconfigured(self):
        self.assert_exit(self.run_step("deploy", "Notify Slack if configured", SLACK_WEBHOOK_URL=""), 0)

    def test_final_gate_uses_poll_and_status_only(self):
        gate = step("deploy", "Require successful deploy")
        self.assertEqual(gate["if"], "always()")
        self.assertEqual(gate["env"]["RESULT"],
                         "${{ steps.poll.outcome == 'success' && steps.status.outcome == 'success' && 'success' || 'failure' }}")
        for poll in ("success", "failure", "skipped", "cancelled"):
            for status in ("success", "failure", "skipped", "cancelled"):
                success = poll == status == "success"
                with self.subTest(poll=poll, status=status):
                    self.assert_exit(self.run_step("deploy", "Require successful deploy",
                                                   RESULT="success" if success else "failure"), 0 if success else 1)

    def pages(self, responses):
        for index, response in enumerate(responses):
            (self.root / f"page-{index}.json").write_text(json.dumps(response))
        self.tool("aws", '''
[[ "$1 $2" == 'logs filter-log-events' ]]
shift 2
token=''
start=''
manual=false
while (( $# )); do
  case "$1" in
    --next-token) token=$2; shift 2 ;;
    --start-time) start=$2; shift 2 ;;
    --no-paginate) manual=true; shift ;;
    --log-group-name|--log-stream-name-prefix|--output) shift 2 ;;
    *) exit 90 ;;
  esac
done
[[ "$manual" == true ]]
printf '%s:%s\\n' "$start" "$token" >> "$STATE/requests"
index=$(jq -r '. // 0' "$STATE/index" 2>/dev/null || echo 0)
printf '%s\\n' "$((index + 1))" > "$STATE/index"
jq . "$STATE/page-$index.json"
''')

    @staticmethod
    def event(event_id, timestamp, message):
        return {"eventId": event_id, "timestamp": timestamp, "message": message,
                "ingestionTime": timestamp, "logStreamName": "test-command/stdout"}

    def test_paginated_logs_drain_before_sleep(self):
        first = self.event("first", 1000001, "first page")
        self.pages([
            {"events": [first], "nextToken": "page-two"},
            {"events": [], "nextToken": "page-three"},
            {"events": [first, self.event("last", 1000001, '{"deployExit": 0}')]},
        ])
        result = self.run_step("deploy", "Follow command logs")
        self.assert_exit(result, 0)
        self.assertEqual(result.stdout.count("first page"), 1)
        self.assertIn('{"deployExit": 0}', result.stdout)
        self.assertEqual((self.root / "requests").read_text().splitlines(),
                         ["1000000:", "1000000:page-two", "1000000:page-three"])

    def test_poll_cursor_stays_inclusive(self):
        first = self.event("first", 1000001, "first poll")
        self.pages([
            {"events": [first]},
            {"events": [first, self.event("last", 1000001, '{"deployExit": 0}')]},
        ])
        self.tool("sleep", 'exit 0')
        result = self.run_step("deploy", "Follow command logs")
        self.assert_exit(result, 0)
        self.assertEqual(result.stdout.count("first poll"), 1)
        self.assertEqual((self.root / "requests").read_text().splitlines(), ["1000000:", "1000001:"])

    def test_nonzero_exit_marker_fails(self):
        self.pages([{"events": [self.event("exit", 1000001, '{"deployExit": 7}')]}])
        result = self.run_step("deploy", "Follow command logs")
        self.assert_exit(result, 1)
        self.assertIn("SSM deploy failed", result.stderr)

    def test_paginated_read_error_fails(self):
        self.pages([{"events": [], "nextToken": "missing-page"}])
        result = self.run_step("deploy", "Follow command logs")
        self.assert_exit(result, 1)
        self.assertIn("Could not read command logs", result.stderr)
        self.assertEqual((self.root / "requests").read_text().splitlines(),
                         ["1000000:", "1000000:missing-page"])

    def test_pagination_respects_overall_deadline(self):
        self.pages([{"events": [], "nextToken": "another-page"}])
        self.tool("date", '''
if [[ -f "$STATE/index" ]]; then echo 4900; else echo 1000; fi
''')
        result = self.run_step("deploy", "Follow command logs")
        self.assert_exit(result, 1)
        self.assertIn("No deployExit marker", result.stderr)
        self.assertEqual((self.root / "requests").read_text().splitlines(), ["1000000:"])

    def test_log_read_error_fails(self):
        self.tool("aws", "exit 1")
        result = self.run_step("deploy", "Follow command logs")
        self.assert_exit(result, 1)
        self.assertIn("Could not read command logs", result.stderr)

    def test_no_logs_after_ten_minutes_fails(self):
        self.pages([{"events": []}])
        result = self.run_step("deploy", "Follow command logs", NOW="1600")
        self.assert_exit(result, 1)
        self.assertIn("Command was not delivered", result.stderr)

    def test_no_exit_marker_after_65_minutes_fails(self):
        result = self.run_step("deploy", "Follow command logs", NOW="4900")
        self.assert_exit(result, 1)
        self.assertIn("No deployExit marker", result.stderr)


if __name__ == "__main__":
    unittest.main()
