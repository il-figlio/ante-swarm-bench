# Contributing

Contributions are welcome under the Apache License 2.0 (see [LICENSE](LICENSE)).

## Before you open a pull request

- **Tests pass.** Run `tests/run-all.sh` (bench, hub and swarm) from a fresh clone. `tests/run-all.sh quick` runs bench and hub only. Python 3.11+, standard library only; `node` and an `ante` binary are optional and their tests are skipped without them.
- **Fixtures stay synthetic.** Test data, run fixtures, logs and task inputs are generated or hand-written. Never commit real run receipts, credentials, tokens, host names, e-mail addresses or personal data.
- **No real model calls in tests.** Tests use the scripted model, the fake upstream (`tests/bench/fake_upstream.py`) or stand-in mode. A test must never need network access or a provider credential.

## Test environment

Result of `tests/run-all.sh` (full suite) on macOS 26.2 arm64, Python 3.14.8, with `node` and `ante` installed, on 2026-11-18:

| Suite | Result |
|---|---|
| `bench` (protocol relay, adapters) | 13 tests, OK |
| `hub` (hub, runner, annotations) | 50 tests, OK |
| `swarm` (harness, lanes, front, maze, games, live play) | 541 tests, OK (11 skipped) |

The 11 swarm skips need Linux (`/proc`, a writable delegated cgroup v2, non-UTF-8 file names) or optional harness binaries that were not installed (Pi, nanocode, Codex CLI, Claude Code). Without `node`, the browser-script parity tests are skipped too; without an `ante` binary, the Ante harness test and the end-to-end live rounds are skipped.

The `tbench-ported` checkers skip without fetched inputs (`OK (skipped=3)`); with the inputs, they fail on an unsolved workspace and pass after the upstream reference solution.

The swarm suite launches real processes (the `mini` agent, the scripted model, lane services, and Ante for the live rounds) and goes through admission, so it needs free disk and normal memory pressure. `test_maze_integration` is the slowest module.

### Parts that need Linux

The live bench worker and the replay studies need Linux with cgroup v2, root for a private network namespace,
pinned harness installs and (for the worker) a model gateway; see their READMEs. The `tbench-ported` task pack's
binary inputs are fetched, not committed: `python3 bench/tasks/tbench-ported/fetch_inputs.py` (see its
[ATTRIBUTION.md](bench/tasks/tbench-ported/ATTRIBUTION.md); one of the three inputs can no longer be rebuilt from
upstream).

## Design principles

Changes to the harness follow the principles in [swarm/DESIGN.md](swarm/DESIGN.md#principles):

1. **One question per run.** A spec names the question, the outcome, every setting, the budget and the stopping rule, and is frozen before anything starts.
2. **Configuration is not evidence.** Model, effort and output cap are checked on the wire for every request.
3. **Completion is a runtime outcome.** An attempt passes only on a clean exit, a passing checker on untouched tests, no stray file changes, complete model requests and an empty sandbox.
4. **Failures are data.** Nothing is retried after the fact; every attempt, admission decision and sample is kept.
5. **Pressure, not "RAM used".** Admission follows the kernel's own pressure signal.
6. **Uncertainty is visible.** Missing values stay `null`, and measurement scope is recorded.
7. **Context hygiene.** Sandboxes live under a clean temp root so no harness sees context the others don't.

## Security

Report vulnerabilities privately; see [SECURITY.md](SECURITY.md).
