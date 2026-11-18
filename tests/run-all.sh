#!/usr/bin/env bash
# Every test suite in this repository, from a fresh clone. Python 3.11+, standard library only.
# Optional: `node` (browser-script parity tests) and an `ante` binary (Ante harness test, end-to-end live rounds);
# the tests that need them are skipped when they are missing.
#   tests/run-all.sh            bench + hub + swarm (the swarm suite takes several minutes)
#   tests/run-all.sh quick      bench + hub only
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1

echo "== bench: protocol relay and adapters"
(cd "$ROOT" && python3 -m unittest discover -s tests/bench -p 'test_*.py')

echo "== hub: hub, runner, annotations"
(cd "$ROOT/hub" && python3 -m unittest discover -s tests)

if [[ "${1:-}" != "quick" ]]; then
  echo "== swarm: harness, lanes, front, maze, games"
  (cd "$ROOT/swarm" && PYTHONPATH=src:tests python3 -m unittest discover -s tests)
fi
