#!/usr/bin/env bash
set -euo pipefail
# Inspect only the runtime itself, without caller-supplied startup JavaScript.
unset NODE_OPTIONS

[[ ${RUNNER_OS:-} == Linux && ${RUNNER_ENVIRONMENT:-} == github-hosted ]] || {
  echo '::error::This bootstrap requires a GitHub-hosted Linux runner'
  exit 1
}
[[ ! -e /.dockerenv && ! -e /run/.containerenv ]] || {
  echo '::error::Container jobs cannot patch the host action runtime'
  exit 1
}
case "$(uname -m)" in
  x86_64) arch=x64; checksum=6c172a9ab6718f7b801fb5a7bd486af2ee5e32758086a04b84fcce2aa77095e6 ;;
  aarch64) arch=arm64; checksum=dbabf7a1cbf947e6059ffad2c27b80041db97dea1e553b62afdee7f13fca336e ;;
  *) echo '::error::Only Linux x64 and arm64 action runtimes are supported'; exit 1 ;;
esac

# Derive the runtime from this shell's actual Worker ancestor, never an input.
node=$(python3 <<'PY'
import os
from pathlib import Path

pid = os.getppid()
while pid > 1:
    proc = Path('/proc') / str(pid)
    try:
        executable = (proc / 'exe').resolve(strict=True)
    except OSError as error:
        raise SystemExit(f'Cannot inspect host Runner.Worker ancestry: {error.strerror}') from error
    if executable.name == 'Runner.Worker':
        root = executable.parent.parent
        if executable.parent.name != 'bin' or not root.is_relative_to('/home/runner'):
            raise SystemExit('Runner.Worker is outside the hosted runner directory')
        node = (root / 'externals/node24/bin/node').resolve(strict=True)
        # Hosted x64 runners link externals to this shared cache; ARM keeps it in root.
        if not (node.is_relative_to(root) or node == Path('/opt/runner-cache/externals/node24/bin/node')):
            raise SystemExit('Bundled Node 24 resolves outside the hosted runner installation')
        # Hosted binaries may be owned by runner (the Worker owner), rather than root.
        if not node.is_file() or node.stat().st_uid not in {0, executable.stat().st_uid}:
            raise SystemExit('Bundled Node 24 must be a regular file owned by root or the Runner.Worker owner')
        print(node)
        break
    status = (proc / 'status').read_text().splitlines()
    pid = int(next(line.split()[1] for line in status if line.startswith('PPid:')))
else:
    raise SystemExit('No host Runner.Worker ancestor found; container and detached execution are unsupported')
PY
)

fixed=$("$node" -p 'require("node:zlib").constants.BROTLI_DECODER_ERROR_FORMAT_BLOCK_SWITCH === -17')
if [[ $fixed == false ]]; then
  work=$(mktemp -d "$RUNNER_TEMP/node24-security.XXXXXX")
  candidate=''
  cleanup() {
    rm -rf "$work"
    if [[ -n $candidate ]]; then sudo -n rm -f -- "$candidate"; fi
  }
  trap cleanup EXIT
  archive="node-v24.22.1-linux-$arch.tar.xz"
  curl --fail --location --proto '=https' --proto-redir '=https' --tlsv1.2 \
    --connect-timeout 15 --max-time 180 "https://nodejs.org/dist/v24.22.1/$archive" \
    --output "$work/$archive"
  printf '%s  %s\n' "$checksum" "$work/$archive" | sha256sum --check
  tar -xJf "$work/$archive" -C "$work" --strip-components=2 "node-v24.22.1-linux-$arch/bin/node"
  [[ $("$work/node" -p process.version) == v24.22.1 ]]
  "$work/node" -e 'if(require("node:zlib").constants.BROTLI_DECODER_ERROR_FORMAT_BLOCK_SWITCH !== -17) process.exit(1)'
  candidate=$(sudo -n mktemp "$(dirname "$node")/.node-security.XXXXXX")
  sudo -n install -m 0755 "$work/node" "$candidate"
  sudo -n chown --reference="$node" "$candidate"
  sudo -n chmod --reference="$node" "$candidate"
  sudo -n mv -fT -- "$candidate" "$node"
  candidate=''
else
  [[ $fixed == true ]]
fi
"$node" -e 'if(require("node:zlib").constants.BROTLI_DECODER_ERROR_FORMAT_BLOCK_SWITCH !== -17) process.exit(1)'
"$node" -p 'JSON.stringify({execPath:process.execPath,node:process.versions.node,brotli:process.versions.brotli,blockSwitch:require("node:zlib").constants.BROTLI_DECODER_ERROR_FORMAT_BLOCK_SWITCH})'
printf '%s\n' FORCE_JAVASCRIPT_ACTIONS_TO_NODE24=true >> "$GITHUB_ENV"
