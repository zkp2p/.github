# Sekko release notices (draft, disabled)

This opt-in hook reports an already-successful **Pay API staging image deploy**.
It does not deploy, create a scope, run Sekko, call a model, or establish campaign
authority. Existing callers omit the inputs and keep the existing deployment
behavior. No Pay or Sekko caller/configuration/key is changed by this patch.

## Interface and distribution

The reusable SSM workflow accepts `sekko-release-notice` (boolean, default
`false`), `sekko-notice-config` (JSON string, default empty), and the optional
`sekko-release-key` secret. The secret must contain at least 32 random bytes,
encoded as even-length hex (this sender accepts 32–4096 bytes). It is exposed
only through the notice step's `SEKKO_RELEASE_KEY` environment variable. Use a
purpose-specific release signing key registered as `release-ci` in Sekko; do
not reuse the scope-signing key or put a key in repository variables/config.

`actions/sekko-release-notice` is a shell/Python standard-library composite
**in this public repository**, referenced by a full commit pin. The deploy job
needs no checkout, no extra `contents` permission, no private Sekko import or
checkout, no new cross-repository token and no `secrets: inherit`. The runner
needs Python 3 and GNU `timeout` in addition to the existing deploy tools.
Before any caller adoption, the final reviewed helper commit must be reachable
on this repository's `main`, and the reusable workflow must be repinned to that
exact immutable helper SHA. Verify both against `main` after the authorized
merge; publishing a feature branch or draft PR is insufficient. If merging
rewrites the helper commit, repin and review the resulting workflow commit before
adoption. Main reachability and the final repin remain activation gates until
merge is authorized and complete; this draft does not fulfill those gates.

The exact config fields are:

```json
{
  "inbox_url": "https://<reviewed-inbox-host>/v1/releases",
  "scope_id": "<current-operator-signed-scope-id>",
  "target_id": "<target-in-that-scope>",
  "surface": "<surface-in-that-scope>",
  "origin": "https://api-staging.pay.peer.xyz",
  "description": "Pay API staging image release"
}
```

Placeholders above are intentionally invalid configuration. The inbox must be
a canonical HTTPS DNS hostname on port 443, with the exact `/v1/releases` path
and no credentials, explicit port, query or fragment. Config accepts no extra
fields. Scope/target/surface must be consumer-valid identifiers; description is
1–1000 characters. Missing/invalid config or key prevents **all** network calls.

The staging hostname is corroborated by current Pay source at
`apps/checkout/.env.example` and `apps/demo/.env.example` (main
`5dffbe5e1b06296550bae55d1c514023771af9b9`). This is a hosted-origin candidate,
not a signed scope or campaign approval. Keep the hook off until an operator
has reviewed the inbox, current scope and matching target/surface/origin,
registered the release key with the consumer, and approved staging execution.
The sender cannot mint or inspect a scope; Sekko remains the authority that
validates its signature, currency and target limits.

## Success, availability and delivery

The workflow invokes the helper only after `Require successful deploy` passes:
command log polling and GitHub deployment-status recording must both succeed.
Both workflow and helper enforce `zkp2p/pay`, branch `releases/staging`, resolved
environment `staging`, map `deploy/environments-pay.yml`, and mode `image`.
Support, preprod, prod and config deployments cannot emit a notice through this
hook. SSM command ID and GitHub deployment ID are checked and recorded as safe
evidence; they are not added to the consumer payload.

Before signing, the helper independently fetches exactly
`https://api-staging.pay.peer.xyz/readyz`. It requires HTTP 200, `success: true`,
integer `statusCode: 200`, `responseObject.ok: true`, the exact artifact
`source-sha` as `responseObject.commit`, and exactly healthy database/Redis
dependencies. It never follows redirects, uses ambient proxy settings, or
accepts a body over 65,536 bytes. TLS certificate/hostname verification uses
the system trust store. A failed or mismatched public probe records a gap and
sends no notice. This check proves that public ingress serves the expected
commit at that moment; it does not prove full payment-flow health.

The payload is the consumer's release-v1 contract: event, scope, target,
surface, staging environment, origin, exact source SHA, integer release/expiry
times, `availability: available`, description, and
`change_type: release`. Event identity is
`pay:staging:<run_id>:<run_attempt>:<source-sha>`. The sender signs
`b'release-v1\x00' + canonical(payload)` using HMAC-SHA256 and emits exactly
`key_id`, `purpose`, `payload`, `signature`. Canonical JSON is ASCII-escaped,
sorted, compact and rejects non-finite numbers, matching Sekko's contract.

The sender backdates `released_at` by an explicit five-second clock-skew
allowance: `floor(sender_now) - 5`. Expiry is that backdated release time plus
3600 seconds, so the allowance does not extend the one-hour TTL. This permits
a receiver one to five seconds behind the sender to accept the timestamp; a
larger skew may still fail. Consumer timestamp/scope validation is unchanged.

The envelope bytes and timestamps are constructed once per invocation. Up to
three POST attempts reuse those exact bytes, with 2s and 4s delays. Only
transport failures and HTTP 408/429/502/503/504 retry. HTTP 202 with `status: queued`
or HTTP 200 with `status: duplicate` succeeds. Redirects, other 4xx/5xx, malformed
acknowledgements, wrong acknowledgement statuses, or exhausted retries leave
a delivery gap. Each request has a 10s socket timeout and bounded response;
GNU timeout terminates the helper after 100s (5s kill grace), with a two-minute
workflow step cap. There is one readiness GET and no readiness retry.

Notice steps use `continue-on-error` and do not rewrite the GitHub deployment
result, rerun SSM, roll back, or change the final deployment success gate. The
step summary explicitly reports a delivery gap. Two separate failed-delivery
artifacts are retained for seven days when present:

- `sekko-release-notice-gap-<run>-<attempt>` contains the safe report: fixed reason
  codes, attempts, HTTP status and safe event / command / deployment references.
- `sekko-release-payload-pending-<run>-<attempt>` contains only the exact canonical
  **unsigned public payload** saved before the first signing/send. Its source
  JSON file is created exclusively with mode `0600`. It contains no inbox URL,
  configuration document, key, signature or signed envelope.

Before every invocation, only this task's exact stale pending filename is
removed. Invalid config, a failed readiness check, or failed persistence cannot
leave a previous payload available for upload. Failure to save the payload
prevents signing and sending. The safe report is initialized before network
I/O, so interrupted deliveries retain an `interrupted` report when the process
began; a workflow fallback summary also covers helper startup failure. No key,
config, response body, envelope, signature or external exception text is logged
or uploaded. Cancellation/runner loss can still prevent artifact upload; a
successful deployment is never evidence that a notice arrived. Inspect the
notice step/report and consumer acknowledgement.

Do not rerun a deployment to resend a notice. For a separate reviewed
notice-only recovery **within the original expiry**, retrieve the unsigned
pending payload, set its downloaded file mode to `0600`, and re-sign that identical
payload using the registered release
key and the same `release-v1` canonicalization. Keep every field, event ID and
release/expiry timestamp unchanged. Deterministic HMAC reproduces the same
signature with the same key. A lost acknowledgement may mean the inbox already
accepted it; resending the same payload can return `duplicate`. Do not substitute
current config fields or regenerate a payload from the current clock.

Before any **expired** reconciliation, inspect the inbox/event dedupe record to
determine whether the original event was accepted or queued. Do not extend the
expired payload or reuse its event ID with fresh timestamps; Sekko rejects
collisions. Any new notice requires a separately reviewed recovery decision,
current scope/availability proof and a distinct event identity. This patch
provides the recovery payload and evidence only, with no replay job, automatic
expired reconciliation or authority to run a campaign.

## Future Pay caller change (proposal only)

Only after the final helper pin is reachable on `main`, the reusable workflow
is repinned/reviewed against it, and an approved config/key exists, change
**only** `jobs.pay-api` in Pay's
`.github/workflows/deploy.yml`. Leave `jobs.support` at its existing pin and
without notice inputs/secrets. Retain every existing Pay input/permission.
Replace the Pay API workflow pin with the full reviewed workflow commit and add:

```yaml
jobs:
  pay-api:
    uses: zkp2p/.github/.github/workflows/ssm-deploy.yml@<reviewed-full-workflow-commit>
    with:
      # All existing Pay API inputs remain.
      sekko-release-notice: ${{ github.ref_name == 'releases/staging' && vars.SEKKO_RELEASE_NOTICES_ENABLED == 'true' }}
      sekko-notice-config: ${{ vars.SEKKO_PAY_STAGING_NOTICE_CONFIG }}
    secrets:
      sekko-release-key: ${{ secrets.SEKKO_RELEASE_KEY }}
```

Keep `SEKKO_RELEASE_NOTICES_ENABLED` unset/false until the approved configuration
and key registration exist and are reviewed, and the final helper-main/pin gates
above have been verified after an authorized merge. Clearing it disables future
notices. It does not cancel an invocation already running. The source SHA
must continue to come from `needs.pay-api-image.outputs.source-sha`.

## CI and local verification

The PR-native `Sekko release notice checks` workflow, job `Native notice and
deploy gates`, runs these checks on a fixed Ubuntu 24.04 runner with read-only
`contents`, pinned checkout and no deploy/publish steps or supplied credentials:
notice tests, the actual existing Docker/yq deploy harness, and actionlint 1.7.12
from its versioned official release archive verified against a fixed SHA-256.
Lint covers the two affected workflows, `ssm-deploy.yml` and
`test-sekko-release-notice.yml`, with all actionlint rules enabled. Version
1.7.12 does not recognize the unrelated baseline `test-patch-node24.yml` matrix
label `ubuntu-26.04`; that workflow is outside this change's lint scope. No
baseline workflow, runner label or generic ignore rule is changed.
It does not replace the Docker/yq bootstrap with a parser shim. The actionlint
Linux x64 digest is `8aca8db96f1b94770f1b0d72b6dddcb1ebb8123cb3712530b08cc387b349a3d8`.
No native CI result is claimed until that PR check has actually run.

```bash
python3 scripts/test-sekko-release-notice.py
python3 scripts/test-ssm-deploy.py
actionlint .github/workflows/ssm-deploy.yml \
  .github/workflows/test-sekko-release-notice.yml
```

The notice harness uses only local fixtures and injected transports, including
canonical signing, opt-out and every eligibility/config gate, mismatched
readiness/SHA, the five-second timestamp/signature fixture, transport and HTTP
retry classes (including 502/504), duplicate acknowledgement,
redirect rejection, constant retry bytes, bounded response reads, verified TLS,
lost acknowledgements, unsigned-payload persistence before signing, stale cleanup,
and safe gap evidence. No live inbox/readiness/deployment/provider/model is
called. The existing deploy harness additionally requires Docker and its
pinned yq image; missing tools are verification gaps, not live-test permission.
