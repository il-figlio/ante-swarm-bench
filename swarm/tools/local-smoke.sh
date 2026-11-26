#!/bin/sh
# Download the pinned public benchmark and run the bundled, scripted mini agent.
set -eu
python3 -c 'import sys; sys.exit("Python 3.11+ is required") if sys.version_info < (3, 11) else None'
parent=$(cd "${1:-${TMPDIR:-/tmp}}" && pwd -P)
work=$(mktemp -d "$parent/swarm-smoke.XXXXXXXX")
echo "Swarm working directory: $work"
revision=952b4097b5665d018203c27c093c894a06d72c3c
curl -fsSL "https://codeload.github.com/il-figlio/ante-swarm-bench/tar.gz/$revision" -o "$work/swarm.tar.gz"
python3 - "$work/swarm.tar.gz" <<'PY'
import hashlib
import sys
with open(sys.argv[1], "rb") as stream:
    digest = hashlib.file_digest(stream, "sha256").hexdigest()
if digest != "b668c9e3c7046ebaaed9dcb7121b655f3c8926f7f86ea84dfdce445f1add3ff0":
    raise SystemExit("Source archive SHA-256 differs from the pinned download.")
PY
tar -xzf "$work/swarm.tar.gz" -C "$work"
cd "$work/ante-swarm-bench-$revision/swarm"
export PYTHONPATH=src PYTHONDONTWRITEBYTECODE=1
python3 -m swarm plan examples/smoke-mini.toml -o "$work/plan.json"
python3 -m swarm run "$work/plan.json" -o "$work/bundle"
python3 -m swarm verify "$work/bundle"
echo "HTML report: $work/bundle/report.html"
python3 - "$work/bundle/summary.json" <<'PY'
import json
import sys
from pathlib import Path
summary = json.loads(Path(sys.argv[1]).read_text())
groups = summary["groups"]
for cell in groups.values():
    print(f"{cell['harness']} at {cell['level']} concurrent: {cell['passes']}/{cell['attempts']} passed")
if not summary["complete"] or not all(c["passes"] == c["attempts"] for c in groups.values()):
    raise SystemExit("The run recorded an incomplete cell or a failed attempt. See the report.")
PY
