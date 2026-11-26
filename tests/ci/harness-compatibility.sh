#!/usr/bin/env bash
# GitHub-hosted x86-64 Linux. Install the published artifacts outside the checkout.
set -euo pipefail
root=$PWD
work=$(mktemp -d "${RUNNER_TEMP:-/tmp}/swarm-compat.XXXXXXXX")
cd "$work"
mkdir -p home/.ante/bin claude codex
download() {
  local url=$1 dest=$2 digest=$3
  curl --fail --silent --show-error --location "$url" -o "$dest"
  if [[ ${#digest} == 128 ]]; then
    echo "$digest  $dest" | sha512sum --check
  else
    echo "$digest  $dest" | sha256sum --check
  fi
}

download https://download.ante.run/releases/v0.3.0/ante-v0.3.0-linux-x86_64-musl.tar.gz ante.tgz b67ed92cbb1438f44780a4486724c57fef88bccf7ada417bcee098e81af254ed
tar xzf ante.tgz -C home/.ante/bin ante
download https://registry.npmjs.org/@openai/codex/-/codex-0.165.0-linux-x64.tgz codex.tgz 168b4373b6d51f06a89a6b6c7433fc3cfac7d1ae48c02fedf07fcb85d11c1f8101aa07981510ada6d88772ec20bcd4422db4db3b2144fd4afa1b360296381a49
tar xzf codex.tgz -C codex
download https://registry.npmjs.org/@anthropic-ai/claude-code-linux-x64/-/claude-code-linux-x64-2.1.312.tgz claude.tgz a23e791cbf6e6ac2d6cf29b97993600bcc729343741f8870f1674dd03783b8096aba1b367788619105597112af25aac1077ec853d6db1065d9c2dba1b1ec7a11
tar xzf claude.tgz -C claude
download https://github.com/earendil-works/pi/releases/download/v1.5.2/pi-linux-x64.tar.gz pi.tgz 811c6f2c9e7aaee252b5404252e556fb713aa209c1f7f924226aa77d4ef70cb5
tar xzf pi.tgz
download https://github.com/nanocodexyz/nanocode/releases/download/nanocode-cli/nanocode-x86_64-unknown-linux-gnu.6ed92be87b3a.gz nanocode.gz ab528482c25cf9828b25738934775d31efb02af8470550eb2d08b1e4de11df7c
gzip -dc nanocode.gz > nanocode
chmod +x nanocode

# Each test runs as the ordinary runner user. systemd delegates an empty cgroup
# to this process, so the limits tests can create and clean up their own children.
sudo systemd-run --wait --pipe --collect --uid="$(id -un)" -p Delegate=yes \
  --working-directory="$root/swarm" \
  --setenv="HOME=$work/home" --setenv="PATH=$work/home/.ante/bin:/usr/bin:/bin" \
  --setenv=PYTHONDONTWRITEBYTECODE=1 --setenv=PYTHONPATH=src:tests \
  --setenv="SWARM_TEST_CODEX=$work/codex/package/vendor/x86_64-unknown-linux-musl/bin/codex" \
  --setenv="SWARM_TEST_CLAUDE=$work/claude/package/claude" \
  --setenv="SWARM_PI_BINARY=$work/pi/pi" --setenv="SWARM_NANOCODE_BIN=$work/nanocode" \
  "$(command -v python3)" "$root/tests/ci/real_harnesses.py"
