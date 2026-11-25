#!/usr/bin/env bash
# Every test suite in this repository, from a fresh clone. Python 3.11+, standard library only.
# Optional: `node` (browser-script parity tests) and an `ante` binary (Ante harness test, end-to-end live rounds);
# the tests that need them are skipped when they are missing.
#   tests/run-all.sh            bench + hub + swarm (the swarm suite takes several minutes)
#   tests/run-all.sh profile    show test names and the 20 slowest durations per suite (Python 3.12+)
#   tests/run-all.sh quick      bench + hub only
set -euo pipefail
ROOT=$(cd "$(dirname "$0")/.." && pwd)
export PYTHONDONTWRITEBYTECODE=1

# Python 3.11 supports the ordinary and quick runs. CI pins 3.12 for --durations.
test_mode=${1:-full}
run_tests() {
  if [[ "$test_mode" == profile ]]; then
    python3 -m unittest "$@" -v --durations 20
  else
    python3 -m unittest "$@"
  fi
}

echo "== bench: protocol relay and adapters"
(cd "$ROOT" && run_tests discover -s tests/bench -p 'test_*.py')

echo "== hub: hub, runner, annotations"
(cd "$ROOT/hub" && run_tests discover -s tests)

if [[ "$test_mode" != quick ]]; then
  echo "== swarm: harness, lanes, front, maze, games"
  if [[ -r /proc/pressure/memory || "${SWARM_REQUIRE_PSI:-0}" == "1" ]]; then
    (cd "$ROOT/swarm" && PYTHONPATH=src:tests run_tests discover -s tests)
  else
    echo "NOTE: /proc/pressure/memory unavailable on this host; admission controls remain intact."
    echo "      Running non-PSI swarm unit/parity suites (set SWARM_REQUIRE_PSI=1 on a PSI kernel for full discovery)."
    (cd "$ROOT/swarm" && PYTHONPATH=src:tests run_tests \
      test_anatomy test_export test_gate test_gateway test_harnesses test_hill \
      test_live test_maze test_maze_fleet test_nanocode test_page test_pi \
      test_play test_play_front test_relay test_solo test_stats test_trail \
      test_swarm.SummaryShapeTests test_swarm.ScriptedModelTests test_adversarial.AdmissionTests)
  fi
fi
