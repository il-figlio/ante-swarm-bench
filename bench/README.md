# Bench

This directory holds the method and code behind the live-model and replay studies. It has the task packs, the worker and strict model relay that run each harness on them, the adapters that put Ante's core under other harnesses' tools, two deterministic replay studies, and the cohort builder that turns run receipts into a summary.

Everything you need to run it on your own host is here.

| Path | What it is |
|---|---|
| [`protocol/`](protocol/) | Protocol shared-v3. `worker.py` runs N concurrent instances of one harness on one pack, each with a fresh home, samples RSS, and applies the checker gate. `model_gate.py` is a loopback relay. It pins model, effort, and output cap on every request, and it never forwards credentials. The directory also has `usage_tracker.py`, `summarize_run.py`, `protocol.json` (frozen cohort definitions), and the install, probe, gate, and matrix scripts. [README](protocol/README.md). `protocol/earlier/` keeps the frozen definitions of two earlier protocols (shared-v1, lean-v1). |
| [`adapters/`](adapters/) | `ante-pi` runs Ante's core with Pi's native file and shell tools over MCP. `ante-hermes` and `ante-hermes-lean` do the same for Hermes. Their tool schemas sit alongside them. `verify_native.py` is a pinned gate for native read/write and interrupted streams. |
| [`tasks/`](tasks/) | Task packs. Each one has a `tasks.json` (prompts) and `tests/check_*.py` (hidden checkers). The data is synthetic except in `tbench-ported`. `toolbox` is the pack for the live cohorts: four small Python functions with unit tests. The rest (budget-report, data-pipeline, device-agent, flask-fixes, log-triage, migrate-off, offline-notes, repo-hygiene, tbench-ported) are longer multi-step tasks for the hub runner. The `.log` corpus in log-triage is gitignored. Run `python3 tools/gen_logs.py` inside that pack to rebuild it byte for byte against `tests/manifest.json`. |
| [`tasks/tbench-ported/`](tasks/tbench-ported/) | Five tasks adapted from Terminal-Bench (Apache-2.0; [ATTRIBUTION.md](tasks/tbench-ported/ATTRIBUTION.md)). The binary inputs aren't committed. Run `python3 fetch_inputs.py` in that directory to download and verify them. The t05 archive can't be rebuilt from upstream right now, because the repository it clones is gone. `check_t05.py` skips without it. |
| [`studies/runtime-replay/`](studies/runtime-replay/) | Study `core-text-20261016-v2`: a deterministic HTTP/SSE replay of a 65-turn text conversation against Ante's session core, Pi's `Agent`, and Hermes's `AIAgent`, at 1, 2, 4, and 8 instances. It measures cgroup CPU and summed PSS. No model, no tools. |
| [`studies/tool-replay/`](studies/tool-replay/) | Study `matched-tools-20261016-v1`: the same replay method, but with real pinned native read/write tools. It compares Pi vs Ante-Pi and Hermes vs lean Ante-Hermes. It also includes engine-free direct-vs-MCP controls that isolate the bridge cost. |
| [`build-cohorts.py`](build-cohorts.py) | Builds a cohort summary JSON from run receipts and progress logs. Every number is read from a receipt. Any disagreement between the two stops the build. |

Not in this repository: result data, reproduction-kit manifests, raw receipts, run archives, and progress logs.

## Tests

From the repository root:

```sh
python3 -m unittest discover -s tests/bench -p 'test_*.py'
```

`test_relay.py` drives `model_gate.py` against a fake upstream. It checks that a wrong model is refused and never forwarded, that effort and caps are forced, that sampling parameters are removed, and that no auth headers get through. It also checks that SSE streams before the end and that the audit log contains no prompts. `test_adapter_completion_v2.py` and `test_ante_hermes_lean.py` check the adapters' completion and shutdown paths using mocked processes.

## Running a cohort

The worker needs a Linux host with the pinned harness installs listed in `protocol/protocol.json`. It also needs a model gateway that authenticates the host (`BENCH_MODEL_UPSTREAM`). See [protocol/README.md](protocol/README.md). The replay studies need Linux cgroup v2, root for a private network namespace, and the pinned runtimes in their `protocol.json`. Their READMEs give the exact commands. Neither is a portable installer.

## Provenance

Exported from a private monorepo at commit `ed46a4de`: the protocol, adapters, task packs, cohort builder and two replay studies. `protocol/README.md` was written for this repository from the protocol definition, and `protocol/earlier/` holds two frozen protocol definitions that sat beside the results data. The `tbench-ported` attribution was restored, and its archive was replaced by `fetch_inputs.py`. Hostnames, a local path and the hosting platform's name were replaced in prose. No number in the code changed.
