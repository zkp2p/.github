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

Runtime symlinks are resolved before inspection or replacement. The target must
be a regular file owned by root or the actual Worker's owner, inside that runner's
installation or at `/opt/runner-cache/externals/node24/bin/node`. Hosted x64
images link `externals` to that cache; ARM64 images keep it inside the installation.
The replacement is staged beside the resolved target, retaining its owner and mode.

It probes the native Brotli decoder first. Already-fixed native decoders on any
Ubuntu release, including 26.04, skip the build and installation. The probe
requires `libbrotlidec.so.1` and `_ERROR_FORMAT_BLOCK_SWITCH`; an unsupported
SONAME fails closed. An unfixed decoder can only be rebuilt on Ubuntu 24.04 with
the inspected Brotli 1.1.0 ABI used by curl. Other releases fail closed with the
Ubuntu release and decoder version.

The unchanged checksum-pinned builder produces paired Brotli 1.1.1 runtime
and development packages, runs all 14 included upstream tests, and retains
shared/static ABI symbols and existing development files. Only those two
packages are installed; no package index or unrelated package is upgraded.
Original package inventories, full build/install logs and receipts remain private
in RUNNER_TEMP. Downloads never request automatic content decompression.

Ubuntu x64 and arm64 hosted runners are supported subject to those native decoder
checks; other hosts and container jobs fail closed. System Node, application
dependencies and permission policy are unchanged. Select an explicit fixed Node
version with setup-node for job tools.
This is a per-job measure while hosted images roll out the fix. Remove the step
after both provider bundled and native decoders are independently verified fixed.

The `Test patch-node24` workflow runs the local action on GitHub-hosted
`ubuntu-24.04` and `ubuntu-26.04` runners. Run the isolated release-gate tests
locally with `python3 scripts/test-patch-native-brotli.py`.

Sources: [Node security release](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases)
and [official archive checksums](https://nodejs.org/dist/v24.22.1/SHASUMS256.txt).

## Public repository image builds

Use `.github/workflows/build-deploy-image-public.yml` for public repositories
such as `zkp2p/peer-cli` that cannot call the reusable build workflow in the
private `zkp2p/peer-infra` repository. It builds the caller's `github.sha` on
`ubuntu-24.04` and uploads an image artifact using AWS OIDC. The image is not
built on `ubuntu-latest`: Ubuntu 26.04's Docker reports a manifest digest as the
image ID, which the deploy hosts' classic image store never assigns. Only caller events
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

A single `deploy` job validates inputs, checks out and resolves the environment
map, waits for required CI, then deploys. It uses `inputs.runner` throughout,
so ephemeral runners are acquired only once. Callers' failed-job names are now
`<caller job> / deploy`, including validation and CI failures. The peer-infra
config-cd reconciler is being updated separately to recognize this job name.

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
| `mode` | Required: `image`, `parent`, `config` or `config-plan`. `parent` installs an attestation parent bundle and requires `artifact-kind: eif`. |
| `artifact-kind` | Optional string, default `docker`; accepts `docker` or `eif`. Config modes require `docker`. EIF requires the caller repository variable `SSM_DEPLOY_EIF_ENABLED` to equal `true`. |
| `artifact-key` | Required string; S3 artifact key in image mode. In parent mode it must be exactly `zkp2p/attestation-service/parent/<manifest-sha256>/parent-bundle.tar.gz` for staging, or `zkp2p/attestation-service/prod/parent/<manifest-sha256>/parent-bundle.tar.gz` for prod. |
| `manifest-sha256` | Required string; 64 lowercase hex characters in image and parent modes. |
| `image-id` | Required string; in image mode, `sha256:` followed by 64 lowercase hex characters for Docker, or `pcr0:` followed by 96 lowercase hex characters for EIF. In parent mode, `parent:` followed by the 64 lowercase hex parent source fingerprint. |
| `source-sha` | Required string; full lowercase commit SHA of the artifact, supplied as `github.sha`. |
| `allow-key-removal` | Boolean, default `false`; sent to SSM as `true` or `false`. EIF requires `false`. |
| `environments-file` | Default `deploy/environments.yml`, relative to the caller repository. |
| `ci-workflow` | Default `ci.yml`. Must support dispatch on the release ref if no run exists. |
| `ci-tree-reuse` | Boolean, default `false`. Allow a successful run of `ci-workflow` for an identical Git tree to satisfy the CI gate. |
| `host-timeout-seconds` | Optional number, default `3900`; integer from `600` to `6600` inclusive. Maximum time to follow the host command, in seconds. |
| `runner` | Default `ubuntu-latest`; a Linux runner with Bash, jq, gh, AWS CLI, OpenSSL, GNU timeout and mikefarah/yq v4 (or Docker when yq is absent). |

The host timeout is validated before checkout or CI gating. The follow step timeout
is `ceil(host-timeout-seconds / 60) + 1` minutes (66 minutes by default, or
96 minutes for `host-timeout-seconds: 5700`). The single job has a static
190-minute bound, covering environment resolution, the CI gate and the maximum
110-minute host follow plus the existing margins. The CI wait has its own
50-minute step timeout and retains its 45-minute polling cap.

The maximum is 6600 seconds because the deploy role's AWS credentials last 7200
seconds, also the module role's maximum session duration. The role is assumed
only after CI passes or is skipped. This leaves 600 seconds of headroom for
delivery and the steps before log following; longer follows could outlast the
credentials and lose CloudWatch access.

Environment resolution uses the runner's preinstalled mikefarah/yq, verified by
`yq --version`, and falls back to `mikefarah/yq:4.44.3` in Docker only when yq is
missing. An incompatible installed yq fails resolution.

Image mode requires all four artifact inputs to be real values. Both config modes
force **all four**, including `source-sha`, to `none` before sending the command,
regardless of caller values. GitHub deployment records and notification metadata
still identify the caller commit. `config-plan` delegates the dry run to the host
document; `allow-key-removal` is enforced by the host bridge.

### Parent mode (attestation only)

`mode: parent` installs a signed attestation parent bundle (cred-server,
vsock-tls-proxy, nginx vhost and upstreams, parent units) through the same SSM
document as EIF deploys. It requires `artifact-kind: eif`, the
`SSM_DEPLOY_EIF_ENABLED=true` opt-in, `allow-key-removal: false`, an `image-id`
of `parent:<64 hex>` and the exact environment-scoped bundle key shown above.
The CI gate runs as it does for image mode. A parent bundle key is rejected in
image mode, and an EIF key is rejected in parent mode. The host verifies the
bundle's KMS signature against its pinned public key; this workflow only
forwards the request.

### EIF artifacts (dormant by default)

Existing callers omit `artifact-kind` and keep the Docker identity contract and
the same SSM parameter JSON. Kind and identity are validated before checkout,
CI dispatch, role assumption or deployment records. Unknown kinds, mixed identity
prefixes, uppercase/nonhex/wrong-length digests, and EIF with either config mode
fail closed. The manifest digest and source SHA retain their strict lowercase
64- and 40-character validation.

EIF calls fail with an explicit disabled message unless an operator sets the
**caller repository** variable `SSM_DEPLOY_EIF_ENABLED` to exactly `true`. Leave it
unset until staging prerequisites, trusted host bridge adoption, IAM roles and
the EIF-specific SSM document are ready. Clearing the variable disables future
EIF calls; it does not cancel a command already running. Docker callers do not
depend on this variable. This reusable workflow has only `workflow_call`, adds
no release trigger, and performs no signing.

The attestation caller must use a staging-only environment map, `ci: required`,
the approved fleet runner with no hosted fallback, and a reviewed full commit
pin for this workflow. After the EIF build job publishes its outputs, its deploy
job supplies:

```yaml
with:
  mode: image
  artifact-kind: eif
  artifact-key: ${{ needs.eif.outputs.artifact-key }}
  manifest-sha256: ${{ needs.eif.outputs.manifest-sha256 }}
  image-id: ${{ needs.eif.outputs.image-id }} # pcr0:<96 lowercase hex>
  source-sha: ${{ github.sha }}
  allow-key-removal: false
  host-timeout-seconds: 2400
  runner: ${{ vars.CI_RUNNER }}
```

The final artifact key is
`zkp2p/attestation-service/<runId>/<runAttempt>/attestation-eif.tar.gz`. It contains
only `app.eif`, `receipt.json`, `vsock-routes.json` and `SHA256SUMS`.
`manifest-sha256` hashes the exact sorted `SHA256SUMS` bytes, with two-space
separators and no self-hash; it is not the hash of `receipt.json` or the EIF.
Ciphertext build inputs stay in the build workflow and never pass through this
deploy interface.

SSM still receives `Mode=image` and `ImageId=pcr0:<PCR0>`. There is **no new
`ArtifactKind` SSM parameter**: the selected document's fixed settings and identity
pattern establish EIF behavior. The host verifies the artifact, signature and
measurements. `RunId` and `RunAttempt` retain their existing meaning. The 2400-second
follow duration covers the document's 1800-second execution timeout plus up to
600 seconds delivery; SendCommand's delivery timeout remains 600 seconds.

The caller must subsequently verify the serving v2 receipt at
`deployed/attestation-service/attestation-staging.json` and the exact v1 attempt
receipt at `attempts/attestation-service/attestation-staging/<runId>-<runAttempt>.json`.
Acceptance requires matching run/attempt and artifact identity, `applied` or `noop`,
and successful loopback/public verification. Shared SSM success and its deployment
card alone do not establish this receipt contract. Receipt verification is owned
by the attestation/peer-infra caller integration.

### CI and command following

For image and parent deploys with `ci: required`, the workflow polls CI for the
source commit every 30 seconds for up to 45 minutes. A completed success passes;
otherwise any unfinished run keeps it waiting, completed unsuccessful runs fail,
and no runs triggers one dispatch followed by polling. Config modes and `ci: skip`
bypass this gate.

When only completed unsuccessful runs exist, the newest run gets a runner-loss
grace period if its latest attempt has failed or cancelled jobs but no failed
step anywhere. Attempts 1 and 2 wait until 10 minutes after the run's `updated_at`
to let the spot re-run automation start a new attempt; a running attempt then
uses the normal wait path. A failed step, attempt 3, an expired grace period or
unreadable job evidence fails immediately. The grace does not extend the overall
45-minute cap. Each API call remains bounded to 60 seconds.

Set `ci-tree-reuse: true` in the caller's `with` mapping to reuse CI for the same
Git tree, such as a squash merge whose tree matches a successful PR head. When
the source SHA has no successful run, the gate resolves its tree and checks up
to 100 successful runs of the configured CI workflow for a matching
`head_commit.tree_id`. It checks on the initial poll and after every five further
polls, logging the reused run's URL and head SHA on success. An invalid tree SHA,
API error or missing match falls back to the normal SHA decision. With the input
omitted or `false`, no tree lookups occur.

Enable reuse only when identical trees provide equivalent CI evidence across
the caller's PR and release contexts; branch/event-specific checks or external
inputs can differ even when the source files match.

The deploy role must support a 7,200-second OIDC session and permit sending only
the intended document to its target instance, plus reading its CloudWatch log
group. The workflow never queries SSM command status. Its document must accept
`Mode`, `ArtifactKey`, `ManifestSha256`, `ImageId`, `SourceSha`,
`AllowKeyRemoval`, `RunId` and `RunAttempt`, write value-safe logs under the
command ID stream prefix, and emit `{"deployExit": N}` when finished. `RunId` and
`RunAttempt` are the caller's `github.run_id` and `github.run_attempt`, so hosts
can name per-attempt receipts. SSM rejects a command that sends a parameter its
document does not define: pin a commit that sends them only after the caller's
documents define both. Zero means success; a nonzero marker fails
the deployment. Logs are polled every 15 seconds from the fixed command start
time, following every `nextToken`, including through empty pages. Previously
printed event IDs are deduplicated, and each poll's new events are printed in
timestamp and event ID order so late ingestion cannot skip earlier events.
After an exit marker, the poller waits 10 seconds and performs one final full
read before exiting with the marker's status.
No events after 10 minutes or no exit marker within `host-timeout-seconds`
(65 minutes by default) fails the workflow. A workflow timeout does not cancel
the host command; the document must enforce its own execution timeout.

Deployment status is recorded as `in_progress`, then `success` or `failure`, with
a link to the workflow run. Hosted SSM jobs use the runtime patch described above
before JavaScript actions; self-hosted runner maintenance belongs to the caller.

Run the local checks from the repository root (Docker must be running; the shell
step harnesses also require Python 3, jq, OpenSSL and `sha256sum`):

```bash
bash scripts/test-ci-gate.sh
python3 scripts/test-ssm-deploy.py
python3 scripts/test-build-deploy-image-public.py
actionlint .github/workflows/*.yml
```

The test extracts the workflow's CI decision, runner-loss and tree-reuse jq
filters with `mikefarah/yq:4.44.3` and checks success, failure with an unfinished run, failure
only, empty results and failed-step evidence across job pages. The shell step
harness checks host timeout validation and rounding, yq selection, log pagination
and deduplication, polling failures/timeouts, Slack failures, and the final success
gate with isolated fake external commands.
It also checks Docker/EIF identity validation, dormant EIF gating, unchanged SSM
parameters and config normalization, run/attempt forwarding, delivery timeout,
and exact-SHA CI polling with a single dispatch through a fake `gh` command.
Runner-loss checks cover grace expiry, real failed steps, the latest attempt,
the re-run limit, API errors and the overall CI deadline.
Tree-reuse checks cover matching and mismatched trees, malformed data, API errors,
the opt-in default, lookup cadence and the overall deadline.
These checks do not exercise a live deployment or call AWS/GitHub APIs.

The public build harness checks the event guard, fixed hosted runner, caller
checkout, manifest format and outputs, archive contents, run-attempt-specific
S3 keys, and failed uploads using local Docker/AWS stubs. It does not build a
real application image or assume an AWS role.
