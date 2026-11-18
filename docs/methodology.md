# Methodology

How the numbers are produced and what makes two of them comparable. This page and the code are what you need to reproduce a run. Detailed contracts live next to the code; this page connects them: [swarm/docs/reference.md](../swarm/docs/reference.md) (what a run measures), [swarm/docs/evaluation.md](../swarm/docs/evaluation.md) (lane control, quiet gate, cohorts, statistics), [swarm/docs/gateway.md](../swarm/docs/gateway.md) (real models), [bench/protocol/README.md](../bench/protocol/README.md) (the live-model task protocol) and the two replay studies under [bench/studies/](../bench/studies/).

## Three kinds of measurement

| Kind | Model | Answers | Does not answer | Where |
|---|---|---|---|---|
| **Runtime cost** | Scripted: a local deterministic server replays each task's `script.json` through every harness's own native tools | How much CPU, memory and time a harness costs to keep alive and finish verified work; how many fit on one machine | Anything about coding ability; model latency; large contexts | `swarm run` (density, tier-0 packs), the lanes |
| **Controlled replay** | A replay server that answers only the exact expected request history | Engine and tool-bridge overhead with every request content-checked: cgroup CPU per turn or per tool, summed PSS | Task quality; anything outside the replayed trace | `bench/studies/runtime-replay`, `bench/studies/tool-replay` |
| **Live tasks** | A real model behind a strict relay | Footprint and pass rate on small real tasks with real model behaviour, per harness, at ×1 and ×8 | A capability ranking (four small tasks); provider-side variance | `bench/protocol` (shared-v3), Swarm's `relay` source |

A scripted run measures the runtime, not the model. It is the right instrument for density ("how many Antes fit on 2 vCPU / 8 GB") and the wrong one for "which agent codes better". The live cohorts deliberately use small tasks with visible unit tests: their question is cost and reliability under a real model, not skill.

## What counts as a pass

An attempt passes only when the agent exited by itself with status 0, every model request completed, the hidden tests passed on a private copy of the workdir with a fresh HOME, only allowed paths changed, nothing changed after exit, and no process outlived the attempt. The live-task worker applies the same idea with per-task checkers run after the agent exits, a 600 s task deadline and a 30 s checker deadline. Failures are classed (`timeout`, `oom_killed`, `agent_error`, `descendant_leak`, `request_budget_exhausted`, …) and kept.

## Rules that apply everywhere

1. **Freeze before running.** A Swarm spec becomes a hashed `plan.json` (tasks, harness binaries, settings); a bench protocol is a hashed `protocol.json`; each replay study freezes its `protocol.json` before the measured campaign. A change makes a new plan or cohort.
2. **Configuration is not evidence.** Model, effort and output cap are checked on the wire for every request, by the scripted model or by the relay, and a mismatch is refused (422), never silently served by another model.
3. **No retries, no dropped losses.** A failed or refused attempt is a row. A rerun is a new row; the old one stays. Calibration failures and invalid attempts are kept as separate evidence.
4. **Record what you could not control.** Unknown values stay `null`, measurement scope (rusage vs cgroup vs PSS) is written beside every memory figure, and caveats ship inside the data files themselves.
5. **Hash-chained output.** Swarm bundles chain every attempt record and `swarm verify` recomputes the summary from the records; bench receipts record the sha256 of the worker, relay and protocol that produced them.

## Fairness between harnesses

- **Same work.** Same inputs, same limits. Every harness gets the same task pack, prompts, deadlines, request budget, and output cap.
- **Native tools.** The scripted model drives each harness through its own tools (Ante `Read`/`Write`/`Bash`, Hermes `read_file`/`write_file`/`terminal`, and so on). The cost we measure is the harness's own.
- **Off-switches recorded.** Telemetry, analytics, auto-update, auto-memory, skills, and project-doc discovery are turned off wherever a harness allows it. Each of these settings is a required, recorded option in the plan. Ante's telemetry setting counts as a cohort boundary. Where a harness can't match (Codex CLI's JavaScript "code mode" exec tool on GPT-6 models), the data says so: "Codex as shipped," not a tool-matched comparison.
- **Every model slot pinned.** Background and sub-agent models (Claude Code's small/fast and sub-agent slots) are pinned to the benchmark model. A stray request fails loudly at the relay.
- **Effort is a label, not an amount.** "medium" maps to `reasoning.effort` on GPT and to adaptive thinking with `output_config.effort` on Claude. The relay overwrites both on every request and records requested vs. forced.
- **Context hygiene.** Sandboxes live under a clean temp root. No harness reads instruction files (`AGENTS.md`, `CLAUDE.md`) that the others can't see.
- **First-request canary.** Swarm hashes the normalised system prompt, tools, and injected text of each harness's first request. If a harness's canary changes between runs, it starts a new cohort. If it's unstable within a run, it gets no drift verdict.

## Comparing machines: control, quiet gate, statistics

Races run each harness on its own machine (a lane). So a raw number mixes the harness with the machine.

- **Lane control.** Before every harness run (or after it, in a seeded order), the lane runs `mini` on `tool-loop-50` inside the same job. Time metrics get reported raw and as a ratio to the lane's own control. Memory is reference only, since bytes don't scale with CPU speed. A lane with a failed or noisy control is shown raw and not ranked.
- **Quiet gate.** Checked before a run and before its control: no swap, other-CPU under 0.10 cores over a 5 s window, available memory back at baseline, disk headroom, no leftover agent processes or sandboxes, and normal memory pressure. `enforce` refuses the run (`not_quiet`) instead of flagging and running. `observe` only records.
- **Admission.** During a run, each launch waits for memory, pressure, task-slot, and disk headroom. Holds get recorded with their reason and wait time.
- **Repetitions.** The unit is the repetition, meaning a whole lane run. Harness A and B pair up by repetition index. The statistic is the median of paired differences, with a 95% percentile bootstrap interval (10,000 resamples, seeded by race id). An interval containing 0 counts as a tie. With fewer than 6 pairs, the rule becomes a 10% gap, and the page names the rule.
- **Cohorts.** A 12-hex hash covers preset, size, protocol, model, effort, task hashes, harness binary, adapter, canary, control key, gate settings, and `VERSION`. Rows get pooled, ranked, and tested only inside one cohort. When a change lands, older rows get marked `superseded_by`. Rows older than 60 days are `stale`. A measurement-code change bumps `VERSION`, which supersedes every lane.

## Live-task cohorts (bench)

- 32 attempts per cell (four toolbox tasks × 8), run at ×1 and ×8 concurrent instances on one 2 vCPU, 8 GB VM. Cells run serially and never overlap.
- Three repeats per cell. Cell order is `repeat → model → level → harness`, and the harness order rotates by repeat.
- Each attempt gets a fresh home and workspace. RSS of the whole process tree is sampled every 250 ms.
- The relay forwards to a managed model gateway that authenticates the host. Harness credentials are dummies and never get forwarded.
- `build-cohorts.py` reads every number from the receipts and cross-checks them against the progress logs. Any disagreement stops the build. Each run gets reported on its own, with the median of three and the range from fastest to slowest repeat. That range is not a confidence interval.

## Replay studies

- A private network namespace with loopback only, fresh homes and workspaces, dummy credentials.
- Before answering each request, the replay server checks the full system/user/assistant/tool history, model label, effort and output budget. Skipped history or an unexpected tool fails the cell.
- CPU is the cgroup v2 total for the engine's group, including exited descendants (`accounting_probe.py` checks it against a known 500 ms child). Memory is summed PSS, sampled every 100 ms and at a final barrier. The driver, server and sampler run outside the measured groups; their CPU is reported separately.
- The text study runs six full engine-order permutations at 1/2/4/8 instances (72 cells). The tool study runs four balanced Williams orderings of four variants per lane (128 cells). Paired regressions are flagged per metric; there is no composite winner, and four repeats do not establish significance.

## What is not controlled

- Provider routing, backend snapshot, upstream caching and rate limits in live cohorts.
- Observer contention on a 2 vCPU guest (sampler, relay and driver share the CPUs), which mostly affects wall time.
- Process-tree RSS double-counts shared pages; PSS sampling can miss short peaks.
- One machine shape (2 vCPU, 8 GB). Results on larger hosts may differ.
