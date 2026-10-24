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

The composite uses only a shell step. It derives the binary path from the
running Worker, downloads checksum-pinned official Node 24.22.1, and atomically
replaces only that bundled binary using the hosted runner's existing sudo
access. An already-fixed decoder is retained. It selects Node 24 for subsequent
legacy JavaScript actions through GitHub's supported migration setting.

Linux x64 and arm64 are supported; other hosts and container jobs fail before
any download or write. System Node, application dependencies and permissions
are unchanged. This is a per-job measure while hosted images roll out the fix.
It does not patch the host's other native Brotli consumers. Remove the step
after the provider's bundled decoder is independently verified fixed.

Sources: [Node security release](https://nodejs.org/en/blog/vulnerability/october-2026-security-releases)
and [official archive checksums](https://nodejs.org/dist/v24.22.1/SHASUMS256.txt).
