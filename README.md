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
