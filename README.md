# Peer

This repo holds the GitHub organization profile for Peer, the non-custodial peer-to-peer fiat-to-crypto marketplace built on the ZKP2P protocol.

The org landing page content lives in [profile/README.md](profile/README.md).

Learn more at [peer.xyz](https://peer.xyz) and [docs.peer.xyz](https://docs.peer.xyz).

## Hosted action runtime security

`actions/patch-node24` patches the actual GitHub-hosted runner's bundled Node 24
for CVE-2026-111497 before main JavaScript steps execute. Put it first in each
Linux host job, before checkout, and pin the reviewed commit. First confirm that
none of the job's actions declares a JavaScript `pre` hook: the runner starts
those hooks before ordinary steps, so this bootstrap cannot protect them.

```yaml
steps:
  - uses: zkp2p/.github/actions/patch-node24@<full-reviewed-commit-sha>
  - uses: actions/checkout@<full-reviewed-commit-sha>
```

The composite uses shell steps without JavaScript. It derives the binary path from the
running Worker, downloads checksum-pinned official Node 24.22.1, and atomically
replaces that bundled binary using the hosted runner's existing sudo
access. An already-fixed decoder is retained. It selects Node 24 for subsequent
legacy JavaScript actions through GitHub's supported migration setting.

On Ubuntu 24.04 it also patches the inspected native Brotli 1.1.0 ABI used by
curl. The unchanged checksum-pinned builder produces paired Brotli 1.1.1 runtime
and development packages, runs all 14 included upstream tests, and retains
shared/static ABI symbols and existing development files. Only those two
packages are installed; no package index or unrelated package is upgraded.
Already-fixed native decoders skip the build and installation. Original package
inventories, full build/install logs and receipts remain private in RUNNER_TEMP.
Downloads never request automatic content decompression.

Ubuntu 24.04 x64 and arm64 are supported; other hosts and container jobs fail
closed. System Node, application dependencies and permission policy are
unchanged. Select an explicit fixed Node version with setup-node for job tools.
This is a per-job measure while hosted images roll out the fix. Remove the step
after both provider bundled and native decoders are independently verified fixed.

Sources: [Node security release](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases)
and [official archive checksums](https://nodejs.org/dist/v24.22.1/SHASUMS256.txt).

## Public repository image builds

Use `.github/workflows/build-deploy-image-public.yml` for public repositories
such as `zkp2p/peer-cli` that cannot call the reusable build workflow in the
private `zkp2p/peer-infra` repository. It builds the caller's `github.sha` on
`ubuntu-latest` and uploads an image artifact using AWS OIDC. Only caller events
`push` and `workflow_dispatch` are accepted; other events fail before checkout.
The runner is fixed and cannot be overridden by the caller.

Pin the reusable workflow to a reviewed commit and grant the calling job
`contents: read` and `id-token: write`:

```yaml
jobs:
  image:
    permissions:
      contents: read
      id-token: write
    uses: zkp2p/.github/.github/workflows/build-deploy-image-public.yml@<full-reviewed-commit-sha>
    with:
      dockerfile: Dockerfile
      image-name: example-service
      artifact-name: deploy-image
      artifact-role-arn: ${{ vars.ARTIFACT_ROLE_ARN }}
      build-args: |
        NODE_ENV=production
```

| Input | Contract |
| --- | --- |
| `dockerfile` | Required Dockerfile path in the caller checkout. |
| `context` | Docker build context; default `.`. |
| `image-name` | Required image name, tagged with the caller commit SHA. |
| `artifact-name` | Required archive name, without the `.tar.gz` suffix. |
| `build-args` | Newline-separated Docker build arguments; default empty. |
| `artifact-role-arn` | Required caller-provided OIDC artifact writer role. |

The writer role must trust the caller's intended branch-based OIDC subjects and
permit uploads to its repository prefix in `zkp2p-gha-artifacts-411399509833`
in `us-east-1`. The bucket name is the only fixed AWS account-specific value;
the caller supplies the role ARN. This workflow has no smoke test, fleet upload,
or runtime patch step.

The build receives `SOURCE_SHA` and OCI revision/source labels. The archive
contains `image.tar.gz`, `image-id.txt`, `source-sha.txt`, and `SHA256SUMS` using
the private workflow's sorted relative-path manifest format. The workflow
returns `artifact-key`, `manifest-sha256`, and `image-id` for the SSM deploy
workflow below. `artifact-key` is the S3 key
`<github.repository>/<run_id>/<run_attempt>/<artifact-name>.tar.gz`;
`manifest-sha256` hashes `SHA256SUMS`, and `image-id` identifies the built image.
Pass `github.sha` separately as the deploy workflow's `source-sha`.

## Reusable SSM deploy workflow

`.github/workflows/ssm-deploy.yml` deploys through a caller-owned SSM document
and follows its CloudWatch logs. Pin the reusable workflow to a reviewed commit.
All deployment infrastructure identifiers come from the caller's environment
map; the SSM workflow contains no deployment credentials or account configuration.

The caller's `deploy/environments.yml` uses this schema (replace placeholders
in the caller repository):

```yaml
service: example-service
ssmDocument: <deploy-document-name>
logGroup: <deploy-log-group>
region: <aws-region>
environments:
  staging:
    branch: releases/staging
    instanceId: <staging-instance-id>
    roleArn: <staging-role-arn>
    ci: skip
  prod:
    branch: releases/prod
    instanceId: <prod-instance-id>
    roleArn: <prod-role-arn>
    ci: required
```

The map is checked out from the caller at `github.sha`. Exactly one environment
must match `github.ref_name`; missing matches, duplicate matches, missing fields
and unknown CI policies fail closed. GitHub deployment records use this resolved
environment without attaching a GitHub Environment to the job, preserving the
branch-based OIDC subject.

Example caller job, after an `image` job that publishes the three artifact outputs
and skips itself for config modes:

```yaml
permissions:
  contents: read
concurrency:
  group: deploy-${{ github.ref_name }}
  cancel-in-progress: false

jobs:
  deploy:
    needs: image
    if: ${{ !cancelled() && (needs.image.result == 'success' || needs.image.result == 'skipped') }}
    permissions:
      contents: read
      id-token: write
      deployments: write
      actions: write
    uses: zkp2p/.github/.github/workflows/ssm-deploy.yml@<full-reviewed-commit-sha>
    with:
      mode: ${{ inputs.mode || 'image' }}
      artifact-key: ${{ needs.image.outputs.artifact-key || 'none' }}
      manifest-sha256: ${{ needs.image.outputs.manifest-sha256 || 'none' }}
      image-id: ${{ needs.image.outputs.image-id || 'none' }}
      source-sha: ${{ github.sha }}
      allow-key-removal: ${{ inputs.allow-key-removal == true }}
      runner: ${{ vars.CI_RUNNER || 'ubuntu-latest' }}
    secrets:
      slack-webhook-url: ${{ secrets.SLACK_DEPLOY_WEBHOOK_URL }}
```

Omit the `secrets` mapping when notifications are not needed. The optional webhook
receives one line containing repository, environment, mode, short commit, result
and run URL. Notification failures emit a warning and do not affect the result;
the final success gate depends only on log polling and deployment-status recording.

| Input | Contract |
| --- | --- |
| `mode` | Required: `image`, `config` or `config-plan`. |
| `artifact-key` | Required string; S3 artifact key in image mode. |
| `manifest-sha256` | Required string; 64 lowercase hex characters in image mode. |
| `image-id` | Required string; `sha256:` followed by 64 lowercase hex characters in image mode. |
| `source-sha` | Required string; full lowercase commit SHA of the image, supplied as `github.sha`. |
| `allow-key-removal` | Boolean, default `false`; sent to SSM as `true` or `false`. |
| `environments-file` | Default `deploy/environments.yml`, relative to the caller repository. |
| `ci-workflow` | Default `ci.yml`. Must support dispatch on the release ref if no run exists. |
| `runner` | Default `ubuntu-latest`; a Linux runner with Bash, jq, gh, AWS CLI, OpenSSL, GNU timeout and mikefarah/yq v4 (or Docker when yq is absent). |

Environment resolution uses the runner's preinstalled mikefarah/yq, verified by
`yq --version`, and falls back to `mikefarah/yq:4.44.3` in Docker only when yq is
missing. An incompatible installed yq fails resolution.

Image mode requires all four artifact inputs to be real values. Both config modes
force **all four**, including `source-sha`, to `none` before sending the command,
regardless of caller values. GitHub deployment records and notification metadata
still identify the caller commit. `config-plan` delegates the dry run to the host
document; `allow-key-removal` is enforced by the host bridge.

For image deploys with `ci: required`, the workflow polls CI for the image commit
every 30 seconds for up to 45 minutes. A completed success passes; otherwise any
unfinished run keeps it waiting, completed unsuccessful runs fail, and no runs
triggers one dispatch followed by polling. Config modes and `ci: skip` bypass
this gate.

The deploy role must support a 7,200-second OIDC session and permit sending only
the intended document to its target instance, plus reading its CloudWatch log
group. The workflow never queries SSM command status. Its document must accept
`Mode`, `ArtifactKey`, `ManifestSha256`, `ImageId`, `SourceSha` and
`AllowKeyRemoval`, write value-safe logs under the command ID stream prefix, and
emit `{"deployExit": N}` when finished. Zero means success; a nonzero marker fails
the deployment. Logs are polled every 15 seconds from the fixed command start
time, following every `nextToken`, including through empty pages. Previously
printed event IDs are deduplicated, and each poll's new events are printed in
timestamp and event ID order so late ingestion cannot skip earlier events.
After an exit marker, the poller waits 10 seconds and performs one final full
read before exiting with the marker's status.
No events after 10 minutes or no exit marker after
65 minutes fails the workflow. A workflow timeout does not cancel the host
command; the document must enforce its own execution timeout.

Deployment status is recorded as `in_progress`, then `success` or `failure`, with
a link to the workflow run. Hosted SSM jobs use the runtime patch described above
before JavaScript actions; self-hosted runner maintenance belongs to the caller.

Run the local checks from the repository root (Docker must be running; the shell
step harnesses also require Python 3, jq, OpenSSL and `sha256sum`):

```bash
bash scripts/test-ci-gate.sh # 4 passed
python3 scripts/test-ssm-deploy.py
python3 scripts/test-build-deploy-image-public.py
actionlint .github/workflows/*.yml
```

The test extracts the workflow's `decide.env.DECISION_JQ` with
`mikefarah/yq:4.44.3` and checks success, failure with an unfinished run, failure
only, and empty results. The shell step harness checks yq selection, log
pagination and deduplication, polling failures/timeouts, Slack failures, and the
final success gate with isolated fake external commands. These checks do not
exercise a live deployment or call AWS/GitHub APIs.

The public build harness checks the event guard, fixed hosted runner, caller
checkout, manifest format and outputs, archive contents, run-attempt-specific
S3 keys, and failed uploads using local Docker/AWS stubs. It does not build a
real application image or assume an AWS role.
