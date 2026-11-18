# swarm serve: live runs for visitors

`swarm serve` lets people launch a real swarm from a web page without giving them a shell. One machine running `swarm serve` for one harness is a lane; `swarm front` races several lanes (see [../docs/evaluation.md](../docs/evaluation.md)).

- **Presets only.** A visitor picks a preset id and size from `presets.json`. Task paths, commands and model settings are fixed there; nothing in a request reaches a file path or process argument.
- **Scripted model by default.** Agents are real harness processes in their own sandboxes, driven by the scripted model. There is no LLM spend or outbound network for agents. Real models need `--relay-upstream` or `--gateway-upstream` and an allowlist (see [../docs/gateway.md](../docs/gateway.md)). Runs measure the runtime—memory, processes, admission and clean wind-down—not coding ability; the page says so.
- **One run at a time.** There is a short queue (`--max-queue`, default 5) and a per-client hourly cap (`--per-client-hour`, default 3). With `SWARM_PASSKEY` set, a request carrying that passkey gets 5 more launches an hour; wrong guesses are capped at 10 an hour per client. Admission still holds agents back when the machine is short of memory or process slots.
- **Every run is a normal bundle.** After a run finishes, its bundle is sealed and verified (`swarm verify`). It is kept until `--keep` newer runs push it out.
- **Ingress.** With `SWARM_INGRESS_SECRET` set, every `/api` call except `/api/healthz` needs a matching `X-Swarm-Ingress` header, which a reverse proxy in front adds (`nginx.conf.example`). Without the secret, only loopback is served.

API (JSON): `GET /api/presets`, `GET /api/status`, `GET /api/tasks` (every task a preset uses: title, instruction, hidden `tests/test.sh`, allowed changes, workspace file names and sizes, and which presets use it; `script.json` is never exposed), `GET /api/ledger?limit=N` (default 50, max 500, newest first; see below), `POST /api/runs {"preset","size"}` → `202 {"id","position"}` (optional `series` and `control_order`, below), `GET /api/runs/<id>`, `GET /api/runs/<id>/events` (server-sent events: run state, queue position, admitted and finished counts, the latest host sample, new attempts, and console lines for every admission, tool call, reply and verdict, read from Ante's own event log), `GET /api/runs/<id>/agents/<n>` (one agent's command, prompt, tool calls with inputs and outputs, reply, and the hidden test with its output) and `GET /api/runs/<id>/spec`.

All of it passes through `live.clean()`. Sandbox paths become relative, the per-attempt model URL (which carries a token) and home directories are removed, and Ante's SessionStart event is never shown.

**Run states.** `queued → checking → control → running → done | failed`. `checking` is the quiet gate. With `control_order: "control_last"`, the run goes first and the control follows; the terminal state is set after the control.

**Quiet gate and lane control.** One run holds the lane's lock through gate, control, reset, gate, run, reset (or gate, run, reset, gate, control, reset). The control is `mini` on `tasks/tool-loop-50`, size 1, 3 rounds, fixed seed 1 (preset `lane-control`, `kind: "control"`; it is never listed to visitors and gets no control itself). A failed control does not stop the run. In `--gate observe` (the `serve` default), the gate records one reading per stage and never refuses. In `--gate enforce`, a lane that stays busy for `--gate-wait-s` ends the run `failed` with `refused: "not_quiet"` and the public error "This lane was not quiet enough to measure: ...". The library defaults to gate `off` and control off.

**API additions.**

- `POST /api/runs` accepts `series: {"race": <12 hex>, "rep": k, "of": n}` (`n` at most 64) and `control_order: "control_first" | "control_last"` (default first). Anything else in either is a 400. The front sets both for a repetition.
- `GET /api/status` adds `gate` (the last gate record), `control` (the last control's `key`, `median`, `at`, `state`), `canary` (`{<harness>: sha12, since}`) and `settings` (`{gate, control}`).
- `GET /api/runs/<id>` and `/events`: the run carries `gate`, `control`, `series`, `cohort` and `canary`; each attempt carries `first_request` (sizes, tool count, canary hash).
- `GET /api/runs/<id>/export`: the numbers-only document for that run (needs the ingress secret like every other route). It contains allow-listed keys only: no paths, hostnames, logs or prompt text.

Flags: `--gate off|observe|enforce` (default `observe`), `--gate-quiet-s`, `--gate-cpu-cores`, `--gate-max-swap-mb`, `--gate-wait-s`, `--gate-allow-unknown`, `--control on|off` (default `on`). These go into each run's `run.json`. See [../docs/reference.md](../docs/reference.md) for the values and [../docs/evaluation.md](../docs/evaluation.md) for the reasons.

**Ledger.** When a run finishes, whether done or failed, one compact line is appended to `<data>/ledger.jsonl`: id, preset, title, size, created/started/finished, state, verify, chain head, plan sha256, harness version, and the result numbers (passes, attempts, max running, successes/min, median wall, median peak RSS, CPU s per success, admission wait), plus `cohort`, `control_key`, `gate_ok`, `refused` and `series`. It survives `--keep` pruning and holds nothing about the visitor. `bundle_kept` on each entry says whether the full run still exists. If the ledger is missing at startup, it is rebuilt from the `runs/*/run.json` files that are done or failed.

Local try-out:

    PYTHONPATH=src python3 -m swarm.cli serve --presets serve/presets.json --data /tmp/swarm-live --telemetry off
    curl -s -XPOST localhost:8080/api/runs -H 'content-type: application/json' -d '{"preset":"mixed","size":32}'
    curl -sN localhost:8080/api/runs/<id>/events

## Deploying a lane (generic)

`swarm-serve.service.example` is a systemd unit. The user, paths, and binaries are placeholders.

1. Copy this app directory to the lane machine, say `/opt/swarm`. Then install the harness binaries the presets use.
2. Put `SWARM_INGRESS_SECRET=<random>` in an environment file, and add `SWARM_PASSKEY` too if you want it. Only root should be able to read the file (mode 600), and `EnvironmentFile=` should point to it.
3. Install the unit and run `systemctl daemon-reload && systemctl enable --now swarm-serve`.
4. Put a TLS reverse proxy in front to add the ingress header (`nginx.conf.example`). That way browsers never see the secret. Requests without it get a 403.

Maze cells under cgroup limits need cgroup v2 delegation. Set `Delegate=yes`, which the example unit has commented out. Run output stays in the `--data` directory, outside the source tree.
