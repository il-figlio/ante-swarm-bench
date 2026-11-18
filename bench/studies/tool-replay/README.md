# Matched native-tool replay and bridge-route ablation

This experiment uses actual pinned Pi and Hermes read/write tools, a deterministic Responses replay server, and no live model or account credentials. It extends the text-only study without changing that study's source, receipts or conclusions.

Two **within-family** engine pairs:

- Pi `Agent` + Pi native `read`/`write` versus Ante session core + MCP transport around exactly those Pi implementations.
- Hermes `AIAgent` + native `read_file`/`write_file` versus Ante core + lean MCP transport around exactly those Hermes implementations. This excludes the full Hermes CLI wrapper.

Two independent **engine-free** route pairs run the same trace through the same native tool-dispatch code: in-process direct invocation versus a separate process over MCP stdio. These controls measure the cost of the bridge route, including process layout, controller, JSON framing and IPC. An agent-pair delta alone also changes the engine and cannot identify bridge overhead.

The two families have different native schemas, output formats and implementation details. Only within-family pairs are strict schema/result matches. The shared logical trace is identical across families: write1KiB into a fresh relative file, read it, then finish. Every episode has a distinct path to avoid stale-read, rewrite-warning and dedup state differences. Exact semantic arguments, full native outputs and all accumulated user/assistant/tool history are gated before the next model response. Only MCP name prefixes and transport content envelopes are normalized; workspace prefixes map to `$WORKSPACE`. Other native result fields remain checked. The common extra system prompt replaces Hermes's additive prompt builder using the same disclosed prompt-only subclass as the text experiment.

33 episodes/process: one cold first success, synchronized barrier,32 warm episodes (64 tools), then synchronized end. Final file contents are independently checked for all33 paths. Model-request count99/process for engine lanes and0 for controls. Actual native functions remain in use; this is not a dummy tool substitution. Model label/medium effort/16k cap are equal within every engine pair; there is no model computation.

The restricted bridges retain production native dispatch and MCP behavior for read/write. The Hermes bridge is a scoped/instrumented copy of lean bridge0.1; Pi bridge dispatch is factored into an importable module so the direct and stdio controls share the same code. Grants are restricted to the two exercised tools, and tool audit timers are added. Neither frozen production adapter is overwritten. Cancellation, parallel tool calls, other tool types, retries and non-file workloads remain unmeasured.

CPU uses cgroup v2 totals including all descendants, including exited shell children from Hermes file operations. Per-cell subgroups separate runtime/controller and bridge. Bridge process startup before its entry point moves to the bridge group is charged to runtime/controller; warm component accounting occurs after that move. Body `process_time` audit fields exclude exited child CPU and are diagnostic only; they are not subtracted to manufacture transport overhead. Bridge-route differences come from paired direct/MCP cgroup totals.

PSS is summed over all group processes at100ms plus the final barrier. Component PSS values are retained at each observation; independent component peaks must not be added to infer a simultaneous total. Sampler/server/driver sit outside both groups and their CPU is reported separately. The same2-vCPU guest still permits observer contention, especially for wall time. No filesystem-cache dropping. Hermes source is bytecode-prepared once before any measured cell, unlike the preceding text-study cold cohort; do not compare cold values between those studies.

Cleanup permits three seconds after parent exit for native Python executor shutdown, requires an empty complete group, then kills remaining descendants on failure and waits before removing groups. Ante stdio gets Shutdown/EOF and bounded SIGTERM after500ms as in the text study. Warm and cold measurements exclude teardown. Failed gates and all measured losses remain evidence; no rerun replaces an unfavorable measured repeat.

The frozen `protocol.json` defines four balanced Williams orderings of the four variants in each lane at1/2/4/8,128 cells total. Cells never overlap. Correctness gates precede freezing. Primary outcomes: warm group CPU per completed tool and sampled full-group PSS per instance. Cold startup and warm elapsed time are secondary. Every paired regression is flagged separately by metric; mixed resources/time is a tradeoff, not an invented composite winner. Four repeats do not establish statistical significance. Cross-family rankings and universal efficiency claims are excluded.

Private network namespace: loopback only, fresh homes/workspaces, dummy credentials, no live provider or subscription usage. Dependencies and paths are preinstalled/pinned in the protocol; this is not an installer or a clean-room reproduction claim. Public kitv2.1 predates this experiment.
