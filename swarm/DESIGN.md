# Swarm — design

Swarm measures how many coding-agent instances a machine can run at once, and what each one costs, on tasks you choose. It runs on one laptop or a set of small machines ("lanes"). Every run produces a bundle anyone can verify and compare.

Swarm asks: **can many Ante instances run in parallel, in separate sandboxes, on resource-constrained machines, and still finish verified work?** To answer honestly, it measures every harness the same way, keeps the losses, and lets anyone rerun it.

## What it is not

- Not a capability leaderboard. Capability (Terminal-Bench and similar) stays with [Harbor](https://github.com/harbor-framework/harbor). Swarm tasks borrow Harbor's shape (`task.toml`, `instruction.md`, `tests/`), but use a `workspace/` folder instead of an `environment/` Dockerfile. There is no Harbor task importer.
- Not an optimisation loop. A spec is frozen before it runs; a change makes a new cohort.

## Principles

These are requirements, not style.

1. **One question per run.** A spec names the question, the primary outcome, every setting, the budget and the stopping rule. `swarm plan` freezes it into a hashed plan before anything starts.
2. **Configuration is not evidence.** Model, effort and output cap are checked on the wire for every request, not trusted from flags.
3. **Completion is a runtime outcome.** An attempt passes only when the agent exits cleanly, the checker passes on untouched tests, no file outside the allowed set changed, every model request completed and the sandbox is empty afterwards.
4. **Failures are data.** Nothing is retried after the fact. Every attempt, admission decision and sample is kept.
5. **Pressure, not "RAM used".** Admission follows the kernel's own pressure signal (Linux PSI, macOS memorystatus). Gaps in sampling are unobserved time, never good time.
6. **Uncertainty is visible.** Missing usage stays `null`. Measurement scope (rusage vs cgroup, process vs container) is written into every record.
7. **Context hygiene.** Agents read instruction files (`AGENTS.md`, `CLAUDE.md`) from parent directories. Sandboxes live under a clean temp root so no harness sees context the others don't.

## Concepts

| Concept | Meaning |
|---|---|
| **Task** | A Harbor-like directory with `task.toml`, `instruction.md`, `workspace/` (starting files), and `tests/test.sh` (exit 0 means pass). `script.json` is optional and drives the scripted model. |
| **Harness** | A plugin that installs nothing. It configures and launches one agent CLI against a model endpoint. Options: `ante`, `codex`, `claude-code`, `nanocode`, `hermes`, `pi`, and `mini` (the reference floor). Tier 0 packs measure harness overhead. The script writes the known solution, so these numbers don't rank capability. |
| **Model source** | `scripted` is a local, deterministic model. It runs each harness through the same tool steps, using the harness's own native tools. It spends nothing and makes no network calls. `relay` sends the same per-attempt routes to a real provider gateway. That gateway enforces strict checks on model, effort, cap, and token budget (see [docs/gateway.md](docs/gateway.md)). |
| **Sandbox** | Where an attempt runs. Only `process` is available: fresh HOME and workdir, clean environment, its own session. It's portable but not a security boundary (see the README's trust model). There's no namespaced (`bwrap`) or container sandbox. Maze cells can run under a cgroup v2 limit on Linux (`limits.py`). |
| **Level** | A concurrency setting. Each run measures every harness at every level, in a seeded, randomly interleaved order. |
| **Admission** | Before each launch, the runner checks host memory pressure, available memory, free task slots (cgroup `pids.max` on Linux, `kern.maxprocperuid` on macOS), and free disk on the filesystems holding the sandboxes and the bundle (`reserve_disk_mb`). Every reading is taken fresh. Unknown readings hold the launch unless `allow_unknown` is set. Held launches get logged with their reason and wait time. |
| **Bundle** | The output is `plan.json`, `host.json`, `attempts.jsonl` (hash-chained), `samples.jsonl`, `admission.jsonl`, `summary.json`, `report.html`, and `MANIFEST.json`. `swarm verify` rechecks all of them. |
| **Control** | `mini` runs on `tasks/tool-loop-50` at size 1, for 3 rounds, with a fixed seed. It runs on a lane inside each run's own job. Its medians form the lane's reference line, and time metrics are reported as a ratio to that control. A failed control gets recorded, but it doesn't stop the run. |
| **Quiet gate** | Checks that the lane is quiet before a run and before its control. It looks at swap, other CPU use, memory against baseline, disk, leftover agent processes, and memory pressure. In `observe` mode it only records. In `enforce` mode it holds, resets leftovers once, then refuses with `not_quiet`. The gate checks before each run, the reset cleans up after, and admission watches headroom during. |
| **Cohort** | A 12-character hex hash over what makes two rows comparable: preset, size, protocol, model, tasks, harness binary and adapter, canary, control key, gate settings, and `VERSION`. Rows get pooled, ranked, and tested only within one cohort. See [docs/evaluation.md](docs/evaluation.md#cohorts-rerun-and-supersede). |

## Headline outputs

Per harness, per level:

- **Verified successes per minute** and pass rate (Wilson interval).
- **Peak memory per instance**: largest-process RSS from rusage and sampled process-tree RSS, each labeled by scope. Cgroup peaks get recorded wherever the runner has a cgroup.
- **CPU seconds per verified success**.
- **Concurrency reached**: the most instances alive at once in each cell, plus admission holds with their reasons and wait time. In ramp mode, it also records the reason each harness stopped climbing.
- **Wall time** median and p90, time to first verified result, and the runner's own CPU use.

The comparison that matters for the thesis is the **density curve**. It tracks successes per minute and memory per instance as N grows, on a fixed small box. The reference shape is 2 vCPU / 8 GB.

## CLI

```
swarm probe                       # host fingerprint, memory pressure, task headroom
swarm plan spec.toml -o plan.json # validate (unknown keys rejected) and freeze: hashes tasks, harness binaries, spec
swarm run plan.json -o bundle/    # execute into a new directory; on crash or cancel the bundle is still sealed and says why
swarm verify bundle/              # manifest, attempt chain, plan/cell/attempt reconciliation, summary recomputation
swarm report bundle/ -o out.html  # render the report outside the bundle (run also writes report.html inside it)
```

Races add a paired comparison across repetitions: the median of paired differences and a 95% bootstrap interval. See [docs/evaluation.md](docs/evaluation.md). There is no `swarm compare` for two bundles and no resume for an interrupted run.

## Spec

```toml
question = "How does Ante's verified throughput scale with concurrency on 2 vCPU / 8 GB?"
primary_outcome = "successes_per_minute"
model = { source = "scripted", id = "swarm-scripted-1", effort = "medium", max_output_tokens = 16384 }
harnesses = ["ante", "mini"]
levels = [1, 4, 8, 16]
repeats = 2                # rounds per (harness, level)
tasks = ["tasks/*"]
sandbox = "process"
seed = 7
stop_rule = "Run every cell once; report all of them."

[budget]
max_requests_per_attempt = 40
attempt_timeout_s = 300

[admission]
reserve_mb = 1024
reserve_tasks = 512        # free process/thread slots to keep
max_pressure = "normal"    # normal | warn
allow_unknown = false      # hold launches when pressure or memory can't be read

[harness_options.ante]
telemetry = "off"          # required: "on" also needs telemetry_env = "swarm-bench-..."
```

Replace `levels` with a `[ramp]` table (`start`, `factor`, `max`, `stop_pass_rate`) to climb until the host pushes back.

## Visual language

Use brand orange `#EA5518` as the only brand signal, with ink `#252525`, page `#F9F9F9`, hairline borders and no shadows. Use Space Grotesk for UI and IBM Plex Mono with tabular numerals for every number. "Quiet chrome, loud state": status pills carry the colour; uncertain or unmeasured values are dashed, never drawn as zero.
