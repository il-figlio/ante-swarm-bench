# Maze tasks

The maze task checks coordination, not just how many agents you run. Does a swarm of agents sharing one map finish a problem that can be split up sooner than a single agent? What does that speedup cost in calls? And at what CPU and memory limits does adding agents stop helping? Code: `src/swarm/maze.py` (generator, service, `./maze` client, explorer policy, lockstep simulation), `src/swarm/limits.py` (cgroup limits), `front-maze/maze-core.js` (the same generator and simulation in JS), `tools/make_maze_tasks.py`, and the sealed `tasks/maze-*`.

## What it measures

On one machine, how much sooner does a swarm of Antes solve a splittable problem than one Ante? What does that cost in calls, and where does adding Antes stop helping as CPU and memory shrink? Also: does a shared map beat the same number of Antes working alone? And does it still win when the number of calls is equal?

## Shape

A **maze task** is a normal task directory with a `[maze]` section in `task.toml`. The maze never sits in the workspace. The runner starts a **maze service** for each cell, and that service keeps the maze in memory. Agents reach it only through `./maze`, a stdlib Python script in the workspace, and through the `.maze.json` the runner writes into each workdir (`{"socket": path, "token": ...}`: the service's Unix socket and this agent's token).

- **Not on disk, not derivable, not stealable.** The sandbox runs as one uid on a shared filesystem. So the service assumes any agent can read whatever another agent or the runner wrote:
  - *Seed.* The runner builds each cell's maze from an effective seed, not the task's seed. It is HMAC-SHA256 of `"<task seed>:<cell index>:<task id>"` under a 32-byte key from `os.urandom`, made once per run, first 4 bytes. The key is never written anywhere. Reading `[maze]` from task.toml or plan.json and calling `maze.generate` gives a different maze. Every cell gets its own maze, so levels are compared over mazes from the same generator, not over one maze. The effective seed is recorded after the cell ends (the cell's `maze.<task>.seed` in cells.jsonl, plus `seed` and `params.seed` in `maze-c<cell>-<task>.json`). Any cell can therefore be replayed and re-simulated with `maze.simulate({**params, "seed": effective}, n)`.
  - *Plain seed.* Setting `maze_seed = "plain"` in the spec (the default is `"secret"`, recorded in the plan) uses the task seed as written. Use it for trusted agents only: the scripted run, which must match the precomputed simulation call for call, and tests. Anyone holding the task can generate a plain maze. The serpentine control draws no random numbers, so its maze is identical in both modes. It has one path, so there is nothing to learn. `call_budget` on maze-budget-shared is one Ante's calls on the plain maze. Under secret seeds it is the same number on a different maze, so "equal cost" holds on average, not per maze.
  - *Tokens.* The service listens on a Unix socket inside its own 0700 temp directory, never a workdir. On each request it reads the peer pid (Linux `SO_PEERCRED`, macOS `LOCAL_PEERPID`). It honours the token only when that pid is the agent's root pid or a descendant of it (parents come from `/proc/<pid>/stat` or libproc). The runner binds the root pid at launch and rebinds the token to the checker's pid for `./maze verdict`. A valid token from any other process gets 403 `forbidden` and is logged as `stolen_token` (with `owner` and `peer`). It changes nothing for its owner. Where peer pids cannot be read, the service does not start. The attempts then fail as infra (`maze_unavailable`).
  - *Log.* The call log stays in memory and is written when the service stops. So no file shows live positions while agents run.

The service records every call as `(agent, t, op, cell, result)`. Time to exit, route, credit, and repeat work all come from that log. None of it comes from agent text.

- The **scripted explorer** is a policy in the scripted model. It reads the last tool result and picks the next `./maze` command. A real Ante process therefore makes real tool calls at zero model cost. Its rule is the prototype's rule. Real models get the same instruction and the same tools.
- **Limits**: each cell's agents run in one child cgroup of the lane service, with `cpu.max`, `memory.max`, and `pids.max` set. The maze service, the relay, and the runner stay outside it. So the limit applies only to the Antes. This requires cgroup v2 with delegation (systemd `Delegate=yes` on the service unit, or a transient `systemd-run -p Delegate=yes`). `swarm probe` reports whether cgroup limits are available.

## Contracts

### `[maze]` in task.toml

```toml
[maze]
cols = 40            # 4..128
rows = 25            # 4..128
seed = 2026
generator = "prim"   # "prim" (many branches) | "backtracker" (long corridors) | "serpentine" (one path: the control)
loops = 0.05         # share of cells that get one extra opening (0..0.3)
start = "center"     # "center" | "corner"
map = "shared"       # "shared" (one map for all agents) | "own" (each agent alone)
step_limit = 4000    # calls per agent before the service refuses further moves
call_budget = 0      # 0 = none; else total calls across the cell (the equal-budget case)
```

The generator has to match `front-maze/maze-core.js` cell for cell with the same parameters. That means the same mulberry32 PRNG, the same edge order, and the same loop pass.

### `./maze` commands (stdout is one JSON object per call)

| Command | Result |
|---|---|
| `./maze look` | `{"at":[x,y],"open":["N","E"],"new":["E"],"exit":false,"steps":n}`. `new` lists open sides into cells not explored (shared: by anyone; own: by me). |
| `./maze move <N\|E\|S\|W>` | Same shape after moving, or `{"error":"wall"}` / `{"error":"step_limit"}` / `{"error":"budget"}`. `exit:true` when standing on the exit. |
| `./maze next-edge` | Shared map only. Nearest open edge over explored cells that no other agent has claimed; claims it for me. `{"path":["N","N","E"],"to":[x,y]}` or `{"path":null}` when none is left. |
| `./maze map` | Explored cells and their open sides, plus claims: what a real model can reason over. |

Every command is one logged call, refused ones included. Each one counts toward the agent's step limit.

### Service log and summary

The file `logs/maze-c<cell>-<task>.jsonl` holds rows `{t, agent, op, arg, at, ok, error?}`. `t` is seconds since the service started. The file is written when the cell ends, including when it ends in an exception. `op` and `arg` are cut to 64 characters. A cut value carries `op_len` / `arg_len`, which gives its full length. Refused tokens log `agent: null` with `error: forbidden | stolen_token | unbound | peer_unknown`. A request body over 4096 bytes, or one with a negative Content-Length, is refused with 400 and never read.

The summary file `maze-c<cell>-<task>.json` holds `{params, cells, shortest, exit: {t, agent} | null, calls, unique_cells, repeat_pct, route, credit: {agent: cells}, per_agent: {agent: {calls, firsts, refused}}, forbidden, stolen, seed: {task, effective, mode}}`.

### Check

`tests/test.sh` passes an attempt when the service reports that the agent made only legal moves within its limit. The cell also has to reach the exit. On shared maps, any agent reaching it counts. On own maps, that agent has to reach it itself. Every attempt's check is recorded. The cell's exit time is reported separately.

## Test cases (sealed tasks)

| Task | Generator | Map | Why |
|---|---|---|---|
| maze-branchy-shared | prim, 40×25 | shared | swarm splits branches |
| maze-branchy-own | prim, 40×25 | own | same Antes, no coordination |
| maze-corridor-shared | backtracker, 40×25, no loops | shared | long corridors, fewer branches |
| maze-line-shared | serpentine, 40×25, from the corner | shared | negative control: one path, nothing to split |
| maze-large-shared | prim, 64×40 | shared | scaling |
| maze-budget-shared | prim, 40×25, call_budget = solo's calls | shared | coordination at equal cost |

Adversarial tests: reading the maze any other way (not in the workspace, nothing on disk); regenerating it from task.toml (a different maze); moving through a wall; two agents claiming the same edge at once (exactly one wins); an agent crashing mid-run; an agent past its step limit; a forged or reused token; a sibling's token used from another process (refused, owner untouched); the log while the cell runs (not on disk).

Cross-check: the lockstep simulations in Python (`maze.simulate`) and JS (`maze-core.js`) agree on walls, calls, exit tick, and credit for the same parameters. A real scripted run of one Ante has to match the simulation call for call. On the plain seed (`maze_seed = "plain"`), compare against the precomputed simulation. On a secret seed, re-simulate using the cell's recorded effective seed.

## Fleet simulation

`maze.simulate_fleet` (Python) and `simulateFleet` (JS) run the fleet modes in lockstep and keep them in parity. The modes are single, swarm, parallel (2..6 agents, 3 at once), delegate (a lead plus 1..6 workers, one assignment per tick), and council (2..6 members, with an optional per-member budget; a synthesizer merges the maps). See `tests/test_maze_fleet.py`. Delegate and council exist only in simulation. The runner has no lead-agent or synthesizer step.

## Pages

`front-maze/race.html` lets you race against 1, 4, or 16 simulated agents. Controls are WASD, arrow keys, the d-pad, or swipe. `front-maze/prototype.html` has simulated agents only. Both are served under `/maze/` by `swarm solo --maze front-maze`, with a hash-only CSP.

## Limitations

- The Linux-only paths (`SO_PEERCRED`, `/proc` ancestry, joining a real cgroup) are covered by unit tests. Those tests were last run on macOS, so check them on Linux before you rely on them.
- A secret-seed maze's `call_budget` equals one agent's calls on the plain `own`-map maze (DFS backtracking). Because the `shared`-map explorer uses BFS `next-edge` instead of DFS backtracking, even a single agent takes a different path and call count on `shared` vs `own` (992 vs 736 calls on the default seed).
- In `MazeService` runs (`swarm run`), `shared` stops all agents as soon as any agent reaches the exit (`done = exit_by is not None`), while `own` requires every agent to reach the exit independently (`done = a.reached`). Compare `exit.t` (time to first exit) in `logs/maze-c<cell>-<task>.json` rather than `batch_wall_s` or total `calls` across `shared` vs `own`.
- `summary.json` and `report.html` surface aggregated `cells.jsonl` maze metrics (`maze_exits`, `maze_exit_s_median`, `maze_calls_median`, `maze_unique_cells_median`, `maze_repeat_pct_median`, `maze_credit_agents_median`) when maze cells are present; per-agent call breakdowns remain in `logs/maze-*.json`.
