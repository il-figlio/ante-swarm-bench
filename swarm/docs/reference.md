# Swarm reference

What a run measures, the trust model, how to write a task, spec settings, lane runs and the export. For the architecture and concepts, see [../DESIGN.md](../DESIGN.md). For the lane evaluation method, see [evaluation.md](evaluation.md).

## What a run measures

Each harness (`ante`, `hermes`, `mini`) runs at every concurrency level, in a seeded, interleaved order. In ramp mode the levels climb in sequence, and each harness stops at its own stop rule.

**Pass** requires all of these:
- the agent exited on its own with status 0;
- every model request completed and the script's final step was served;
- the hidden tests passed;
- only allowed paths changed;
- nothing changed after the agent exited;
- no process outlived the attempt.

**Reported for each harness and level:**
- verified successes per minute;
- pass rate with a Wilson interval;
- largest-process memory (kernel rusage), plus sampled process-tree memory. The tree figure is used only for admission, and it counts shared pages more than once;
- combined memory for all agents, written to `samples.jsonl` as `agents_mem_mb`. The scope is the runner's cgroup where one exists, with shared pages counted once. Otherwise it is summed PSS, or summed RSS if neither is available. Summed RSS is marked as overcounting;
- disk each agent wrote between launch and exit (`disk_kb`), excluding the runner's own setup (`disk_kb_setup`);
- CPU per success;
- admission holds;
- every failure, with its class.

**Ramp mode** also checks, after each wave, that nothing survived and that memory dropped back to its earlier level.

**The `scripted` model** replays each task's `script.json` through each harness's native tools. Ante uses `Read`/`Write`/`Bash`, for example, and Hermes uses `read_file`/`write_file`/`terminal`. It measures runtime cost and capacity, with no spend and no network. It says nothing about model capability. For real models, use the `relay` model source instead; see [gateway.md](gateway.md).

**Startup and per-step timing.** The scripted model shares the runner's clock, so each attempt also records:
- `first_request_s`: launch to first model request (cold start);
- `step_gap_s_median` and `step_gap_s_p90`: one reply to the next request. This includes running the requested tool;
- `step_gap_s_early` and `step_gap_s_late`: the first and last 25 gaps, once a session is long enough;
- `context_slope`: bytes of growth per step, after the first request's schemas and prompt are subtracted;
- `after_final_s`: final reply to exit;
- `model_turns` and `probes`: model requests and client probes, counted separately. Probes and `count_tokens` calls do not count against the request budget.

Each pack's primary metric is the question that pack asks (`first_request_s`, `step_gap_s`, or `context_slope`). `successes_per_minute` stays blank when a wave did not reach its size, or when any agent waited to be admitted. The report adds a timing table when timing data exists. Older bundles keep their old summary shape.

**Output cap on the wire.** A request that asks for more output than the plan allows is rejected. A request that asks for less is served, but it is flagged and counted per attempt as `cap_lowered`. Ante 0.3 lowers its own cap as it nears its context limit.

### Tier 0 packs ($0, harness only)

`python3 tools/make_tier0_tasks.py` generates these deterministically. A test checks that the committed copies match. Run them with `examples/tier0-*.toml`. These packs measure harness overhead only. The script writes the known solution itself, so a pass shows conformance. It does not show that a harness solved the task.

Every pack except `startup` has two protocols over the same files. `naive` makes one tool call per step (`script.json`). `collapsed` runs one program that produces the same files (`script.collapsed.json`). Set `protocol` in the spec. A collapsed spec needs every task to have the collapsed script. `mini` is the floor in the local examples and stays off any winner board.

| Pack | What it isolates | Primary metric |
|---|---|---|
| `startup` | One request, no tools: cold start and exit | `first_request_s` |
| `trivial` | nanocode's "million subagents" child: 3 requests, 2 writes, no shell | `successes_per_minute` |
| `tool-loop-50` | 50 trivial shell steps: what the harness adds per step | `step_gap_s` |
| `session-250` | 250 steps: drift in step time and resent context over a long session | `context_slope` |
| `triage-20`, `triage-60` | Logs of about 8.7 KB; find the file with the most ERROR lines. Naive makes one read per file, so the byte slope shows how much context each harness resends | `context_slope` |

**Failure classes**, in priority order:

| Class | Meaning |
|---|---|
| `infra` | The swarm itself failed during the attempt. `infra_reason` gives the cause (`disk_full`, `setup_failed`, `runner_error`) and `infra_stage` gives the stage. This is never blamed on the harness. An agent that failed while the disk was full is recorded here, and its own outcome is kept as `harness_failure` |
| `admission_timeout` | The attempt was never admitted |
| `cancelled` | The run was cancelled |
| `timeout` | The agent hit its time limit |
| `output_limit` | The agent produced too much output |
| `descendant_leak` | A process outlived the agent |
| `agent_error` | The agent exited with a nonzero status |
| `request_budget_exhausted` | The agent used up its model-request budget |
| `request_error` | A model request was rejected |
| `request_incomplete` | A model request did not complete |
| `script_unfinished` | The script's final step was never served |
| `late_change` | Files changed after the agent exited |
| `check_failed` | The hidden tests failed |
| `off_target` | A path outside the allowed list changed |

## Trust model

**The process sandbox is not a security boundary.** Each attempt gets:
- a fresh HOME and workdir under a clean temp root;
- a clean environment with no operator credentials;
- its own session;
- an unguessable model URL.

Every attempt still runs as the same user, on the same filesystem, network and signals. A hostile agent can read your files, interfere with other attempts, or hide a process from the leak sweep (for example, `env -i` plus `setsid`). Only use it for agents you trust. Run untrusted agents in a VM or container until the namespaced and container sandboxes land.

Within that limit, Swarm makes cheating hard and visible:
- **Hidden tests:** tests are copied in only after the agent exits, onto a private copy of the workdir.
- **Clean check environment:** the check runs with a fresh HOME and Python's user site-packages disabled.
- **Full change snapshot:** the snapshot includes symlinks, modes and hidden files.
- **Late changes:** files changed after the agent exits fail the attempt.
- **Leaks:** processes still alive in the group, or found by the attempt's environment tag, fail the attempt and are stopped.
- **Bounded waits:** a process that keeps the output pipe open cannot stall the run.

**What `swarm verify` guarantees.** It catches corruption and inconsistency:
- missing, changed or extra files;
- a broken attempt chain;
- cells or attempts that don't match the frozen plan;
- a summary that doesn't follow from the records.

Bundles are not signed yet. `verify` cannot tell who produced a bundle, since anyone with write access can build a consistent fake. Bundles contain raw agent output (`logs/`) and absolute paths from the machine that ran them. Review them before you share them.

## Writing a task

```
tasks/<id>/task.toml       [task] id, allowed_changes (required)
                           [agent] timeout_sec    [verifier] timeout_sec (default 120)
tasks/<id>/instruction.md  the prompt, identical for every harness
tasks/<id>/workspace/      starting files
tasks/<id>/tests/test.sh   runs after the agent exits, on a copy of the workdir; exit 0 = pass
tasks/<id>/script.json     steps for the scripted model: read, write, bash, final
tasks/<id>/script.collapsed.json   optional: the same file effects in one program
```

- **Allowed changes:** `allowed_changes` takes globs. `*` stays inside one path segment, and `**` spans directories. Use `["**"]` to allow any change.
- **No symlinks:** task directories can't contain symlinks. The task hash doesn't cover what they point to.
- **Test environment:** tests see `SWARM_WORKDIR` and `SWARM_TESTS_DIR`.

## Spec settings

`swarm plan` rejects unknown keys. Any setting that changes results has to be stated explicitly for each harness:

- **Ante telemetry:** `[harness_options.ante] telemetry = "on" | "off"`. `telemetry_env` sets a synthetic `swarm-bench-*` label and is required when telemetry is on. Ante exports telemetry by default. With it on, Ante 0.2.9 took about 2 s extra to exit. Ante 0.3.0 adds about 0.3 s on exit, but runs 4 extra threads per instance.
- **Hermes home:** `[harness_options.hermes] home = "cold" | "prepared"`. A fresh Hermes home installs about 1.5 GB of dependencies on first use. With `prepared`, `seed` points at a prepared home that has no credentials or symlinks. Hermes's turn cap comes from the pack's request budget (300 if the run doesn't set one). Stopping the gateway happens after the attempt is timed, so that stop isn't counted in `after_final_s`.

Other settings have defaults, like admission reserves (memory, task slots and, with `reserve_disk_mb`, free disk), timeouts and the sample interval. Each default gets written into the frozen plan, so the bundle records what was actually used.

## Live runs

`swarm serve` runs preset swarms for visitors. Visitors can choose only presets and sizes. The model is scripted by default, and only one run goes at a time. Every run is sealed and verified. One machine running one `swarm serve` for one harness is a **lane**. See [serve/README.md](../serve/README.md).

The service resets the machine after every run and once at startup. It removes the sandbox roots runners made. It stops processes runners started, found by the `SWARM_ATTEMPT` variable in their environment, never by user. Then it checks that disk, processes and memory are back near idle. The record goes to `<data>/lane-reset.json` and into the run's directory. A lane whose reset failed answers launches with 503 until a reset succeeds. To run it by hand:

```
PYTHONPATH=src python3 -m swarm.cli lane-reset --data <data dir>    # exit 0 clean, 1 not; prints the record
```

### What a lane run records

Each lane run (`run.json` in the lane's data dir) also records the state of the machine at the time:

- **`gate`**: one record per stage (`control`, `run`) from the quiet gate. It covers swap in use, other CPU (cores, over a 5 s window), memory against the lane's baseline, free disk, leftover agent processes and sandboxes, memory pressure, the limits, and whether the lane was quiet. Written to `gate.json` and `<data>/gates.jsonl`.
- **`control`**: the lane control. It is `mini` on `tasks/tool-loop-50`, size 1, 3 rounds, fixed seed 1, run on the same machine in the same job. It records its state (`done`, `failed`, `skipped`, `off`) and a `key`, which decides what counts as comparable: preset, task, `mini`'s agent file, Python version, `VERSION`. It also records the per-round values and medians (`first_request_s`, `step_gap_s`, `cpu_s`, `wall_s`, `rss_mb`, `setup_s`, `request_bytes_first`). Finally it records `noisy`, the IQR divided by the median of its step gap, flagged when it is over 20%. A failed control does not stop the run. The run gets the warning `control_failed`.
- **`cohort`**: 12 hex characters naming which runs this one can be compared with. Rows are pooled, ranked or tested only inside one cohort. See [evaluation.md](evaluation.md#cohorts-rerun-and-supersede).
- **`canary`**: three hashes of the first model request, with volatile text removed (tools, system prompt, injected text). It also says whether they changed since the lane's last run (`changed`, `parts_changed`, `binary_changed`), and raises the warning `harness_changed` if so. `stable: false` means the attempts in one run disagreed. In that case there is no verdict.
- **`series`**: `{race, rep, of}` when the run is one repetition of a race.
- **`result`** also adds `cpu_s_median`, `disk_kb_median`, `batch_wall_s_total`, `agents_mem_mb_peak`, `mem_per_agent_mb` and the `first_request_*` medians.

Under one lock the order is gate, control, reset, gate, run, reset. A `control_last` run flips the middle part: gate, run, reset, gate, control, reset. The terminal state comes after the control. The states are `queued`, `checking` (the gate), `control`, `running`, then `done` or `failed`. In `enforce` mode, a lane that is not quiet ends `failed` with `refused: "not_quiet"`.

**First request.** Each attempt records `requests.first_request`. It holds the bytes of the first model request, split into system prompt, tool definitions, user text (the task prompt and injected text) and the rest, plus the tool count. Scripted runs report bytes, and bytes / 4 labelled synthetic. They do not report provider token counts. Every request row also has a `request_id` (`<attempt>#<index>`) and a `prev_request_id`. For Anthropic it also has `cache_write_input_tokens`, which is null where a wire does not report it. Only sizes, counts, tool names and hashes are kept. Prompt text is never kept.

**Quiet gate and control flags** for `swarm serve`:

| Flag | Default | Meaning |
|---|---|---|
| `--gate off\|observe\|enforce` | `observe` | `observe` takes one reading per stage and records it. It never holds, refuses or resets. `enforce` polls for up to `--gate-wait-s`, calls one reset if leftovers are the cause, then refuses the run |
| `--gate-quiet-s` | 5 | seconds of CPU the gate averages over |
| `--gate-cpu-cores` | 0.10 | most other CPU (cores) a quiet lane may show |
| `--gate-max-swap-mb` | 0 | most swap in use a quiet lane may show |
| `--gate-wait-s` | 120 | how long `enforce` waits before it refuses |
| `--gate-allow-unknown` | off | let unknown readings pass |
| `--control on\|off` | `on` | run the lane control with every run |

As a library, `Service(...)` defaults to gate `off` and control off. Only the `serve` command turns them on. Every setting is written into `run.json`.

**Repetitions and ties (the front).** `POST /api/races` takes `repeats` of 1, 6 or 10. The series driver launches repetition k on every chosen lane together, waits, and only then moves on. Each repetition is a normal lane run with its own control, gate and bundle. Nothing is retried. A refused or failed repetition stays as a row and lowers n. A visitor may ask for 6 only on a harness preset at size 1. Other series need the operator passkey (`SWARM_FRONT_PASSKEY`). A front restarted mid-series loads the race as `interrupted` and never resumes it. `GET /api/races/<id>/stats` gives the statistics, and `?rep=k` selects a repetition's agent. The unit is the repetition.

The statistic is the median of paired differences. It comes with a 95% percentile bootstrap interval: 10,000 resamples, seeded by the race id. The resample medians are drawn from their exact rank distribution, so the result is deterministic and fast. A tie is an interval that includes 0. Below 6 valid pairs the rule is the 10% gap, and the page names which rule it used. Time metrics are relative to each lane's own control. Memory and bytes are raw. Wall is normalised at size 1 only.

### Exporting numbers

```sh
PYTHONPATH=src python3 -m swarm export --front-data <front data dir> -o <out>   # numbers-only tree
python3 tools/rebuild_export.py <out>                                          # recompute every summary.json
```

The export tree has `v1/index.json` (races, cohorts, `superseded_by`, `stale`). Each race gets `manifest.json`, `rows.jsonl`, `requests.jsonl`, `gates.jsonl` and `summary.json`. It holds only numbers, codes, hashes and tool names, filtered by an allow-list. No paths, hostnames, logs, tool arguments or prompt text get in. `rebuild_export.py` must reproduce each `summary.json` byte for byte. A lane also serves one run's document at `GET /api/runs/<id>/export`.

Method, data formats and rerun rules are in [evaluation.md](evaluation.md). Any change to measurement code needs a `VERSION` bump (see the cohort rules).
