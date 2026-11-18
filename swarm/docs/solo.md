# Solo replay page

`swarm solo` serves a single page about Ante alone, with no competitors and no leaderboard. It replays Ante runs already stored in a `swarm serve` data directory and draws each attempt's trace as a small handheld game. Every move on the board is a real event from the trace. It is replay only. Nothing here starts a harness, calls a model, or writes to the data.

```sh
PYTHONPATH=src python3 -m swarm.cli solo --data <serve data dir> --static front-solo [--maze front-maze] --port 8000
```

`--maze` also serves the maze pages. `/maze/` (and `/maze/race`) is the maze cartridge, a playable race against simulated Antes. `/maze/prototype` is the analysis view; see [maze.md](maze.md).

## Server (`src/swarm/solo.py`)

- Reads `runs/<id>/run.json` and its bundle from the data directory. Read only. Run ids are 12 hex characters, and anything else returns a 404.
- `GET /api/runs` and `GET /api/runs/<id>` are built field by field from an allow-list. No row is copied as-is. The output has run receipts (preset, size, hashes, pass counts, timings) and, for each attempt, its events `{t, kind, arg?, ok?, bytes?}`.
- Free text (file names, shell commands, the final message) goes through `clean()`. It drops absolute paths, long hex and base64-like runs, e-mail addresses and URLs, and then cuts the result short. The leak test is in `tests/test_solo.py`.
- Trace to events: request rows map to `read`, `edit`, `exec`, `test` and `done`. The checker's verdict closes each list as `verify`. Times are relative to the run's first request.
- Static files ship with a same-origin Content-Security-Policy. The cartridge (`front-solo/maze.html`) is a normal front-solo page under that policy and loads `/maze/maze-core.js`. The analysis view gets its own policy, built from the hashes of its inline scripts and styles. `/maze` redirects to `/maze/`. Any path with a further slash, such as `/maze/race/`, is a 404. That way relative script paths can't resolve to the wrong directory.

## Page (`front-solo/`)

- Boards are pure functions `(events, t) → frame` in `front-solo/boards.js`. That makes them testable without a browser; `tests/test_solo.py` runs them in Node when `node` is installed. The boards are **Trail** (snake genre: the trail grows with each step, and a failed command knocks it back) and **Hill** (stacking: each model turn drops a block, and complete rows clear). There's also the swarm grid, which shows every agent of a run.
- The device: the main LCD shows the board. A mode dial picks Trail, Hill or Maze (Maze opens the cartridge). Run keys step through stored runs, and task keys filter the run's agents. The D-pad's left and right steps an event; up and down changes agent. A plays or pauses, B resets. Keyboard: arrows, space, `r`, `t`, `m` (maze). The receipt sits below the device.
- With reduced motion, there's no autoplay and no swarm fill. A steps instead.
- The look follows spec-01's hardware skin (github.com/shuding/spec-01, MIT). It's rewritten in plain CSS and JS, not vendored. The type is Spec Mono, spec-01's subset of IBM Plex Mono (SIL OFL 1.1). Notices are in `front-solo/NOTICE.txt` and `front-solo/fonts/OFL.txt`.

## Maze cartridge (`front-solo/maze.{html,css,js}`, `front-solo/mazeplay.js`)

- A race on the same device: you against simulated Antes on a fog-of-war maze. The Antes come from `simulateFleet` in `front-maze/maze-core.js`, the JS twin of `swarm.maze.simulate_fleet`. They run in the five fleet modes (single, swarm, parallel, delegate, council). The page replays their call log at the chosen pace. No model is called.
- `mazeplay.js` holds the rules: round settings, opponents, and the tick-by-tick replay of the simulated call log. It has no DOM, clock or randomness, which is why `tests/test_solo.py` can run it under Node. `maze.js` handles drawing and input.
- Controls: arrows or WASD move, `a` or space races, `b` deals a new maze. On touch screens, the D-pad moves on `pointerdown` and repeats while held. A swipe moves once, in its main direction.
- Best times are stored per size, opponent and pace in `localStorage`. A maze already raced this visit doesn't set one.

## Live rounds

With `--play-ante`, the same server hosts live maze, trail and hill rounds against real Ante processes, behind an operator passcode. See [live.md](live.md). Without it, every cartridge plays scripted opponents in the browser.

Trail and hill are playable cartridges at `/trail/` and `/hill/`. `/` opens trail. The replay console described above is at `/replay` and `/r/<run>`.

## Limitations

- The replay console can't start a harness run. Only live rounds can.
