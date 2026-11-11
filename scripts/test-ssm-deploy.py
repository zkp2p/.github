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
            "HOST_TIMEOUT_SECONDS": "3900",
            "ENVIRONMENTS_FILE": "environments.yml", "REF": "releases/staging",
            "SLACK_WEBHOOK_URL": "https://example.invalid/webhook",
            "REPO": "example/repo", "DEPLOY_ENVIRONMENT": "staging", "MODE": "image",
            "SHA": "a" * 40, "RESULT": "success", "RUN_URL": "https://example.invalid/run",
            "ARTIFACT_KIND": "docker", "EIF_ENABLED": "",
            "ARTIFACT_KEY": "example/repo/41051883943/2/image.tar.gz",
            "MANIFEST_SHA256": "b" * 64, "IMAGE_ID": "sha256:" + "c" * 64,
            "SOURCE_SHA": "a" * 40, "ALLOW_KEY_REMOVAL": "false",
            "RUN_ID": "41051883943", "RUN_ATTEMPT": "2", "RUNNER_TEMP": str(self.root),
            "CI_TREE_REUSE": "false",
        }
        self.tool("timeout", 'shift\nexec "$@"')
        self.tool("date", 'printf "%s\\n" "${NOW:-1000}"')
        self.tool("sleep", 'echo "unexpected sleep" >&2; exit 97')
        self.tool("docker", 'echo "unexpected Docker fallback" >&2; exit 98')
        self.config = {
            "region": "test-region", "ssmDocument": "test-document", "logGroup": "test-logs",
            "environments": {"staging": {"branch": "releases/staging", "instanceId": "i-test",
                                         "roleArn": "test-role", "ci": "skip"}},
        }

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

    def test_host_timeout_input(self):
        host_timeout = WORKFLOW["on"]["workflow_call"]["inputs"].get("host-timeout-seconds", {})
        self.assertEqual(host_timeout.get("type"), "number")
        self.assertEqual(host_timeout.get("default"), 3900)
        self.assertFalse(host_timeout.get("required", False))

    def test_host_timeout_minutes(self):
        self.assertIn("Validate host timeout", [item["name"] for item in WORKFLOW["jobs"]["deploy"]["steps"]])
        for seconds, follow, deploy in ((600, 11, 25), (601, 12, 26), (2400, 41, 55), (3899, 66, 80),
                                        (3900, 66, 80), (3901, 67, 81), (5700, 96, 110),
                                        (6599, 111, 125), (6600, 111, 125)):
            with self.subTest(seconds=seconds):
                result = self.run_step("deploy", "Validate host timeout", HOST_TIMEOUT_SECONDS=str(seconds))
                self.assert_exit(result, 0)
                outputs = dict(line.split("=", 1) for line in (self.root / "outputs").read_text().splitlines())
                self.assertEqual(outputs["follow-timeout-minutes"], str(follow))
                self.assertEqual(outputs["deploy-timeout-minutes"], str(deploy))

    def test_invalid_host_timeout_fails_before_outputs(self):
        self.assertIn("Validate host timeout", [item["name"] for item in WORKFLOW["jobs"]["deploy"]["steps"]])
        for value in ("", "0", "-1", "599", "6601", "7200", "14400", "14401", "600.5", "3900.1",
                      "1e20", "18446744073709552216", "abc", "600\nother=value"):
            with self.subTest(value=value):
                (self.root / "outputs").unlink(missing_ok=True)
                result = self.run_step("deploy", "Validate host timeout", HOST_TIMEOUT_SECONDS=value)
                self.assert_exit(result, 1)
                self.assertIn("host-timeout-seconds must be an integer between 600 and 6600", result.stderr)
                self.assertFalse((self.root / "outputs").exists())

    def config_tool(self, name, config=None):
        if config is None:
            config = self.config
        body = 'printf "%s\\n" \'' + json.dumps(config) + "'"
        if name == "yq":
            body = 'if [[ "$1" == --version ]]; then echo "$YQ_VERSION"; exit 0; fi\n' + body
        else:
            body = '[[ "$*" == *mikefarah/yq:4.44.3* ]]\n' + body
        self.tool(name, body)

    def assert_resolved_values(self, region="test-region", document="test-document", log_group="test-logs"):
        self.config_tool("docker")
        self.assert_exit(self.run_step("deploy", "Resolve environment"), 0)
        outputs = dict(line.split("=", 1) for line in (self.root / "outputs").read_text().splitlines())
        self.assertEqual(outputs, {
            "environment": "staging", "instance-id": "i-test", "role-arn": "test-role",
            "region": region, "document": document, "log-group": log_group, "ci": "skip",
        })

    def test_resolve_environment_level_only(self):
        environment = self.config["environments"]["staging"]
        for key in ("region", "ssmDocument", "logGroup"):
            environment[key] = self.config.pop(key)
        self.assert_resolved_values()

    def test_resolve_top_level_only(self):
        self.assert_resolved_values()

    def test_resolve_environment_overrides_top_level(self):
        self.config["environments"]["staging"].update({
            "region": "env-region", "ssmDocument": "env-document", "logGroup": "env-logs",
        })
        self.assert_resolved_values(region="env-region", document="env-document", log_group="env-logs")

    def test_resolve_falls_back_per_field(self):
        self.config["environments"]["staging"]["ssmDocument"] = "env-document"
        self.assert_resolved_values(document="env-document")

    def test_resolve_missing_at_both_levels_fails(self):
        for key in ("region", "ssmDocument", "logGroup"):
            with self.subTest(key=key):
                config = {name: value for name, value in self.config.items() if name != key}
                self.config_tool("docker", config)
                result = self.run_step("deploy", "Resolve environment")
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("Invalid environment configuration", result.stderr)
                self.assertFalse((self.root / "outputs").exists())

    def test_resolve_rejects_invalid_values(self):
        for level in ("top", "environment"):
            for key in ("region", "ssmDocument", "logGroup"):
                for value in ("", "first\nsecond", "first\rsecond", 123, True, [], {}):
                    with self.subTest(level=level, key=key, value=value):
                        config = json.loads(json.dumps(self.config))
                        target = config if level == "top" else config["environments"]["staging"]
                        target[key] = value
                        self.config_tool("docker", config)
                        result = self.run_step("deploy", "Resolve environment")
                        self.assertNotEqual(result.returncode, 0)
                        self.assertFalse((self.root / "outputs").exists())

    def test_native_yq(self):
        self.config_tool("yq")
        for version in ("yq (https://github.com/mikefarah/yq/) version v4.44.3", "yq version v4.44.3"):
            with self.subTest(version=version):
                result = self.run_step("deploy", "Resolve environment", YQ_VERSION=version)
                self.assert_exit(result, 0)
                self.assertIn("environment=staging", (self.root / "outputs").read_text())

    def test_missing_yq_uses_pinned_docker(self):
        self.config_tool("docker")
        self.assert_exit(self.run_step("deploy", "Resolve environment"), 0)

    def test_incompatible_yq_fails_without_docker(self):
        self.config_tool("yq")
        self.config_tool("docker")
        self.assert_exit(self.run_step("deploy", "Resolve environment", YQ_VERSION="yq 3.4.3"), 1)

    def test_native_yq_read_failure_does_not_fall_back(self):
        self.tool("yq", 'if [[ "$1" == --version ]]; then echo "yq version v4.44.3"; else exit 1; fi')
        self.config_tool("docker")
        result = self.run_step("deploy", "Resolve environment")
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

    def test_parameters_carry_run_identity(self):
        normalize = step("deploy", "Normalize deploy parameters")
        self.assertEqual((normalize["env"]["RUN_ID"], normalize["env"]["RUN_ATTEMPT"]),
                         ("${{ github.run_id }}", "${{ github.run_attempt }}"))
        image = {"MODE": "image", "ARTIFACT_KEY": "example/repo/41051883943/2/image.tar.gz",
                 "MANIFEST_SHA256": "b" * 64, "IMAGE_ID": "sha256:" + "c" * 64, "SOURCE_SHA": "a" * 40}
        for mode, values in (("image", image), ("config", {"MODE": "config"})):
            with self.subTest(mode=mode):
                result = self.run_step("deploy", "Normalize deploy parameters", RUNNER_TEMP=str(self.root),
                                       ALLOW_KEY_REMOVAL="false", RUN_ID="41051883943", RUN_ATTEMPT="2", **values)
                self.assert_exit(result, 0)
                parameters = json.loads((self.root / "ssm-parameters.json").read_text())
                self.assertEqual((parameters["RunId"], parameters["RunAttempt"]), (["41051883943"], ["2"]))
                self.assertEqual(sorted(parameters), ["AllowKeyRemoval", "ArtifactKey", "ImageId", "ManifestSha256",
                                                      "Mode", "RunAttempt", "RunId", "SourceSha"])

    def normalize_parameters(self, **env):
        result = self.run_step("deploy", "Validate artifact identity", **env)
        if result.returncode != 0:
            return result
        return self.run_step("deploy", "Normalize deploy parameters", **env)

    def test_artifact_kind_defaults_to_docker(self):
        self.assertEqual(set(WORKFLOW["on"]), {"workflow_call"})
        artifact_kind = WORKFLOW["on"]["workflow_call"]["inputs"].get("artifact-kind", {})
        self.assertEqual(artifact_kind.get("type"), "string")
        self.assertEqual(artifact_kind.get("default"), "docker")
        self.assertFalse(artifact_kind.get("required", False))

    def test_artifact_validation_precedes_external_actions(self):
        resolve = WORKFLOW["jobs"]["deploy"]
        self.assertEqual(resolve["steps"][0]["name"], "Validate artifact identity")
        validation = resolve["steps"][0]
        self.assertNotIn("if", validation)
        self.assertFalse(validation.get("continue-on-error", False))
        self.assertEqual(validation["env"], {
            "MODE": "${{ inputs.mode }}", "ARTIFACT_KIND": "${{ inputs.artifact-kind }}",
            "IMAGE_ID": "${{ inputs.image-id }}", "ALLOW_KEY_REMOVAL": "${{ inputs.allow-key-removal }}",
            "EIF_ENABLED": "${{ vars.SSM_DEPLOY_EIF_ENABLED }}",
        })
        gate = step("deploy", "Wait for CI on the image commit")
        self.assertEqual(gate["if"], "steps.resolve.outputs.ci == 'required' && (inputs.mode == 'image' || inputs.mode == 'parent')")
        self.assertEqual(gate["env"]["SHA"], "${{ inputs.source-sha }}")

    def test_single_job_order_permissions_and_timeouts(self):
        self.assertEqual(set(WORKFLOW["jobs"]), {"deploy"})
        deploy = WORKFLOW["jobs"]["deploy"]
        self.assertNotIn("needs", deploy)
        self.assertNotIn("if", deploy)
        self.assertEqual(deploy["runs-on"], "${{ inputs.runner }}")
        self.assertEqual(deploy["permissions"], {
            "contents": "read", "actions": "write", "id-token": "write", "deployments": "write",
        })
        self.assertEqual(deploy["timeout-minutes"], 190)
        self.assertEqual(step("deploy", "Wait for CI on the image commit")["timeout-minutes"], 50)
        self.assertEqual(step("deploy", "Follow command logs")["timeout-minutes"],
                         "${{ fromJSON(steps.timeouts.outputs.follow-timeout-minutes) }}")
        names = [item["name"] for item in deploy["steps"]]
        self.assertEqual(names[:7], [
            "Validate artifact identity", "Patch hosted action runtime", "Validate host timeout",
            "Check out caller environment map", "Resolve environment",
            "Wait for CI on the image commit", "Normalize deploy parameters",
        ])
        self.assertEqual(names.count("Patch hosted action runtime"), 1)
        self.assertLess(names.index("Wait for CI on the image commit"), names.index("Assume deploy role"))
        role = step("deploy", "Assume deploy role")
        self.assertEqual(role["with"]["role-duration-seconds"], 7200)
        self.assertNotIn("if", role)
        checkout = step("deploy", "Check out caller environment map")["with"]
        self.assertEqual(checkout["ref"], "${{ github.sha }}")
        self.assertEqual(checkout["sparse-checkout"], "${{ inputs.environments-file }}")
        self.assertFalse(checkout["sparse-checkout-cone-mode"])
        self.assertFalse(checkout["persist-credentials"])
        for item in deploy["steps"]:
            if "uses" in item:
                self.assertRegex(item["uses"], r"@[a-f0-9]{40}$")

    def test_docker_and_eif_parameter_contract(self):
        for kind, identity, artifact in (
            ("docker", "sha256:" + "c" * 64, "image"),
            ("eif", "pcr0:" + "0123456789abcdef" * 6, "attestation-eif"),
        ):
            with self.subTest(kind=kind):
                key = f"example/repo/41051883943/2/{artifact}.tar.gz"
                result = self.normalize_parameters(ARTIFACT_KIND=kind, IMAGE_ID=identity,
                                                   ARTIFACT_KEY=key, EIF_ENABLED="true")
                self.assert_exit(result, 0)
                path = self.root / "ssm-parameters.json"
                self.assertEqual(json.loads(path.read_text()), {
                    "Mode": ["image"], "ArtifactKey": [key], "ManifestSha256": ["b" * 64],
                    "ImageId": [identity], "SourceSha": ["a" * 40], "AllowKeyRemoval": ["false"],
                    "RunId": ["41051883943"], "RunAttempt": ["2"],
                })
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_config_modes_keep_legacy_normalization(self):
        for mode in ("config", "config-plan"):
            for allow in ("false", "true"):
                with self.subTest(mode=mode, allow=allow):
                    # Artifact fields are ignored for config, even when nonempty or invalid.
                    result = self.normalize_parameters(MODE=mode, ALLOW_KEY_REMOVAL=allow,
                                                       IMAGE_ID="pcr0:ignored", MANIFEST_SHA256="ignored",
                                                       ARTIFACT_KEY="ignored", SOURCE_SHA="ignored")
                    self.assert_exit(result, 0)
                    self.assertEqual(json.loads((self.root / "ssm-parameters.json").read_text()), {
                        "Mode": [mode], "ArtifactKey": ["none"], "ManifestSha256": ["none"],
                        "ImageId": ["none"], "SourceSha": ["none"], "AllowKeyRemoval": [allow],
                        "RunId": ["41051883943"], "RunAttempt": ["2"],
                    })

    def normalize_parent_parameters(self, **env):
        values = {
            "MODE": "parent", "ARTIFACT_KIND": "eif", "EIF_ENABLED": "true",
            "IMAGE_ID": "parent:" + "c" * 64,
            "ARTIFACT_KEY": "zkp2p/attestation-service/parent/" + "b" * 64 + "/parent-bundle.tar.gz",
        }
        return self.normalize_parameters(**{**values, **env})

    def test_parent_mode_accepts_eif_parent_id(self):
        self.assert_exit(self.run_step("deploy", "Validate artifact identity", MODE="parent",
                                       ARTIFACT_KIND="eif", IMAGE_ID="parent:" + "0123456789abcdef" * 4,
                                       EIF_ENABLED="true"), 0)

    def test_parent_mode_rejects_docker(self):
        for identity in ("parent:" + "c" * 64, "sha256:" + "c" * 64):
            with self.subTest(identity=identity):
                self.assert_exit(self.normalize_parent_parameters(ARTIFACT_KIND="docker",
                                                                   IMAGE_ID=identity), 1)
                self.assertFalse((self.root / "ssm-parameters.json").exists())

    def test_parent_mode_rejects_pcr0_id(self):
        for identity in ("pcr0:" + "c" * 96, "sha256:" + "c" * 64, "", "none",
                         "parent:" + "c" * 63, "parent:" + "c" * 65, "parent:" + "g" * 64,
                         "parent:" + "C" * 64, "parent:" + "c" * 64 + "\n",
                         " parent:" + "c" * 64):
            with self.subTest(identity=identity):
                self.assert_exit(self.normalize_parent_parameters(IMAGE_ID=identity), 1)
                self.assertFalse((self.root / "ssm-parameters.json").exists())

    def test_parent_mode_runs_ci_gate(self):
        self.assertEqual(step("deploy", "Wait for CI on the image commit")["if"],
                         "steps.resolve.outputs.ci == 'required' && (inputs.mode == 'image' || inputs.mode == 'parent')")
        self.tool("gh", '''
[[ "$1" == api ]]
[[ "$2" == "repos/$REPO/actions/workflows/$CI_WORKFLOW/runs?head_sha=$SHA&per_page=20" ]]
echo '{"workflow_runs":[{"status":"completed","conclusion":"success"}]}'
''')
        gate = step("deploy", "Wait for CI on the image commit")
        self.assert_exit(self.run_step("deploy", "Wait for CI on the image commit", MODE="parent",
                                       SHA="d" * 40, CI_WORKFLOW="ci.yml",
                                       DECISION_JQ=gate["env"]["DECISION_JQ"]), 0)

    def test_parent_mode_normalized_ssm_parameters(self):
        self.assertEqual(step("deploy", "Normalize deploy parameters")["env"]["DEPLOY_ENVIRONMENT"],
                         "${{ steps.resolve.outputs.environment }}")
        for environment, prefix in (("staging", ""), ("prod", "prod/")):
            with self.subTest(environment=environment):
                key = f"zkp2p/attestation-service/{prefix}parent/{'b' * 64}/parent-bundle.tar.gz"
                self.assert_exit(self.normalize_parent_parameters(DEPLOY_ENVIRONMENT=environment,
                                                                   ARTIFACT_KEY=key), 0)
                path = self.root / "ssm-parameters.json"
                self.assertEqual(json.loads(path.read_text()), {
                    "Mode": ["parent"], "ArtifactKey": [key], "ManifestSha256": ["b" * 64],
                    "ImageId": ["parent:" + "c" * 64], "SourceSha": ["a" * 40],
                    "AllowKeyRemoval": ["false"], "RunId": ["41051883943"], "RunAttempt": ["2"],
                })
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)

    def test_image_mode_unchanged(self):
        for kind, identity, artifact, enabled, allow in (
            ("docker", "sha256:" + "c" * 64, "image", "", "false"),
            ("docker", "sha256:" + "c" * 64, "image", "false", "true"),
            ("eif", "pcr0:" + "0123456789abcdef" * 6, "attestation-eif", "true", "false"),
        ):
            for environment, prefix in (("staging", ""), ("prod", "prod/")):
                with self.subTest(kind=kind, environment=environment, allow=allow):
                    key = f"zkp2p/attestation-service/{prefix}41051883943/2/{artifact}.tar.gz"
                    result = self.normalize_parameters(ARTIFACT_KIND=kind, IMAGE_ID=identity,
                                                       ARTIFACT_KEY=key, EIF_ENABLED=enabled,
                                                       ALLOW_KEY_REMOVAL=allow, DEPLOY_ENVIRONMENT=environment)
                    self.assert_exit(result, 0)
                    expected = {
                        "Mode": ["image"], "ArtifactKey": [key], "ManifestSha256": ["b" * 64],
                        "ImageId": [identity], "SourceSha": ["a" * 40], "AllowKeyRemoval": [allow],
                        "RunId": ["41051883943"], "RunAttempt": ["2"],
                    }
                    self.assertEqual((self.root / "ssm-parameters.json").read_bytes(),
                                     (json.dumps(expected, indent=2) + "\n").encode())
                    self.assertEqual((result.stdout, result.stderr), ("", ""))

    def test_parent_mode_requires_eif_opt_in(self):
        for enabled in ("", "false", "1", "TRUE", "true\n"):
            with self.subTest(enabled=enabled):
                result = self.normalize_parent_parameters(EIF_ENABLED=enabled)
                self.assert_exit(result, 1)
                self.assertIn("SSM_DEPLOY_EIF_ENABLED=true", result.stderr)
                self.assertFalse((self.root / "ssm-parameters.json").exists())

    def test_parent_mode_rejects_key_removal(self):
        for allow in ("true", "", "FALSE", "false\n"):
            with self.subTest(allow=allow):
                self.assert_exit(self.normalize_parent_parameters(ALLOW_KEY_REMOVAL=allow), 1)
                self.assertFalse((self.root / "ssm-parameters.json").exists())

    def test_parent_mode_rejects_invalid_artifact_keys(self):
        for name in ("aws", "gh", "curl", "docker"):
            self.tool(name, 'echo called >> "$STATE/external-calls"; exit 99')
        for environment, prefix, other_prefix in (("staging", "", "prod/"), ("prod", "prod/", "")):
            base = f"zkp2p/attestation-service/{prefix}parent/"
            key = base + "b" * 64 + "/parent-bundle.tar.gz"
            for invalid in (
                "", "none", f"zkp2p/attestation-service/{prefix}41051883943/2/attestation-eif.tar.gz",
                f"zkp2p/attestation-service/{other_prefix}parent/{'b' * 64}/parent-bundle.tar.gz",
                key.replace("zkp2p/attestation-service", "example/repo"),
                key.replace("parent/", "parent//"), key.replace("parent/", "parent/../parent/"),
                key.replace("parent-bundle.tar.gz", "attestation-eif.tar.gz"),
                key.replace("parent-bundle.tar.gz", "parent-manifest.json"),
                key.replace("parent-bundle.tar.gz", "parent-bundleXtarXgz"),
                base + "b" * 63 + "/parent-bundle.tar.gz", base + "b" * 65 + "/parent-bundle.tar.gz",
                base + "B" * 64 + "/parent-bundle.tar.gz", base + "g" * 64 + "/parent-bundle.tar.gz",
                base + "c" * 64 + "/parent-bundle.tar.gz",  # Key digest must match the manifest, not ImageId.
                "/" + key, "s3://bucket/" + key, " " + key, key + "\n", key + "/extra", key + "?versionId=x",
            ):
                with self.subTest(environment=environment, key=invalid):
                    self.assert_exit(self.normalize_parent_parameters(DEPLOY_ENVIRONMENT=environment,
                                                                       ARTIFACT_KEY=invalid), 1)
                    self.assertFalse((self.root / "ssm-parameters.json").exists())
                    self.assertFalse((self.root / "external-calls").exists())
        for environment in ("", "production", "dev", "STAGING", "staging\n"):
            with self.subTest(environment=environment):
                self.assert_exit(self.normalize_parent_parameters(DEPLOY_ENVIRONMENT=environment), 1)
                self.assertFalse((self.root / "ssm-parameters.json").exists())

    def test_parent_mode_rejects_invalid_manifest_and_source(self):
        for field, size in (("MANIFEST_SHA256", 64), ("SOURCE_SHA", 40)):
            for value in ("", "none", "a" * (size - 1), "a" * (size + 1), "g" * size,
                          "A" * size, "a" * size + "\n"):
                with self.subTest(field=field, value=value):
                    self.assert_exit(self.normalize_parent_parameters(**{field: value}), 1)
                    self.assertFalse((self.root / "ssm-parameters.json").exists())

    def test_image_mode_rejects_parent_artifact_keys(self):
        for kind, identity in (("docker", "sha256:" + "c" * 64), ("eif", "pcr0:" + "c" * 96)):
            for prefix in ("", "prod/"):
                with self.subTest(kind=kind, prefix=prefix):
                    key = f"zkp2p/attestation-service/{prefix}parent/{'b' * 64}/parent-bundle.tar.gz"
                    self.assert_exit(self.normalize_parameters(ARTIFACT_KIND=kind, IMAGE_ID=identity,
                                                               ARTIFACT_KEY=key, EIF_ENABLED="true"), 1)
                    self.assertFalse((self.root / "ssm-parameters.json").exists())

    def test_invalid_artifact_identity_fails_before_external_calls(self):
        for name in ("aws", "gh", "curl", "docker"):
            self.tool(name, 'echo called >> "$STATE/external-calls"; exit 99')
        cases = [
            {"ARTIFACT_KIND": kind, "IMAGE_ID": identity}
            for kind, prefix, size in (("docker", "sha256:", 64), ("eif", "pcr0:", 96))
            for identity in ("", "none", prefix + "a" * (size - 1), prefix + "a" * (size + 1),
                             prefix + "g" * size, prefix + "A" * size, prefix + "a" * size + "\n",
                             " " + prefix + "a" * size, "invalid:" + "a" * size,
                             "pcr0:sha256:" + "a" * size)
        ]
        cases += [
            {"ARTIFACT_KIND": "eif", "IMAGE_ID": "sha256:" + "a" * 64},
            {"IMAGE_ID": "pcr0:" + "a" * 96},  # Kind omitted: the Docker default applies.
            {"ARTIFACT_KIND": "eif", "MODE": "config", "IMAGE_ID": "pcr0:" + "a" * 96},
            {"ARTIFACT_KIND": "eif", "MODE": "config-plan", "IMAGE_ID": "pcr0:" + "a" * 96},
            {"ARTIFACT_KIND": "eif", "IMAGE_ID": "pcr0:" + "a" * 96, "ALLOW_KEY_REMOVAL": "true"},
            {"MODE": "unknown"},
        ]
        cases += [{"ARTIFACT_KIND": kind, "MODE": mode}
                  for kind in ("", "image", "EIF", "unknown", "eif\n")
                  for mode in ("image", "config", "config-plan")]
        for values in cases:
            with self.subTest(values=values):
                result = self.normalize_parameters(EIF_ENABLED="true", **values)
                self.assert_exit(result, 1)
                self.assertFalse((self.root / "ssm-parameters.json").exists())
                self.assertFalse((self.root / "external-calls").exists())

    def test_eif_is_dormant_until_explicitly_enabled(self):
        for value in ("", "false", "1", "TRUE", "true\n"):
            with self.subTest(value=value):
                result = self.normalize_parameters(ARTIFACT_KIND="eif", IMAGE_ID="pcr0:" + "a" * 96,
                                                   EIF_ENABLED=value)
                self.assert_exit(result, 1)
                self.assertIn("SSM_DEPLOY_EIF_ENABLED=true", result.stderr)
                self.assertFalse((self.root / "ssm-parameters.json").exists())
                # Existing image callers do not depend on the opt-in variable.
                self.assert_exit(self.run_step("deploy", "Validate artifact identity", EIF_ENABLED=value), 0)

    def test_source_and_manifest_validation_remain_strict(self):
        for kind, identity in (("docker", "sha256:" + "a" * 64), ("eif", "pcr0:" + "a" * 96)):
            for field, size in (("MANIFEST_SHA256", 64), ("SOURCE_SHA", 40)):
                for value in ("", "none", "a" * (size - 1), "a" * (size + 1), "g" * size,
                              "A" * size, "a" * size + "\n"):
                    with self.subTest(kind=kind, field=field, value=value):
                        self.assert_exit(self.normalize_parameters(
                            ARTIFACT_KIND=kind, IMAGE_ID=identity, EIF_ENABLED="true", **{field: value}), 1)
                        self.assertFalse((self.root / "ssm-parameters.json").exists())

    def test_ci_gate_queries_exact_source_and_dispatches_once(self):
        self.tool("gh", '''
printf '%s\\n' "$*" >> "$STATE/gh-calls"
case "$1" in
  api)
    [[ "$2" == "repos/$REPO/actions/workflows/$CI_WORKFLOW/runs?head_sha=$SHA&per_page=20" ]]
    index=$(jq -r '. // 0' "$STATE/ci-index" 2>/dev/null || echo 0)
    printf '%s\\n' "$((index + 1))" > "$STATE/ci-index"
    case "$index" in
      0|1) echo '{"workflow_runs":[]}' ;;
      2) echo '{"workflow_runs":[{"status":"in_progress","conclusion":null}]}' ;;
      3) echo '{"workflow_runs":[{"status":"completed","conclusion":"success"}]}' ;;
      *) exit 99 ;;
    esac
    ;;
  workflow) [[ "$*" == "workflow run $CI_WORKFLOW --ref $REF --repo $REPO" ]] ;;
  *) exit 99 ;;
esac
''')
        self.tool("sleep", '[[ "$1" == 30 ]]')
        gate = step("deploy", "Wait for CI on the image commit")
        result = self.run_step("deploy", "Wait for CI on the image commit",
                               SHA="d" * 40, CI_WORKFLOW="ci.yml", DECISION_JQ=gate["env"]["DECISION_JQ"])
        self.assert_exit(result, 0)
        calls = (self.root / "gh-calls").read_text().splitlines()
        self.assertEqual(calls.count(f"api repos/example/repo/actions/workflows/ci.yml/runs?head_sha={'d' * 40}&per_page=20"), 4)
        self.assertEqual(calls.count("workflow run ci.yml --ref releases/staging --repo example/repo"), 1)

    @staticmethod
    def ci_run(**values):
        return {"id": 42, "run_attempt": 1, "status": "completed", "conclusion": "failure",
                "created_at": "1970-01-01T00:10:00Z", "updated_at": "1970-01-01T00:15:00Z",
                **values}

    def ci_api(self, runs, jobs=None, commit=None, tree_runs=None):
        for name in ("ci-index", "ci-sleeps", "gh-calls", "ci-tree-index"):
            (self.root / name).unlink(missing_ok=True)
        for index, response in enumerate(runs):
            (self.root / f"ci-{index}.json").write_text(json.dumps({"workflow_runs": response}))
        if jobs is not None:
            (self.root / "ci-jobs.json").write_text(json.dumps(jobs))
        if commit is not None:
            (self.root / "ci-commit.json").write_text(json.dumps(commit))
        if tree_runs is not None:
            for index, response in enumerate(tree_runs):
                (self.root / f"ci-tree-{index}.json").write_text(json.dumps({"workflow_runs": response}))
        self.tool("timeout", '[[ "$1" == 60s ]]; shift; exec "$@"')
        self.tool("gh", '''
printf '%s\\n' "$*" >> "$STATE/gh-calls"
if [[ "$*" == "workflow run $CI_WORKFLOW --ref $REF --repo $REPO" ]]; then exit 0; fi
[[ "$1" == api ]]
shift
case "$1" in
  "repos/$REPO/git/commits/$SHA")
    jq . "$STATE/ci-commit.json"
    [[ "${CI_TREE_ERROR:-}" != commit ]]
    exit
    ;;
  "repos/$REPO/actions/workflows/$CI_WORKFLOW/runs?status=success&per_page=100")
    index=$(jq -r . "$STATE/ci-tree-index" 2>/dev/null || echo 0)
    echo "$((index + 1))" > "$STATE/ci-tree-index"
    jq . "$STATE/ci-tree-$index.json"
    [[ "${CI_TREE_ERROR:-}" != runs ]]
    exit
    ;;
esac
if [[ "$1" == --paginate ]]; then
  [[ "$2" == --slurp ]]
  shift 2
  [[ "$1" == "repos/$REPO/actions/runs/${CI_RUN_ID:-42}/attempts/${CI_ATTEMPT:-1}/jobs?per_page=100" ]]
  jq . "$STATE/ci-jobs.json"
else
  [[ "$1" == "repos/$REPO/actions/workflows/$CI_WORKFLOW/runs?head_sha=$SHA&per_page=20" ]]
  index=$(jq -r . "$STATE/ci-index" 2>/dev/null || echo 0)
  echo "$((index + 1))" > "$STATE/ci-index"
  jq . "$STATE/ci-$index.json"
fi
''')
        self.tool("sleep", '[[ "$1" == 30 ]]; echo "$1" >> "$STATE/ci-sleeps"')

    def run_ci(self, **env):
        gate = step("deploy", "Wait for CI on the image commit")
        filters = {name: value for name, value in gate["env"].items() if name.endswith("_JQ")}
        return self.run_step("deploy", gate["name"], CI_WORKFLOW="ci.yml", **filters, **env)

    def test_ci_runner_loss_waits_for_new_attempt(self):
        self.ci_api([[self.ci_run()],
                     [self.ci_run(run_attempt=2, status="in_progress", conclusion=None)],
                     [self.ci_run(run_attempt=2, conclusion="success")]],
                    jobs=[{"jobs": [{"conclusion": "failure", "steps": [{"conclusion": "cancelled"}]}]}])
        result = self.run_ci()
        self.assert_exit(result, 0)
        self.assertIn("runner-loss grace", result.stdout)
        self.assertEqual((self.root / "ci-sleeps").read_text().splitlines(), ["30", "30"])
        self.assertEqual((self.root / "gh-calls").read_text().count("/attempts/1/jobs"), 1)

    def test_ci_lost_support_shards_and_failed_aggregator_wait_for_new_attempt(self):
        jobs = [{"jobs": [
            {"name": f"support-api tests ({shard}/4)", "conclusion": "failure",
             "steps": [{"conclusion": "success"}, {"conclusion": "cancelled"}]}
            for shard in range(1, 4)
        ] + [{"name": "support-api tests (4/4)", "conclusion": "success",
              "steps": [{"conclusion": "success"}]}]},
            {"jobs": [{"name": "support / support-api", "conclusion": "failure",
                       "steps": [{"name": "Require support-api build and every test shard",
                                  "conclusion": "failure"}]}]}]
        for attempt in (1, 2):
            with self.subTest(attempt=attempt):
                self.ci_api([[self.ci_run(run_attempt=attempt)],
                             [self.ci_run(run_attempt=attempt + 1, status="in_progress", conclusion=None)],
                             [self.ci_run(run_attempt=attempt + 1, conclusion="success")]], jobs=jobs)
                result = self.run_ci(CI_ATTEMPT=str(attempt))
                self.assert_exit(result, 0)
                self.assertIn(f"CI run 42 attempt {attempt}: runner-loss grace; waiting up to 10 minutes "
                              "after 1970-01-01T00:15:00Z", result.stdout)
                self.assertEqual((self.root / "ci-sleeps").read_text().splitlines(), ["30", "30"])
                self.assertEqual((self.root / "gh-calls").read_text().count(f"/attempts/{attempt}/jobs"), 1)

    def test_ci_runner_loss_fails_at_grace_deadline(self):
        self.ci_api([[self.ci_run()]],
                    jobs=[{"jobs": [{"conclusion": "failure", "steps": [{"conclusion": "cancelled"}]}]}])
        result = self.run_ci(NOW="1500")
        self.assert_exit(result, 1)
        self.assertIn("CI failed for the image commit", result.stderr)
        self.assertFalse((self.root / "ci-sleeps").exists())

    def test_ci_runner_loss_grace_expires_while_waiting(self):
        self.ci_api([[self.ci_run()], [self.ci_run()]],
                    jobs=[{"jobs": [{"conclusion": "failure", "steps": [{"conclusion": "cancelled"}]}]}])
        self.tool("date", 'if [[ -f "$STATE/ci-sleeps" ]]; then echo 1500; else echo 1000; fi')
        result = self.run_ci()
        self.assert_exit(result, 1)
        self.assertIn("CI failed for the image commit", result.stderr)
        self.assertEqual((self.root / "ci-sleeps").read_text().splitlines(), ["30"])

    def test_ci_failed_steps_without_runner_loss_fail_immediately(self):
        for conclusion in ("failure", "cancelled", "success"):
            with self.subTest(conclusion=conclusion):
                (self.root / "ci-index").unlink(missing_ok=True)
                self.ci_api([[self.ci_run()]], jobs=[
                    {"jobs": [{"conclusion": "failure", "steps": []}]},
                    {"jobs": [{"conclusion": conclusion, "steps": [{"conclusion": "failure"}]}]},
                ])
                result = self.run_ci()
                self.assert_exit(result, 1)
                self.assertIn("CI failed for the image commit", result.stderr)
                self.assertFalse((self.root / "ci-sleeps").exists())

    def test_ci_invalid_or_empty_steps_do_not_get_grace(self):
        for conclusion in ("failure", "cancelled"):
            for evidence in ({}, {"steps": []}, {"steps": None}, {"steps": {}}, {"steps": "unknown"}):
                with self.subTest(conclusion=conclusion, evidence=evidence):
                    self.ci_api([[self.ci_run()]], jobs=[{"jobs": [
                        {"conclusion": conclusion, **evidence},
                        {"conclusion": "success", "steps": [{"conclusion": "success"}]},
                    ]}])
                    result = self.run_ci()
                    self.assert_exit(result, 1)
                    self.assertIn("CI failed for the image commit", result.stderr)
                    self.assertNotIn("runner-loss grace", result.stdout)
                    self.assertFalse((self.root / "ci-sleeps").exists())

    def test_ci_third_attempt_fails_without_grace(self):
        self.ci_api([[self.ci_run(run_attempt=3)]])
        result = self.run_ci()
        self.assert_exit(result, 1)
        self.assertIn("CI failed for the image commit", result.stderr)
        self.assertNotIn("/jobs", (self.root / "gh-calls").read_text())
        self.assertFalse((self.root / "ci-sleeps").exists())

    def test_ci_grace_uses_newest_run_and_latest_attempt(self):
        newer = self.ci_run(id=43, run_attempt=2, created_at="1970-01-01T00:11:00Z")
        self.ci_api([[newer, self.ci_run()], [self.ci_run(conclusion="success")]],
                    jobs=[{"jobs": [{"conclusion": "cancelled", "steps": [{"conclusion": "cancelled"}]}]}])
        result = self.run_ci(CI_RUN_ID="43", CI_ATTEMPT="2")
        self.assert_exit(result, 0)
        self.assertIn("/runs/43/attempts/2/jobs", (self.root / "gh-calls").read_text())

    def test_ci_jobs_api_error_fails_closed(self):
        self.ci_api([[self.ci_run()]])
        result = self.run_ci()
        self.assert_exit(result, 1)
        self.assertIn("CI failed for the image commit", result.stderr)
        self.assertFalse((self.root / "ci-sleeps").exists())

    def test_ci_runner_loss_does_not_extend_overall_deadline(self):
        self.ci_api([[self.ci_run()]],
                    jobs=[{"jobs": [{"conclusion": "failure", "steps": [{"conclusion": "cancelled"}]}]}])
        self.tool("date", 'if [[ -f "$STATE/ci-sleeps" ]]; then echo 3700; else echo 1000; fi')
        result = self.run_ci()
        self.assert_exit(result, 1)
        self.assertIn("CI did not pass within 45 minutes", result.stderr)
        self.assertEqual((self.root / "ci-sleeps").read_text().splitlines(), ["30"])

    def test_ci_tree_reuse_is_opt_in(self):
        inputs = WORKFLOW["on"]["workflow_call"]["inputs"]
        self.assertEqual(inputs["ci-tree-reuse"]["type"], "boolean")
        self.assertIs(inputs["ci-tree-reuse"]["default"], False)
        self.assertEqual(step("deploy", "Wait for CI on the image commit")["env"]["CI_TREE_REUSE"],
                         "${{ inputs.ci-tree-reuse }}")

    def successful_tree_run(self, tree="e" * 40, **values):
        return self.ci_run(conclusion="success", head_commit={"tree_id": tree},
                           html_url="https://example.invalid/actions/runs/99", head_sha="b" * 40, **values)

    def test_ci_tree_match_passes_before_fail_wait_or_dispatch(self):
        for runs in ([self.ci_run(run_attempt=3)],
                     [self.ci_run(status="in_progress", conclusion=None)], []):
            with self.subTest(runs=runs):
                self.ci_api([runs], commit={"tree": {"sha": "e" * 40}},
                            tree_runs=[[self.successful_tree_run()]])
                result = self.run_ci(CI_TREE_REUSE="true")
                self.assert_exit(result, 0)
                self.assertIn("https://example.invalid/actions/runs/99", result.stdout)
                self.assertIn("b" * 40, result.stdout)
                self.assertFalse((self.root / "ci-sleeps").exists())
                calls = (self.root / "gh-calls").read_text()
                self.assertNotIn("workflow run", calls)
                self.assertNotIn("/jobs", calls)

    def test_ci_tree_mismatch_preserves_fail_wait_or_dispatch(self):
        for runs, expected, dispatch in (([self.ci_run(run_attempt=3)], 1, False),
                                         ([self.ci_run(status="in_progress", conclusion=None)], 0, False),
                                         ([], 0, True)):
            with self.subTest(runs=runs):
                self.ci_api([runs, [self.ci_run(conclusion="success")]],
                            commit={"tree": {"sha": "e" * 40}},
                            tree_runs=[[self.successful_tree_run(tree="f" * 40)]])
                result = self.run_ci(CI_TREE_REUSE="true")
                self.assert_exit(result, expected)
                self.assertNotIn("Reusing", result.stdout)
                calls = (self.root / "gh-calls").read_text()
                self.assertEqual("workflow run" in calls, dispatch)
                self.assertEqual((self.root / "ci-sleeps").exists(), expected == 0)

    def test_ci_tree_invalid_sha_never_passes_or_lists_runs(self):
        for tree in ("", "e" * 39, "e" * 41, "g" * 40, None, 123, "e" * 40 + "\n"):
            with self.subTest(tree=tree):
                self.ci_api([[self.ci_run(run_attempt=3)]], commit={"tree": {"sha": tree}},
                            tree_runs=[[self.successful_tree_run()]])
                result = self.run_ci(CI_TREE_REUSE="true")
                self.assert_exit(result, 1)
                self.assertIn("CI failed for the image commit", result.stderr)
                self.assertNotIn("status=success", (self.root / "gh-calls").read_text())

    def test_ci_tree_api_errors_never_pass_even_with_matching_output(self):
        for endpoint in ("commit", "runs"):
            with self.subTest(endpoint=endpoint):
                self.ci_api([[self.ci_run(run_attempt=3)]], commit={"tree": {"sha": "e" * 40}},
                            tree_runs=[[self.successful_tree_run()]])
                result = self.run_ci(CI_TREE_REUSE="true", CI_TREE_ERROR=endpoint)
                self.assert_exit(result, 1)
                self.assertIn("CI failed for the image commit", result.stderr)
                self.assertNotIn("Reusing", result.stdout)

    def test_ci_tree_api_error_falls_back_to_normal_wait(self):
        for endpoint in ("commit", "runs"):
            with self.subTest(endpoint=endpoint):
                self.ci_api([[self.ci_run(status="in_progress", conclusion=None)],
                             [self.ci_run(conclusion="success")]],
                            commit={"tree": {"sha": "e" * 40}}, tree_runs=[[self.successful_tree_run()]])
                result = self.run_ci(CI_TREE_REUSE="true", CI_TREE_ERROR=endpoint)
                self.assert_exit(result, 0)
                self.assertNotIn("Reusing", result.stdout)
                self.assertEqual((self.root / "ci-sleeps").read_text().splitlines(), ["30"])

    def test_ci_tree_malformed_responses_do_not_pass(self):
        for name in ("ci-commit.json", "ci-tree-0.json"):
            for response in ('{"message": "not found"}', 'not json'):
                with self.subTest(name=name, response=response):
                    self.ci_api([[self.ci_run(run_attempt=3)]], commit={"tree": {"sha": "e" * 40}},
                                tree_runs=[[self.successful_tree_run()]])
                    (self.root / name).write_text(response)
                    result = self.run_ci(CI_TREE_REUSE="true")
                    self.assert_exit(result, 1)
                    self.assertIn("CI failed for the image commit", result.stderr)

    def test_ci_tree_lookup_disabled_or_exact_sha_passes(self):
        for enabled, conclusion, expected in (("false", "failure", 1), ("true", "success", 0)):
            with self.subTest(enabled=enabled, conclusion=conclusion):
                self.ci_api([[self.ci_run(conclusion=conclusion, run_attempt=3)]])
                result = self.run_ci(CI_TREE_REUSE=enabled)
                self.assert_exit(result, expected)
                calls = (self.root / "gh-calls").read_text()
                self.assertNotIn("/git/commits/", calls)
                self.assertNotIn("status=success", calls)

    def test_ci_tree_lookup_repeats_after_five_polls(self):
        self.ci_api([[self.ci_run(status="in_progress", conclusion=None)]] * 6,
                    commit={"tree": {"sha": "e" * 40}}, tree_runs=[[], [self.successful_tree_run()]])
        result = self.run_ci(CI_TREE_REUSE="true")
        self.assert_exit(result, 0)
        calls = (self.root / "gh-calls").read_text().splitlines()
        polls = 0
        lookup_polls = []
        for call in calls:
            if "head_sha=" in call:
                polls += 1
            if "status=success" in call:
                lookup_polls.append(polls)
        self.assertEqual(lookup_polls, [1, 6])
        self.assertEqual((self.root / "ci-sleeps").read_text().splitlines(), ["30"] * 5)

    def test_ci_tree_reuse_does_not_pass_after_overall_deadline(self):
        self.ci_api([[self.ci_run(status="in_progress", conclusion=None)]],
                    commit={"tree": {"sha": "e" * 40}}, tree_runs=[[self.successful_tree_run()]])
        self.tool("date", 'if [[ -f "$STATE/ci-tree-index" ]]; then echo 3700; else echo 1000; fi')
        result = self.run_ci(CI_TREE_REUSE="true")
        self.assert_exit(result, 1)
        self.assertIn("CI did not pass within 45 minutes", result.stderr)

    def test_send_command_preserves_parameters_and_delivery_timeout(self):
        self.tool("aws", '''
[[ "$1 $2" == 'ssm send-command' ]]
printf '%s\\n' "$@" > "$STATE/aws-args"
echo '00000000-0000-0000-0000-000000000000'
''')
        self.assert_exit(self.normalize_parameters(ARTIFACT_KIND="eif", IMAGE_ID="pcr0:" + "a" * 96,
                                                  EIF_ENABLED="true"), 0)
        self.assert_exit(self.run_step("deploy", "Send SSM command", DOCUMENT="test-document",
                                       INSTANCE_ID="i-test", LOG_GROUP="test-logs"), 0)
        args = (self.root / "aws-args").read_text().splitlines()
        self.assertEqual(args[args.index("--timeout-seconds") + 1], "600")
        self.assertEqual(args[args.index("--parameters") + 1], f"file://{self.root}/ssm-parameters.json")

    def test_config_plan_skips_deployment_records(self):
        self.assertEqual(step("deploy", "Create GitHub deployment").get("if"),
                         "inputs.mode != 'config-plan'")
        self.assertEqual(step("deploy", "Record deployment result")["if"],
                         "always() && steps.deployment.outputs.id != ''")

    def deployment_api(self):
        self.tool("gh", '''
[[ "$1 $2 $3" == 'api --method POST' ]]
endpoint=$4
shift 4
case "$endpoint" in
  "repos/$REPO/deployments")
    [[ "$*" == '--input -' ]]
    jq . > "$STATE/deployment.json"
    echo '{"id": 42}'
    ;;
  "repos/$REPO/deployments/42/statuses")
    printf '%s\\n' "$@" > "$STATE/status-args"
    ;;
  *) exit 90 ;;
esac
''')

    def test_image_and_config_deployment_metadata(self):
        self.deployment_api()
        for mode, task in (("image", "deploy"), ("config", "deploy:config")):
            with self.subTest(mode=mode):
                result = self.run_step("deploy", "Create GitHub deployment", MODE=mode)
                self.assert_exit(result, 0)
                self.assertEqual(json.loads((self.root / "deployment.json").read_text()), {
                    "ref": self.env["SHA"], "environment": "staging", "auto_merge": False,
                    "required_contexts": [], "task": task,
                    "description": f"{mode} deploy of {self.env['SHA'][:7]}",
                })
                self.assertEqual((self.root / "outputs").read_text().splitlines()[-1], "id=42")
                self.assertEqual((self.root / "status-args").read_text().splitlines(),
                                 ["-f", "state=in_progress", "-f", f"log_url={self.env['RUN_URL']}"])

    def test_deployment_result_description(self):
        self.deployment_api()
        for mode in ("image", "config"):
            for result, description in (("success", "succeeded"), ("failure", "failed")):
                with self.subTest(mode=mode, result=result):
                    self.assert_exit(self.run_step("deploy", "Record deployment result",
                                                   MODE=mode, RESULT=result, DEPLOYMENT_ID="42"), 0)
                    args = (self.root / "status-args").read_text().splitlines()
                    self.assertEqual(args[::2], ["-f", "-f", "-f"])
                    self.assertCountEqual(args[1::2], [f"state={result}",
                                                      f"log_url={self.env['RUN_URL']}",
                                                      f"description={mode} deploy {description}"])

    def test_final_gate_and_slack_result_by_mode(self):
        gate = step("deploy", "Require successful deploy")
        self.assertEqual(gate["if"], "always() && steps.normalize.outcome != 'skipped'")
        self.assertEqual(step("deploy", "Notify Slack if configured")["if"], gate["if"])
        self.assertEqual(gate["env"]["RESULT"],
                         "${{ steps.poll.outcome == 'success' && (inputs.mode == 'config-plan' || steps.status.outcome == 'success') && 'success' || 'failure' }}")
        self.assertEqual(step("deploy", "Notify Slack if configured")["env"]["RESULT"],
                         gate["env"]["RESULT"])
        for mode in ("image", "config", "config-plan"):
            for poll in ("success", "failure", "skipped", "cancelled"):
                for status in ("success", "failure", "skipped", "cancelled"):
                    success = poll == "success" and (mode == "config-plan" or status == "success")
                    with self.subTest(mode=mode, poll=poll, status=status):
                        self.assert_exit(self.run_step("deploy", "Require successful deploy", MODE=mode,
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
jq --argjson start "$start" '.events |= map(select(.timestamp >= $start))' "$STATE/page-$index.json"
''')

    def record_sleeps(self):
        self.tool("sleep", '''
printf '%s:%s\\n' "$1" "$(jq -r . "$STATE/index")" >> "$STATE/sleeps"
''')

    def assert_log_messages(self, result, expected):
        messages = []
        stop_token = None
        for line in result.stdout.splitlines():
            if stop_token is None:
                self.assertRegex(line, r"^::stop-commands::[0-9a-f]{32}$")
                stop_token = line.removeprefix("::stop-commands::")
            elif line == f"::{stop_token}::":
                stop_token = None
            else:
                messages.append(line)
        self.assertIsNone(stop_token, "Log output did not restore workflow commands")
        self.assertEqual(messages, expected)

    @staticmethod
    def event(event_id, timestamp, message, stream="stdout"):
        return {"eventId": event_id, "timestamp": timestamp, "message": message,
                "ingestionTime": timestamp, "logStreamName": f"test-command/{stream}"}

    def test_paginated_logs_sorted_and_deduplicated_before_sleep(self):
        early = self.event("early", 1000001, "earliest timestamp")
        first = self.event("first", 1000002, "first page")
        tie = self.event("aaa", 1000002, "same timestamp, earlier ID")
        marker = self.event("exit", 1000003, '{"deployExit": 0}')
        self.pages([
            {"events": [first], "nextToken": "page-two"},
            {"events": [], "nextToken": "page-three"},
            {"events": [marker, first, tie, early, early]},
            {"events": [early, first, tie, marker]},
        ])
        self.record_sleeps()
        result = self.run_step("deploy", "Follow command logs")
        self.assert_exit(result, 0)
        self.assert_log_messages(result, [early["message"], tie["message"], first["message"], marker["message"]])
        self.assertEqual((self.root / "requests").read_text().splitlines(),
                         ["1000000:", "1000000:page-two", "1000000:page-three", "1000000:"])
        self.assertEqual((self.root / "sleeps").read_text().splitlines(), ["10:3"])

    def test_late_event_with_earlier_timestamp_printed_once(self):
        first = self.event("first", 1000020, "new workers ready")
        late = self.event("late", 1000010, '{"phase": "old_workers_drained"}', stream="stderr")
        late["ingestionTime"] = 1000030
        marker = self.event("exit", 1000040, '{"deployExit": 0}')
        self.pages([
            {"events": [first]},
            {"events": [late, first]},
            {"events": [late, first, marker]},
            {"events": [late, first, marker]},
        ])
        self.record_sleeps()
        result = self.run_step("deploy", "Follow command logs")
        self.assert_exit(result, 0)
        self.assert_log_messages(result, [first["message"], late["message"], marker["message"]])
        self.assertEqual((self.root / "requests").read_text().splitlines(), ["1000000:"] * 4)
        self.assertEqual((self.root / "sleeps").read_text().splitlines(), ["15:1", "15:2", "10:3"])

    def test_final_drain_reads_all_pages_and_prints_unseen_events(self):
        marker = self.event("exit", 1000030, '{"deployExit": 0}')
        tail = self.event("tail", 1000040, "another page after the marker")
        late = self.event("late", 1000010, '{"phase": "old_workers_drained"}')
        stderr = self.event("stderr", 1000020, "::error::late stderr", stream="stderr")
        self.pages([
            {"events": [marker], "nextToken": "after-marker"},
            {"events": [tail]},
            {"events": [marker, tail, stderr], "nextToken": "drain-two"},
            {"events": [], "nextToken": "drain-three"},
            {"events": [late, stderr]},
        ])
        self.record_sleeps()
        result = self.run_step("deploy", "Follow command logs")
        self.assert_exit(result, 0)
        self.assert_log_messages(result, [marker["message"], tail["message"], late["message"], stderr["message"]])
        self.assertEqual((self.root / "requests").read_text().splitlines(),
                         ["1000000:", "1000000:after-marker", "1000000:",
                          "1000000:drain-two", "1000000:drain-three"])
        self.assertEqual((self.root / "sleeps").read_text().splitlines(), ["10:2"])

    def test_nonzero_exit_marker_fails(self):
        marker = self.event("exit", 1000020, '{"deployExit": 7}')
        late = self.event("late", 1000010, "late failure details", stream="stderr")
        self.pages([{"events": [marker]}, {"events": [late, marker]}])
        self.record_sleeps()
        result = self.run_step("deploy", "Follow command logs")
        self.assert_exit(result, 7)
        self.assertIn("SSM deploy failed", result.stderr)
        self.assert_log_messages(result, [marker["message"], late["message"]])
        self.assertEqual((self.root / "requests").read_text().splitlines(), ["1000000:"] * 2)
        self.assertEqual((self.root / "sleeps").read_text().splitlines(), ["10:1"])

    def test_final_drain_read_error_fails(self):
        self.pages([{"events": [self.event("exit", 1000001, '{"deployExit": 0}')]}])
        self.record_sleeps()
        result = self.run_step("deploy", "Follow command logs")
        self.assert_exit(result, 1)
        self.assertIn("Could not read command logs", result.stderr)
        self.assertEqual((self.root / "requests").read_text().splitlines(), ["1000000:"] * 2)
        self.assertEqual((self.root / "sleeps").read_text().splitlines(), ["10:1"])

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

    def test_longer_host_timeout_allows_late_success(self):
        marker = self.event("exit", 4900000, '{"deployExit": 0}')
        self.pages([{"events": [marker]}, {"events": [marker]}])
        self.record_sleeps()
        result = self.run_step("deploy", "Follow command logs", HOST_TIMEOUT_SECONDS="5700", NOW="4900")
        self.assert_exit(result, 0)
        self.assert_log_messages(result, [marker["message"]])

    def test_custom_host_timeout_expires_at_deadline(self):
        for seconds in (600, 601, 5700, 6600):
            with self.subTest(seconds=seconds):
                result = self.run_step("deploy", "Follow command logs",
                                       HOST_TIMEOUT_SECONDS=str(seconds), NOW=str(1000 + seconds))
                self.assert_exit(result, 1)
                self.assertIn(f"No deployExit marker within {seconds} seconds", result.stderr)

    def test_pagination_respects_custom_host_timeout(self):
        self.pages([{"events": [], "nextToken": "another-page"}])
        self.tool("date", '''
if [[ -f "$STATE/index" ]]; then echo 1601; else echo 1000; fi
''')
        result = self.run_step("deploy", "Follow command logs", HOST_TIMEOUT_SECONDS="601")
        self.assert_exit(result, 1)
        self.assertIn("No deployExit marker within 601 seconds", result.stderr)
        self.assertEqual((self.root / "requests").read_text().splitlines(), ["1000000:"])


if __name__ == "__main__":
    unittest.main()
