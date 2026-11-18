# ante-swarm-bench

Run many coding-agent CLIs at once on one machine and watch what each one costs.

Experimental. Python 3.11+ standard library, no dependencies, no build step.

<p align="center">
  <img src="docs/media/race-128.gif" width="880" alt="Six harnesses, 128 agents each, one 2 vCPU / 8 GB VM per harness, same scripted task.">
</p>

## Try it

```sh
cd swarm
PYTHONPATH=src python3 -m swarm plan examples/smoke-mini.toml -o /tmp/swarm-plan.json
PYTHONPATH=src python3 -m swarm run /tmp/swarm-plan.json -o /tmp/swarm-smoke
PYTHONPATH=src python3 -m swarm verify /tmp/swarm-smoke      # report: /tmp/swarm-smoke/report.html

PYTHONPATH=src python3 -m swarm solo --data /tmp/swarm-empty --static front-solo --maze front-maze
# http://127.0.0.1:8000/  ·  /hill/  ·  /maze/
```

No model calls, no credentials. Swap in `examples/smoke.toml` or `examples/density-*.toml` to run real agent CLIs.

## Pictures

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/swarm-scale-dark.gif">
    <img src="docs/media/swarm-scale-light.gif" width="880" alt="4, 32, 128 and 256 Ante processes on one 2 vCPU / 8 GB VM, scripted model.">
  </picture>
</p>

<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="docs/media/density-dark.png">
    <img src="docs/media/density-light.png" width="760" alt="Agents alive at once versus agents requested, 1 to 512, scripted model, one 2 vCPU / 8 GB VM.">
  </picture>
</p>

<p align="center"><img src="docs/media/race-128-receipt.png" width="880" alt="Per-agent memory, CPU, disk, startup and step gap from one 128-agent race."></p>

<p align="center"><img src="docs/media/maze-race-swarm-x16.gif" width="720" alt="Maze game: a person against 16 simulated agents sharing one map."></p>

More runs and receipts: [bench](https://antigma.exe.xyz/swarm/bench/) · [race](https://swarms.exe.xyz/).

## Layout

```
swarm/   harness, lanes, race front, replay page, games      swarm/README.md
bench/   live-task protocol, adapters, replay studies         bench/README.md
hub/     run receipts API and console                         hub/README.md
site/    live launch page
docs/    methodology, integrity
```

Tests: `tests/run-all.sh`

Apache-2.0. Third-party notices in [NOTICE](NOTICE). Product names belong to their owners; not affiliated with or endorsed by any of them.
