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
Publish the reviewed helper commit before adopting a reusable-workflow revision
that references it; an unpublished local pin cannot be downloaded by Actions.

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
times (one-hour TTL), `availability: available`, description, and
`change_type: release`. Event identity is
`pay:staging:<run_id>:<run_attempt>:<source-sha>`. The sender signs
`b'release-v1\x00' + canonical(payload)` using HMAC-SHA256 and emits exactly
`key_id`, `purpose`, `payload`, `signature`. Canonical JSON is ASCII-escaped,
sorted, compact and rejects non-finite numbers, matching Sekko's contract.

The envelope bytes and timestamps are constructed once per invocation. Up to
three POST attempts reuse those exact bytes, with 2s and 4s delays. Only
transport failures and HTTP 408/429/503 retry. HTTP 202 with `status: queued`
or HTTP 200 with `status: duplicate` succeeds. Redirects, other 4xx/5xx, malformed
acknowledgements, wrong acknowledgement statuses, or exhausted retries leave
a delivery gap. Each request has a 10s socket timeout and bounded response;
GNU timeout terminates the helper after 100s (5s kill grace), with a two-minute
workflow step cap. There is one readiness GET and no readiness retry.

Notice steps use `continue-on-error` and do not rewrite the GitHub deployment
result, rerun SSM, roll back, or change the final deployment success gate. The
step summary explicitly reports a delivery gap, and the failed-delivery
artifact contains fixed reason codes, attempts, HTTP status and safe event /
command / deployment references only. Its retention is seven days. The report
is initialized before network I/O, so interrupted deliveries retain an
`interrupted` report when the process began; a workflow fallback summary also
covers helper startup failure. No key, config, body, envelope, signature or
external exception text is logged or uploaded. Cancellation/runner loss can
still prevent artifact upload; a successful deployment is never evidence that
a notice arrived. Inspect the notice step/report and consumer acknowledgement.

Do not rerun a deployment to resend a notice. In-process retries are the only
resend mechanism here. Preserve any delivery gap for a separate reviewed
notice-only recovery; this patch provides no automatic replay job or durable
envelope store. A fresh invocation must not reuse an accepted event ID with
new timestamps/signature, because Sekko rejects event-ID collisions.

## Future Pay caller change (proposal only)

After publishing/reviewing this helper and reusable workflow and preparing an
approved config/key, change **only** `jobs.pay-api` in Pay's
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
and key registration exist and are reviewed. Clearing it disables future
notices. It does not cancel an invocation already running. The source SHA
must continue to come from `needs.pay-api-image.outputs.source-sha`.

## Local verification

```bash
python3 scripts/test-sekko-release-notice.py
python3 scripts/test-ssm-deploy.py
actionlint .github/workflows/*.yml
```

The notice harness uses only local fixtures and injected transports, including
canonical signing, opt-out and every eligibility/config gate, mismatched
readiness/SHA, transport and HTTP retry classes, duplicate acknowledgement,
redirect rejection, constant retry bytes, bounded response reads, verified TLS
and safe gap evidence. No live inbox/readiness/deployment/provider/model is
called. The existing deploy harness additionally requires Docker and its
pinned yq image; missing tools are verification gaps, not live-test permission.
