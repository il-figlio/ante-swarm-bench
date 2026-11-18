#!/usr/bin/env bash
# Install the shared-v3 campaign on the bench host into /opt/ante-bench/campaign-20261031/.
# Installs Codex CLI and Claude Code side by side (pinned npm versions, sha256s recorded) and reuses the shared-v2 Ante, Pi, Hermes
# installs read-only by symlink (same pinned versions as campaign-20261030, which stays untouched).
# Refuses to run while the shared-v2 unit is active or a benchmark holds worker.lock (installing would skew its timings).
# Run from the directory holding this file, as a user that can write /opt/ante-bench (and chown to ante-bench when root).
set -euo pipefail
CODEX_VERSION=${CODEX_VERSION:-0.165.0}
CLAUDE_CODE_VERSION=${CLAUDE_CODE_VERSION:-2.1.312}
OLD=${OLD:-/opt/ante-bench/campaign-20261030}
C=${C:-/opt/ante-bench/campaign-20261031}
ROOT=${ROOT:-/var/lib/ante-bench}
NODE_DIR=${NODE_DIR:-/opt/swarm/node/bin}
NODE_MAJOR=${NODE_MAJOR:-24}
ANTE_SHA=${ANTE_SHA:-acd49a8c38a773bdcde0a2dcbf826f07a065bf4f53c74db4011183b9a3a9b2b8}  # overridable for local script tests only
HERE=$(cd "$(dirname "$0")" && pwd)
export PATH="$NODE_DIR:/usr/bin:/bin"

# preflight
if systemctl is-active --quiet ante-bench-shared-v2 2>/dev/null; then echo "ante-bench-shared-v2 is still active; refusing to install"; exit 3; fi
if [[ -e $ROOT/worker.lock ]] && ! flock -n "$ROOT/worker.lock" true 2>/dev/null; then echo "a benchmark holds $ROOT/worker.lock; refusing"; exit 3; fi
free=$(python3 -c "import shutil;print(shutil.disk_usage('/opt').free)")
(( free > 3*1024*1024*1024 )) || { echo "less than 3 GiB free under /opt"; exit 3; }
[[ -d $OLD ]] || { echo "missing $OLD"; exit 2; }
[[ ! -e $C ]] || { echo "$C already exists; remove it deliberately or set C"; exit 2; }
"$NODE_DIR/node" -v | grep -q "^v$NODE_MAJOR\\." || { echo "expected Node $NODE_MAJOR in $NODE_DIR"; exit 2; }

mkdir -p "$C"
# our files
for f in worker.py model_gate.py usage_tracker.py summarize_run.py protocol.json run-v3-matrix.sh gate-v3.sh probe-claude.sh; do cp "$HERE/$f" "$C/$f"; done
chmod +x "$C"/*.sh "$C/summarize_run.py"
# identical task pack (byte-copy, hashes compared)
cp -a "$OLD/tasks" "$C/tasks"
( cd "$OLD/tasks" && find . -type f -not -name '*.pyc' -print0 | sort -z | xargs -0 sha256sum ) > "$C/tasks.sha256.old"
( cd "$C/tasks" && find . -type f -not -name '*.pyc' -print0 | sort -z | xargs -0 sha256sum ) | diff -q - "$C/tasks.sha256.old" >/dev/null || { echo "task pack differs from shared-v2"; exit 2; }
# shared-v2 harnesses, referenced read-only (versions asserted)
for x in ante pi hermes-venv hermes-src hermes-seed hermes-preparation.json; do ln -s "$OLD/$x" "$C/$x"; done
# Hermes provider=anthropic needs the Anthropic SDK, which the shared-v2 venv (uv sync without the anthropic extra) lacks. Add the two pinned
# upstream-locked packages into a side directory on PYTHONPATH instead of touching the frozen venv.
uv pip install --python "$OLD/hermes-venv/bin/python" --target "$C/hermes-extra" --no-deps anthropic==0.87.0 docstring-parser==0.18.0
"$C/ante" --version | grep -q 'ante 0.3.0'
[[ $(sha256sum "$OLD/ante" | cut -d' ' -f1) == $ANTE_SHA ]]
"$NODE_DIR/node" "$C/pi/node_modules/@earendil-works/pi-coding-agent/dist/cli.js" --version | grep -q '^1\.4\.0'

# Codex CLI and Claude Code side by side (each in its own prefix; lockfile pins the full tree)
install_pkg() {  # dir package version
  mkdir -p "$C/$1"; ( cd "$C/$1" && npm init -y >/dev/null && npm install --save-exact --no-audit --no-fund "$2@$3" )
}
install_pkg codex @openai/codex "$CODEX_VERSION"
install_pkg claude-code @anthropic-ai/claude-code "$CLAUDE_CODE_VERSION"
CODEX_BIN=$C/codex/node_modules/.bin/codex; CLAUDE_BIN=$C/claude-code/node_modules/.bin/claude
"$CODEX_BIN" --version | grep -q "$CODEX_VERSION"
"$CLAUDE_BIN" --version | grep -q "$CLAUDE_CODE_VERSION"

# record sha256s of what will actually run (native binaries behind the npm wrappers) and of the lockfiles
CODEX_NATIVE=$(find "$C/codex/node_modules/@openai" -path '*vendor*' -type f -name codex -perm -u+x | head -1)
CLAUDE_NATIVE=$(readlink -f "$C/claude-code/node_modules/.bin/claude")
python3 - "$C" "$CODEX_VERSION" "$CLAUDE_CODE_VERSION" "$CODEX_NATIVE" "$CLAUDE_NATIVE" <<'PY'
import hashlib, json, subprocess, sys, time
c, cv, kv, cn, kn = sys.argv[1:6]
sha = lambda p: hashlib.sha256(open(p, 'rb').read()).hexdigest()
rec = {'installed_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
       'codex': {'package': '@openai/codex', 'version': cv, 'version_output': subprocess.check_output([f'{c}/codex/node_modules/.bin/codex', '--version'], text=True).strip(), 'native_binary': cn, 'native_sha256': sha(cn), 'package_lock_sha256': sha(f'{c}/codex/package-lock.json')},
       'claude_code': {'package': '@anthropic-ai/claude-code', 'version': kv, 'version_output': subprocess.check_output([f'{c}/claude-code/node_modules/.bin/claude', '--version'], text=True).strip(), 'native_binary': kn, 'native_sha256': sha(kn), 'package_lock_sha256': sha(f'{c}/claude-code/package-lock.json')},
       'ante_sha256': sha(f'{c}/ante'), 'protocol_sha256': sha(f'{c}/protocol.json'), 'worker_sha256': sha(f'{c}/worker.py'), 'relay_sha256': sha(f'{c}/model_gate.py')}
json.dump(rec, open(f'{c}/install-v3.json', 'w'), indent=1)
print(json.dumps(rec, indent=1))
PY
touch "$C/progress-v3-claude.log" "$C/progress-v3-codex.log"
chmod -R a+rX "$C"
if [[ $(id -u) == 0 ]] && id ante-bench >/dev/null 2>&1; then chown ante-bench "$C/progress-v3-claude.log" "$C/progress-v3-codex.log"; fi
echo "installed $C. Next: probe-claude.sh, gate-v3.sh, run-v3-matrix.sh (each step after the probe makes paid model calls)."
