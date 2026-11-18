# Configured runtime overhead: first controlled text-turn study

This directory holds a deterministic HTTP/SSE replay experiment. It runs no model, spends no subscription quota, and executes no tools or benchmark tasks.

`protocol.json` freezes study `core-text-20261016-v2` before the measured campaign. Profiles beyond this text-turn workload (tools, faults, larger contexts) are not implemented here. Don't read the fixture DSL as completed engine compatibility.

- Ante 0.2.11 session API over stdio, with a small stdlib Python controller kept inside its cgroup.
- Pi 0.85.1 `Agent` with the pinned native Responses SDK, no coding CLI.
- Hermes pinned AIAgent, no CLI. A disclosed subclass replaces only the system prompt builder, because its supplemental prompt is additive. No tool grants, memory, background review, context files, or trajectory saving.
- Every real inference request must match the system/user/assistant history, model label, effort, and output budget. The server rejects skipped history and unexpected tools. Every client must return the exact final string for every turn.
- Private network namespace, loopback only. Fresh home and workspace for each runtime process. Metadata probes are recorded separately and don't advance the conversation.
- 65 turns per process: one first success, a barrier, then 64 warm turns on a growing conversation. Six full engine-order permutations at 1/2/4/8 instances, 72 serial cells. The process is the experimental unit. Dependent turns are not independent trials.
- CPU: cgroup v2 complete group, including exited descendants. `accounting_probe.py` checks this against a known 500 ms child. Cold and warm deltas exclude teardown.
- Memory: summed PSS from `smaps_rollup`, sampled every 100 ms plus a final barrier. Shared pages count proportionally, and transient peaks can be missed. RSS is reported separately. Group peak divided by instance count is a batch average, not a sampled individual peak.
- Driver, HTTP server, and sampler stay outside engine groups. Their combined CPU is reported separately, but they still contend with the engines on the same 2-vCPU VM. Wall time is not isolated inner-loop latency.
- An initial 20 ms observer comparison was too costly, so 100 ms was chosen before v2. An accidental v1 launch at 20 ms was stopped and excluded in full. Calibration and invalid attempts stay as separate evidence.
- Ante session Shutdown/EOF did not terminate the daemon within 500 ms. Bounded SIGTERM cleanup is recorded and excluded from performance. An empty cgroup is required after every cell.

The measured workload includes HTTP serialization, SSE parsing, history management, runtime bookkeeping, and control shims. It cannot isolate a language-independent core instruction loop. Native Hermes keeps its API runtime housekeeping. Its restored read-only source archive has no precompiled bytecode, so cold startup includes source parsing. It is not a prepared-install comparison. Warm turns happen after imports.

## Run on the prepared benchmark VM

The driver requires Linux cgroup v2, root (to create a private network namespace and per-cell groups), `ip`, a dedicated `ante-bench` account, and the exact installed runtime paths and pins in the protocol. It must never run on a host that serves anything else, or run concurrently with another benchmark. The paths are hardcoded on purpose. This is not a portable installer.

```sh
sudo python3 /opt/ante-bench/core-20261016-frozen-v2/accounting_probe.py
sudo unshare -n sh -c 'ip link set lo up; exec python3 /opt/ante-bench/core-20261016-frozen-v2/runner.py /var/lib/ante-bench/NEW-UNIQUE-OUTPUT study'
```

Use a new output directory. Existing cells fail closed on directory collisions. Keep calibration failures, and don't replace a losing result. No cache dropping and no network or account changes on the shared guest. The historical public kitv2.1 predates this runner.

Next causal tests: fixed tool loops and matched tool transport, larger contexts, retry/cancel cleanup, then setup/install/auth as a separate scorecard. Live task quality and model wait stay apart. They are not synthetic runtime measurements.
