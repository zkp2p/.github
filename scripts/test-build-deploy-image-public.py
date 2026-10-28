#!/usr/bin/env python3
"""Check the public build workflow with isolated tools; never contact AWS."""

import gzip
import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tarfile
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW_PATH = ROOT / ".github/workflows/build-deploy-image-public.yml"
assert WORKFLOW_PATH.is_file(), "Public build workflow is missing"
WORKFLOW = json.loads(subprocess.check_output([
    "docker", "run", "--rm", "-v", f"{ROOT}:/w:ro", "-w", "/w",
    "mikefarah/yq:4.44.3", "-o=json", str(WORKFLOW_PATH.relative_to(ROOT)),
], text=True))
JOB = WORKFLOW["jobs"]["image"]
FILES = ("image.tar.gz", "image-id.txt", "source-sha.txt", "SHA256SUMS")


def step(step_id):
    return next(item for item in JOB["steps"] if item.get("id") == step_id)


class PublicBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        # AWS and Docker are only available as local stubs in the shell harness.
        for name in ("bash", "tar", "gzip", "sha256sum", "find", "sed", "sort",
                     "xargs", "awk", "cat", "mkdir", "rm"):
            (self.bin / name).symlink_to(shutil.which(name))
        self.env = {
            "PATH": str(self.bin), "HOME": str(self.root), "STATE": str(self.root),
            # Match Linux archives when the harness runs with macOS tar.
            "COPYFILE_DISABLE": "1",
            "RUNNER_TEMP": str(self.root), "GITHUB_OUTPUT": str(self.root / "outputs"),
            "GITHUB_REPOSITORY": "example/public-app", "GITHUB_RUN_ID": "123456",
            "GITHUB_RUN_ATTEMPT": "1", "ARTIFACT_NAME": "deploy-image",
            "IMAGE_NAME": "public-app", "SOURCE_SHA": "a" * 40,
        }

    def tool(self, name, body):
        executable = self.bin / name
        executable.write_text("#!/bin/bash\nset -euo pipefail\n" + body + "\n")
        executable.chmod(0o755)

    def run_script(self, script, **env):
        return subprocess.run(
            [str(self.bin / "bash"), "--noprofile", "--norc", "-e", "-o", "pipefail",
             "-c", script], cwd=self.root, env={**self.env, **env},
            text=True, capture_output=True, timeout=5,
        )

    def test_reusable_interface_and_hosted_runner_only(self):
        self.assertEqual(set(WORKFLOW["on"]), {"workflow_call"})
        inputs = WORKFLOW["on"]["workflow_call"]["inputs"]
        self.assertEqual(set(inputs), {
            "dockerfile", "context", "image-name", "artifact-name", "build-args",
            "artifact-role-arn",
        })
        for name in ("dockerfile", "image-name", "artifact-name", "artifact-role-arn"):
            self.assertIs(inputs[name]["required"], True)
        self.assertEqual(inputs["context"]["default"], ".")
        self.assertEqual(inputs["build-args"]["default"], "")
        for job in WORKFLOW["jobs"].values():
            self.assertEqual(job["runs-on"], "ubuntu-latest")
        self.assertEqual(JOB["permissions"], {"contents": "read", "id-token": "write"})

    def test_only_push_and_workflow_dispatch_allowed(self):
        guard = JOB["steps"][0]
        self.assertEqual(guard["if"],
                         "github.event_name != 'push' && github.event_name != 'workflow_dispatch'")
        self.assertFalse(guard.get("continue-on-error", False))
        result = self.run_script(guard["run"])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        # Every subsequent step must retain the default success() gate.
        for item in JOB["steps"][1:]:
            self.assertNotIn("if", item)

    def test_checks_out_caller_commit_without_credentials(self):
        checkout = next(item for item in JOB["steps"]
                        if item.get("uses", "").startswith("actions/checkout@"))
        self.assertEqual(checkout["with"]["ref"], "${{ github.sha }}")
        self.assertIs(checkout["with"]["persist-credentials"], False)
        self.assertEqual(JOB["env"]["SOURCE_SHA"], "${{ github.sha }}")

    def test_artifact_manifest_format_and_outputs(self):
        image_id = "sha256:" + "b" * 64
        self.tool("docker", '''
case "$1" in
  save) printf 'saved image bytes' ;;
  image) printf '%s\\n' "''' + image_id + '''" ;;
  *) exit 99 ;;
esac
''')
        result = self.run_script(step("package")["run"])
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        artifact = self.root / "artifact"
        self.assertEqual({item.name for item in artifact.iterdir()}, set(FILES))
        self.assertEqual(gzip.decompress((artifact / "image.tar.gz").read_bytes()), b"saved image bytes")
        self.assertEqual((artifact / "image-id.txt").read_text(), image_id + "\n")
        self.assertEqual((artifact / "source-sha.txt").read_text(), self.env["SOURCE_SHA"] + "\n")
        manifest = "".join(
            f"{hashlib.sha256((artifact / name).read_bytes()).hexdigest()}  {name}\n"
            for name in sorted(set(FILES) - {"SHA256SUMS"})
        )
        self.assertEqual((artifact / "SHA256SUMS").read_text(), manifest)
        self.assertEqual((self.root / "outputs").read_text().splitlines(), [
            "manifest-sha256=" + hashlib.sha256(manifest.encode()).hexdigest(),
            "image-id=" + image_id,
        ])
        for name, step_id in (("artifact-key", "upload-s3"),
                              ("manifest-sha256", "package"), ("image-id", "package")):
            self.assertEqual(JOB["outputs"][name], "${{ steps." + step_id + ".outputs." + name + " }}")
            self.assertEqual(WORKFLOW["on"]["workflow_call"]["outputs"][name]["value"],
                             "${{ jobs.image.outputs." + name + " }}")

    def test_artifact_key_and_archive_contents(self):
        artifact = self.root / "artifact"
        artifact.mkdir()
        for name in FILES:
            (artifact / name).write_text(name)
        self.tool("aws", 'printf "%s\\n" "$@" > "$STATE/aws-args"')
        upload = step("upload-s3")
        self.assertEqual(upload["env"]["ARTIFACT_NAME"], "${{ inputs.artifact-name }}")
        for attempt in ("1", "2"):
            with self.subTest(attempt=attempt):
                (self.root / "outputs").unlink(missing_ok=True)
                result = self.run_script(upload["run"], GITHUB_RUN_ATTEMPT=attempt)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                key = f"example/public-app/123456/{attempt}/deploy-image.tar.gz"
                self.assertEqual((self.root / "outputs").read_text(), f"artifact-key={key}\n")
                args = (self.root / "aws-args").read_text().splitlines()
                self.assertEqual(args[:2], ["s3", "cp"])
                self.assertEqual(args[3:], [
                    f"s3://zkp2p-gha-artifacts-411399509833/{key}", "--only-show-errors",
                ])
                with tarfile.open(args[2], "r:gz") as archive:
                    self.assertEqual(archive.getnames(), list(FILES))
                    for name in FILES:
                        self.assertEqual(archive.extractfile(name).read(), name.encode())

    def test_failed_upload_does_not_publish_artifact_key(self):
        artifact = self.root / "artifact"
        artifact.mkdir()
        for name in FILES:
            (artifact / name).write_text(name)
        self.tool("aws", "exit 1")
        result = self.run_script(step("upload-s3")["run"])
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertFalse((self.root / "outputs").exists())


if __name__ == "__main__":
    unittest.main()
