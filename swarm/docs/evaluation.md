# Lane evaluation method

How a lane run is made comparable: the lane control, the quiet gate, first-request anatomy and the canary,
repetitions and ties, cohorts, and the data formats and API that carry them. Paths are relative to this app
directory. The method borrows from ADEMark v1.0 (github.com/zqkra/ademark: METHODOLOGY §5–6, TESTS §0.1 and
Track B, RUNBOOK §7–8).

A **lane** is one machine running `swarm serve` for one harness. A **race** starts the same preset on several
lanes at once through `swarm front`. Because each harness runs on its own machine, a raw number mixes the harness
with its machine; the control, the gate and the canary exist to separate them.

## Scope and limits

- No top-up or retry of a failed repetition. A refused or failed repetition is a recorded row and lowers n.
- No provider token counts per segment (that needs `count_tokens`, a model call). Scripted runs report bytes and
  bytes/4, labelled synthetic. Relay rows keep the provider's whole-request `input_tokens`.
- No grades and no overall winner. `swarm run`/`plan` semantics and in-run admission rules are unchanged by any
  of this; old bundles still verify.
- No prompt text leaves a lane: only sizes, counts, tool names and hashes.

## Lane control

- The control runs on the lane, inside the run's job. `Service._work` runs `gate → control → reset → gate →
  harness run → reset` under one lock, so nothing else can queue between them and a direct lane launch gets a
  control too.
- The control is preset `lane-control` (`kind: "control"`) in `serve/presets.json`: `mini` on
  `tasks/tool-loop-50`, size 1, 3 rounds, scripted model, fixed seed 1. One attempt yields cold start
  (`first_request_s`), per-step cost (`step_gap_s`, 50 gaps), `cpu_s`, `wall_s`, RSS and the runner's `setup_s`.
  A control at the race's size would double the race and measure Python's contention, not the machine. The older
  `lane-baseline` preset (mini on `tasks/startup`, one attempt) stays in the file for older fronts.
- A failed control does not stop the harness run. It is recorded (`control.state = "failed"`, warning
  `control_failed`), and the lane loses its normalised ranking.
- The control key is 12 hex of sha256 over the control preset fields, task hash, `mini_agent.py` hash, Python
  version and `VERSION` (`Service` in `serve.py`). Two controls are comparable only with the same key.

### Normalisation

Normalise by ratio to the lane's own control, like with like. A slower machine scales time rather than adding a
constant, so differences would be wrong; rescaling to a cross-lane mean would change with which lanes raced.

| Metric (page key) | Treatment |
|---|---|
| Startup `first_request_s` (`start`) | Normalised: ÷ control `first_request_s`. Ranked on the ratio. |
| Step gap `step_gap_s` (`gap`) | Normalised: ÷ control `step_gap_s`. |
| CPU per agent `cpu_s` (`cpu`) | Normalised: ÷ control `cpu_s`. |
| Wall (`wall`) | Normalised ÷ control `wall_s` at size 1 only. At size > 1: reference only. |
| Passed per VM-hour (`vh`) | Reference only (no like-for-like control at size > 1). |
| Memory per agent, largest process, peak memory, disk per agent | Reference only: bytes do not scale with machine speed. The control's value is drawn as the floor. If control RSS differs by more than 10% across lanes, the page says lanes measure memory differently. |
| Context growth, first-request bytes, requests, tokens | Neither: deterministic. The control appears as the floor row in the first-request chart. |
| Passed, failed, not run | Never normalised. |

Raw values with units stay the primary number; the ratio is printed beside it (`×4.2 ctl`). A lane with no valid
control (failed, skipped, noisy: IQR/median of control `step_gap_s` over 20%, or a control key that differs from
the other lanes') shows raw numbers and is not ranked on normalised metrics.

What the ratio does not correct: a native binary and a Python program need not slow down alike on a slow or shared
CPU, so the ratio fixes the first-order difference only. Different Python builds change the control key, so such a
lane is not ranked on normalised metrics.

## Quiet gate

`gate.py`. The gate refuses; it does not flag-and-run. Checks, each read fresh:

| Check | Pass | Hold reason |
|---|---|---|
| Swap in use (`host.memory_state().swap_used_mb`) | ≤ `max_swap_mb` (0) | `swap_in_use` / `swap_unknown` |
| Other CPU: busy cores from `/proc/stat` minus the service's own CPU (`os.times()`), mean over a continuous `quiet_s` window (5 s) sampled each second | < `max_cpu_cores` (0.10) | `cpu_busy`; `cpu_steal` when steal alone is over the limit; `cpu_unknown` |
| Available memory | ≥ `baseline_available_mb − tolerance_mb` (the reset's own baseline and tolerance) | `memory_below_baseline` / `memory_unknown` |
| Disk free on sandbox temp and data | ≥ max(`reset_min_free_mb`, `reserve_disk_mb`) | `disk_low` |
| Leftover agent processes and sandbox roots (`reset.agent_processes(own=True)`, `reset.runner_roots`) | 0 | `agent_processes_left`, `sandbox_roots_left` |
| Memory pressure | `normal` | `pressure_<level>` |

Recorded but not gated: `/proc/pressure/cpu` some avg10, `steal_cores`. A failing gate polls for up to `wait_s`
(120 s); if leftovers are the reason it calls `Service.reset_lane(None)` once. Still failing, the run is refused
(`state = "failed"`, `refused = "not_quiet"`) and written to the ledger like any failure.

Modes, a recorded setting: `off | observe | enforce`. `observe` records the verdict and never refuses, so
thresholds can be set from data. Unknown readings hold unless `allow_unknown`. `Service(...)` as a library
defaults to `gate="off"`, `control=False`; the `serve` CLI defaults to `--gate observe --control on`.

Reset is clean after, the gate is quiet before, admission is headroom during. The gate is not part of
`runner.Admission`, which works per launch inside a frozen plan.

## First-request anatomy and canary

Anatomy is computed in the model server, sizes only (`anatomy.py`, called from `ScriptedModel._admit` on the first
model row of an attempt and shared by `RelayModel`). Segments by wire:

- Responses: `instructions` and `input` items with role system/developer → system; `tools` and `additional_tools`
  → tools; role user → user.
- Messages: `system`, `tools`, `messages` with role user.
- Chat: `messages` with role system/developer, `tools`, role user.

Sizes are bytes of `util.canonical()` of each segment; `other_bytes` is the canonical body minus the three.
`instruction_bytes` is the task's `instruction.md` when found verbatim in the user text, else null;
`injected_bytes = user_text_bytes − instruction_bytes`.

The canary is three part-hashes of the first request with volatile text normalised (sandbox and temp paths, home,
dates, times, 32-hex tokens, UUIDs; the task prompt replaced by `<instruction>`): `tools_sha256`,
`system_sha256`, `injected_sha256`, plus sorted top-level parameter names and the User-Agent. A harness whose
canary differs between attempts of one run is `stable: false` and gets no drift verdict.

## Repetitions and ties

- Repetitions are driven by the front, one lane run per repetition. `POST /api/races {repeats: N}` returns 202 at
  once; a driver thread launches repetition k on all chosen lanes together, waits, records and goes on. Each
  repetition is a normal lane run with its own control, gate, sealed bundle and ledger line.
- On a lane the only sequential pair is control and harness run; per repetition the order (`control_first` or
  `control_last`) is drawn from `random.Random(race id)` and recorded. Cells inside a bundle are already
  seeded-shuffled.
- The unit is the repetition. A repetition's value is the lane run's median over attempts; for normalised metrics it
  is the ratio to the same repetition's control on the same lane. Harness A vs B pairs by repetition index.
- The statistic is the median of paired differences; the tie rule is its 95% percentile bootstrap interval
  (10,000 resamples, seeded by race id) including 0 (`stats.py`). Below 6 valid pairs the rule is the 10% gap, and
  the page names the rule it used. With fewer than 6, a bootstrap interval excludes 0 whenever all differences share
  a sign, and a sign test cannot reach p < 0.05.
- Statistics are computed once, in Python, so the export can rebuild the same numbers.

## Cohorts, rerun and supersede

A `cohort` is the first 12 hex characters of a sha256 computed over these inputs: preset id, size, protocol (`naive` or `collapsed`), model, effort, task hashes, harness binary sha256, harness options, `adapter_sha256` (the harness's adapter file plus `harnesses/base.py`), canary (three part-hashes), control key, gate settings, and `VERSION`.

1. No run replaces an existing result. A new run adds a new row, and the old row stays.
2. Compare rows only within one cohort. Pooling, ranking, and tests never cross a cohort boundary.
3. Changing the harness, its adapter, its canary, the control key, or `VERSION` starts a new cohort for that lane on its next run. Older rows for the same lane, preset, size, protocol, and model get marked `superseded_by: <cohort>`. They stay published, but they never enter a median.
4. Any change to measurement code must bump `VERSION`: `runner`, `sandbox`, `host`, `scripted`, `relay`, `gate`, and `stats`. A bump supersedes every lane.
5. A series whose lane switches cohorts midway gets no statistics for that lane (`cohort_changed`). The rows themselves stay.
6. Rows older than 60 days get marked `stale`.

Both marks are computed at export time in `v1/index.json`, and neither one edits a bundle, so bundles still verify. A row can carry both.

Example: a lane's harness updates itself after repetition 3 of 6. Reps 1–3 share cohort `c1`, and reps 4–6 land in `c2`. All six rows stay published, and that lane gets no statistics for the series. The next series, run on binary B, lands in `c2`, and the older `c1` rows for the same lane, preset, size, protocol, and model get marked `superseded_by: c2`. Its statistics use only its own rows.

## Data formats

### Request rows: `bundle/logs/<attempt>/requests.jsonl` and `requests.live.jsonl` (`scripted.py`, `relay.py`)

| Field | Where set | Meaning |
|---|---|---|
| `request_id` | `_admit`, `_probe` | `"<attempt>#<index>"`. Always present. |
| `prev_request_id` | `_admit` | The row whose response id equals the body's `previous_response_id`; else the latest earlier completed model row with the same `helper` flag; null for the first. |
| `prev_source` | `_admit` | `"previous_response_id"`, `"sequence"` or null. |
| `upstream_id` | `relay.py` `_finish` (from the reply `id`, `response.created` or `message_start`) | Provider response id; null when not seen. Scripted: its own `resp_…`/`msg_…` id. |
| `user_agent` | `_admit` | Request header, cut to 120 chars. |
| `cache_write_input_tokens` | `relay.usage_of` | Anthropic `cache_creation_input_tokens`; null on wires that do not report it. `input_tokens` keeps its meaning (fresh + read + written). |
| `anatomy` | `_admit`, first model row only | `{wire, canonical_bytes, system_bytes, tools_bytes, user_bytes, user_text_bytes, instruction_bytes, injected_bytes, other_bytes, tool_count, mcp_tool_count, canary: {sha256, tools_sha256, system_sha256, injected_sha256, params_sha256}}`. |

### Attempt record and summary (`runner._requests`, `summary.summarize`)

- `requests.first_request`: the row's `anatomy` plus `request_bytes`, `tools_offered`, and (relay) `input_tokens`.
- `requests.cache_write_input_tokens`: sum, or null when no row reported one.
- Summary group, only when attempts carry the fields (so old bundles verify): `first_request_bytes_median`,
  `first_request_{system,tools,user,instruction,injected,other}_bytes_median`, `first_request_tool_count_median`,
  `canary_sha256` (the commonest), `canary_distinct`, `cache_write_input_tokens_total`.

### Lane run directory `<data>/runs/<rid>/` (`serve.py`)

- `gate.json`: list of gate records, one per stage (`"control"`, `"run"`): `{stage, at, mode, ok, held_for[],
  wait_s, quiet_s, values: {swap_used_mb, cpu_cores_other, cpu_cores_self, steal_cores, psi_cpu_some_avg10,
  pressure, available_mb, baseline_available_mb, disk_free_mb, agent_processes, sandbox_roots}, limits:
  {max_swap_mb, max_cpu_cores, tolerance_mb, min_free_mb, wait_s}, unknown[]}`.
- `control/spec.toml`, `control/plan.json`, `control/bundle/` (sealed, verified), `reset-control.json`.
- `run.json` carries:
  - `gate`: the public form of `gate.json` (numbers and codes).
  - `control`: `{state: done|failed|skipped|off, reason, key, order, harness: "mini", harness_sha256, python,
    plan_sha256, verify, attempts, passes, values: {first_request_s[], step_gap_s[], cpu_s[], wall_s[], rss_mb[],
    setup_s[], request_bytes_first[]}, median: {…same keys}, noisy: bool, canary_sha256}`.
  - `series`: `{race, rep, of}` when launched by a series; `cohort`; `adapter_sha256`.
  - `canary`: `{sha256, parts, stable, distinct, changed, parts_changed[], binary_changed}`.
  - `result`: adds `cpu_s_median`, `disk_kb_median`, `batch_wall_s_total`, `agents_mem_mb_peak`,
    `mem_per_agent_mb`, and the summary's `first_request_*` medians.
  - States: `queued → checking → control → running → done | failed` (`checking` is the gate; with `control_last`
    the middle two flip). `refused` may be `"not_quiet"`.
  - `warnings` may include `harness_changed`, `control_failed`, `control_noisy`.
- Lane data dir: `gates.jsonl` (every gate record); `canary.json`
  (`{"<harness>|<wire>|<source>": {sha256, parts, harness_sha256, harness_version, adapter_sha256, since, run}}`);
  `canary.jsonl` (one line per change); `ledger.jsonl` lines carry `cohort`, `control_key`, `gate_ok`, `refused`,
  `series`.

### Front data dir (`front.py`)

- `races.jsonl` race record: `repeats`, `seed`, `rep` (current index) and `reps: [{k, order, started, finished,
  lanes: {<lane>: {run, error, state, result, control, gate, cohort, canary}}}]`. Top-level `lanes` is the current
  repetition. Terminal state `interrupted` means the front restarted mid-series; a series is never resumed.
- `export/v1/races/<race>/rep-<k>/<lane>.json`: the lane's export document, fetched when the repetition ends.
- `export/v1/races/<race>/stats.json`: written once the series ends.

### Export tree (`swarm export`, `export.py`)

```
<out>/v1/index.json                      races, cohorts, superseded_by, stale (older than 60 days)
<out>/v1/races/<race>/manifest.json      preset, size, protocol, model, repeats, seed, harness versions + sha256[:12],
                                         adapter_sha256, canary, control key, gate limits, plan sha256, times
<out>/v1/races/<race>/rows.jsonl         one line per attempt (control attempts carry "control": true)
<out>/v1/races/<race>/requests.jsonl     one line per model request: ids, helper, kind, model, bytes, tokens, timing
<out>/v1/races/<race>/gates.jsonl        gate records
<out>/v1/races/<race>/summary.json       per lane and repetition medians; statistics
```

Only numbers, codes, hashes and tool names get exported, and only for keys on the allow-list; paths, hostnames, logs, tool arguments, prompt text and client data stay out. Running `tools/rebuild_export.py <out>` recomputes every `summary.json` from `rows.jsonl`. The output must match byte for byte.

## API

### Lane (`swarm serve`)

- `POST /api/runs`: optional `series: {race: 12 hex, rep: int, of: int ≤ 64}` and `control_order: "control_first" | "control_last"` (default first). Anything else in them returns 400.
- `GET /api/status`: `gate` (last record, public form), `control` (last `{key, median, at, state}`), `canary` (`{harness: sha12, since}`), `settings: {gate, control}`.
- `GET /api/runs/<id>` and `/events`: `run` carries `gate`, `control`, `series`, `cohort`, `canary`. Each attempt adds `first_request` (sizes, `tool_count`, canary sha). A refused run's public `error` reads `"This lane was not quiet enough to measure: <reasons in words>."`
- `GET /api/runs/<id>/export`: the numbers-only document for that run. Like every route, it sits behind the ingress secret.
- CLI `serve`: `--gate off|observe|enforce`, `--gate-quiet-s`, `--gate-cpu-cores`, `--gate-max-swap-mb`, `--gate-wait-s`, `--gate-allow-unknown`, `--control on|off`. All of them get written into `run.json`.
- CLI: `swarm export --front-data DIR -o OUT`.

### Front (`swarm front`)

- `POST /api/races`: `repeats` ∈ {1, 6, 10}. Visitors can ask for 6 only on a `kind: "harness"` preset at size 1. Anything else above 1 needs the operator passkey, the same `Front.owner()` check and lock that real models use. A series counts as one launch against the hourly cap. It answers 202 without waiting for any lane.
- `GET /api/races/<id>`: `repeats`, `rep`, `reps[]` (per lane: `started`, `error`, `state`, `result`, `control`, `gate`, `canary.changed`). `baseline_s` is still served for old races.
- `GET /api/races/<id>/events`: lane messages carry `rep`. `{rep: k, start: true}` opens each repetition. The stream closes at its 900 s limit with `{more: true}` if the series is still running, and the page reattaches.
- `GET /api/races/<id>/stats`: `{n, rule: "bootstrap" | "gap10", min_pairs: 6, resamples, metrics: {<key>: {relative_to_control: bool, lanes: {<lane>: {values[], n, median, p25, p75, min, max, vs_control: {diff_median, ci95}, excluded}}, pairs: [{a, b, n, diff_median, ci95: [lo, hi], tie}]}}}`.
- `GET /api/races/<id>/lanes/<lane>/agents/<n>?rep=k` (default: latest).
- Everything relayed goes through `Front.scrub`.
