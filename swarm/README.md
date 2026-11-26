# Swarm

Swarm measures how many coding-agent instances a machine can run at once, what each one costs, and whether they still finish verified work on tasks you choose. It launches real agent CLIs (Ante, Codex, Claude Code, Hermes, nanocode, Pi, and a minimal reference agent) in separate sandboxes. A deterministic scripted model drives them by default, or a real model through a strict relay if you choose one. Each attempt is then checked with hidden tests, and the run is written to a sealed bundle that `swarm verify` can recheck offline. The same package also runs as a small web service. It has lanes that run preset swarms, a front that races several lanes, and a replay page for stored runs. Three small games (maze, trail, hill) round it out, and in them a person plays against real Ante processes on the same machine.

Python 3.11 or newer, standard library only. Runs on Linux and macOS.

## Architecture

```
 swarm plan spec.toml ─▶ plan.json (frozen, hashed) ─▶ swarm run ─▶ bundle/ ─▶ swarm verify / swarm report
                                                         │
                                       runner: admission (pressure, memory, task slots, disk)
                                               sandbox per attempt (fresh HOME + workdir, clean env)
                                               harness adapter ─▶ agent CLI ─▶ model server on 127.0.0.1
                                                                               scripted (no network) or relay ─▶ gateway ─▶ provider
                                               hidden tests on a private copy, leak sweep, samples

 browser ─▶ swarm front (front/) ──HTTPS + X-Swarm-Ingress──▶ swarm serve (one lane = one machine, one harness)
                                                              quiet gate ─▶ lane control ─▶ reset ─▶ run ─▶ reset
 browser ─▶ swarm solo (front-solo/, front-maze/) ─▶ replay: reads a serve data directory, read only
                                                    ─▶ /api/play/*: live rounds, real Ante cells + ./maze | ./trail | ./hill
```

| Command | What it does | Code |
|---|---|---|
| `probe` | Host fingerprint, memory pressure, task headroom, whether cgroup limits are available | `host.py`, `limits.py` |
| `plan` | Validate a spec (unknown keys rejected) and freeze it: hashes tasks, harness binaries, spec | `plan.py`, `tasks.py` |
| `run` | Execute a frozen plan into a new bundle directory | `runner.py`, `sandbox.py`, `scripted.py`, `relay.py`, `harnesses/` |
| `verify` | Manifest, attempt chain, plan/cell/attempt reconciliation, summary recomputation | `bundle.py`, `summary.py` |
| `report` | Render a bundle's HTML report | `report.py` |
| `serve` | Lane service: preset runs for visitors, quiet gate, lane control, ledger | `serve.py`, `gate.py`, `reset.py`, `live.py`, `anatomy.py` |
| `lane-reset` | Remove runner sandboxes and leftover agent processes, check the machine | `reset.py` |
| `front` | Race one preset across several lanes; repetitions and paired statistics | `front.py`, `stats.py`, `catalog.py`, `front/` |
| `front-unlock` | Show or clear the front's passkey lock with its reset PIN | `front.py` |
| `export` | Numbers-only export tree from a front data directory | `export.py`, `tools/rebuild_export.py` |
| `gateway` | Provider adapters behind per-lane tokens and daily caps | `gateway.py`, `upstream.py` |
| `solo` | Replay page for stored Ante runs, the maze/trail/hill cartridges; with `--play-ante`, live rounds against real Antes | `solo.py`, `maze.py`, `play.py`, `arcade.py`, `gamesvc.py`, `trail.py`, `hill.py` |

## Directory map

| Path | Contents |
|---|---|
| `src/swarm/` | The package and CLI (`python3 -m swarm` or `python3 -m swarm.cli`) |
| `src/swarm/harnesses/` | One adapter per agent CLI; `mini_agent.py` is the reference floor |
| `tasks/` | Sealed synthetic tasks: tier 0 harness packs, work tasks, `maze-*` |
| `examples/` | Run specs (`*.toml`) for `swarm plan` |
| `serve/` | `presets.json` for `swarm serve`, example systemd unit and nginx config |
| `front/` | Race page served by `swarm front` |
| `front-solo/` | Replay page and the maze, trail and hill cartridges served by `swarm solo` (third-party notices inside) |
| `front-maze/` | Maze race and prototype pages, served by `swarm solo --maze` |
| `tools/` | Task generators, export rebuild, network audit |
| `tests/` | `unittest` suite and fixtures |
| `docs/` | [reference.md](docs/reference.md), [evaluation.md](docs/evaluation.md), [gateway.md](docs/gateway.md), [maze.md](docs/maze.md), [solo.md](docs/solo.md), [live.md](docs/live.md) |

Start with [DESIGN.md](DESIGN.md) for principles and concepts. Then read [docs/reference.md](docs/reference.md) for what a run measures, the trust model, the task format, and the spec settings.

## Quick start

For a first local run, use `sh tools/local-smoke.sh`. The script downloads a checksum-pinned public snapshot, runs the bundled mini agent at concurrency 1 and 4 with the scripted model, verifies the bundle and prints its HTML report path. It needs Python 3.11+, curl and tar. The hosted 128-agent fleet-fix solo race uses a separate recorder revision and Linux cgroup limits.

Run these from this directory. Write bundles outside the source tree:

```sh
PYTHONPATH=src python3 -m swarm probe
PYTHONPATH=src python3 -m swarm plan examples/smoke.toml -o /tmp/swarm-plan.json
PYTHONPATH=src python3 -m swarm run /tmp/swarm-plan.json -o /tmp/swarm-bundles/smoke-1
PYTHONPATH=src python3 -m swarm verify /tmp/swarm-bundles/smoke-1
open /tmp/swarm-bundles/smoke-1/report.html
```

A spec names harnesses. Each one must be installed and on `PATH`, or passed with `--binary NAME=PATH`. The exception is `mini`, which ships here. `examples/smoke.toml` runs `mini` and `ante`. To try Swarm without any agent CLI installed, copy the file and set `harnesses = ["mini"]`. Then drop the `[harness_options.ante]` table. Task paths in a spec are relative to the spec file. `pip install .` or `uv tool install .` installs a `swarm` command.

A local lane and the replay page run on loopback with a scripted model:

```sh
PYTHONPATH=src python3 -m swarm.cli serve --presets serve/presets.json --data /tmp/swarm-lane --telemetry off
PYTHONPATH=src python3 -m swarm.cli solo --data /tmp/swarm-lane --static front-solo --maze front-maze
```

Live rounds run against real Ante processes with a stand-in model. This needs no model calls and no credentials, just a local passcode. See [docs/live.md](docs/live.md) for the real-model paths:

```sh
export SWARM_PLAY_PASSCODE_SHA256=$(printf %s 'local-test' | shasum -a 256 | cut -d' ' -f1)
PYTHONPATH=src python3 -m swarm.cli solo --data /tmp/swarm-lane --static front-solo --maze front-maze \
  --play-ante "$(command -v ante)" --play-standin
```

`swarm front` needs a lanes file. See the docstring at the top of `src/swarm/front.py`.

## Tests

Run the full suite (`unittest`) from this directory:

```sh
PYTHONPATH=src:tests python3 -m unittest discover -s tests
```

Run one module or test while iterating:

```sh
PYTHONPATH=src:tests python3 -m unittest test_stats
PYTHONPATH=src:tests python3 -m unittest test_maze.JsParityTest
```

- Many tests launch real processes (the `mini` agent, the scripted model, lane services) and go through admission. They need free disk and normal memory pressure. On a machine with almost no free disk or under memory pressure, attempts are held or time out. The suite then slows down or fails for reasons outside the code.
- `test_maze_integration` is the slowest module. Its sealed-maze tests allow 300 s per attempt.
- Tests that need `node` (`test_maze`, `test_maze_fleet`, `test_page`, `test_solo`, one in `test_serve`) are skipped when it is missing. `package.json` here only marks the browser scripts as CommonJS so Node can load them. There are no npm dependencies.
- The Ante harness test and the end-to-end live rounds (`test_play`, `test_arcade`) run only when `ante` is installed at `~/.ante/bin/ante`. The test for filenames that are not valid UTF-8 runs only on Linux.

## Deploying

Each service is a plain Python process with no build step. [serve/README.md](serve/README.md) covers the lane service, its API, and a generic deployment using `serve/swarm-serve.service.example` (systemd) and `serve/nginx.conf.example` (reverse proxy that adds the ingress secret). Keep `--data` directories outside the source tree. They hold run output. Secrets come from the environment only: `SWARM_INGRESS_SECRET` (lane API), `SWARM_PASSKEY` (extra lane launches), `SWARM_FRONT_PASSKEY` (real-model launches on the front), per-lane secrets named in the front's lanes file, and per-route `secret_env` values in `gateway.json`.

## Known technical limitations

- **The process sandbox is not a security boundary.** Every attempt runs as the same user on the same filesystem, network and signals. Run only agents you trust, or run the whole lane in a VM or container. There is no namespaced or container sandbox.
- Bundles are not signed. `verify` catches corruption and inconsistency, but it can't tell who produced a bundle. Bundles hold raw agent output and absolute paths from the machine that ran them, so review them before sharing.
- The maze service's Linux-only paths (`SO_PEERCRED`, `/proc` ancestry, joining a real cgroup) have unit tests that were last run on macOS in the source history. They were not exercised on Linux for this snapshot. Cgroup limits need cgroup v2 with delegation.
- Maze fleet modes `delegate` and `council` exist only in the lockstep simulation, not in real runs.
- No `swarm compare` for two bundles, no resume of an interrupted run, no Harbor task import.
- Live play runs one Ante or a team (maze `lead`/`shared`, trail ×3, hill ×4 pooled), not the simulated delegate and council shapes. Live cells can read every stored sign-in of their provider profile (see [docs/live.md](docs/live.md), "Exposure").
- Real-model runs bypass each harness's native provider integration (see [docs/gateway.md](docs/gateway.md)).

## Rules for changes

See [AGENTS.md](AGENTS.md). The short version: never edit a frozen plan or a sealed task in place, never retry or drop a failure, and record every result-changing setting. Bump `VERSION` for measurement-code changes. Never commit run output, bundles, logs, or telemetry.

Provenance and third-party notices: [PROVENANCE.md](PROVENANCE.md).
