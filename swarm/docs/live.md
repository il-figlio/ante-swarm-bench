# Live play: real Antes against a person

`swarm solo` can run live rounds of three small games. A person plays against real Ante processes on the same machine. The games are the **maze** (a fog-of-war race, optionally with an Ante teammate), **trail** (a snake-style arena) and **hill** (a falling-block duel). Without the operator passcode, every page plays against scripted opponents in the browser. No model gets called.

The code is in `src/swarm/play.py` (round manager, maze rounds, model path, budgets, HTTP API), `src/swarm/arcade.py` (the generic game host and the trail/hill API), `src/swarm/gamesvc.py` (the per-round `./<game>` socket), `src/swarm/trail.py` and `src/swarm/hill.py` (rules), `front-solo/{maze,trail,hill}.{html,js}`, `front-solo/mazelive.js` and `front-solo/arcadelive.js` (the pages' live clients, no DOM, tested under Node), and `front-solo/{trail,hill}-core.js` (the JS twins of the rules and scripted policies). Tests: `test_play.py`, `test_play_front.py`, `test_arcade.py`, `test_trail.py`, `test_hill.py`.

## Running it

```sh
# Scripted opponents only (no Ante needed):
PYTHONPATH=src python3 -m swarm.cli solo --data /tmp/swarm-lane --static front-solo --maze front-maze

# Live rounds with a stand-in model (real Ante processes, no model calls, no credentials):
export SWARM_PLAY_PASSCODE_SHA256=$(printf %s 'local-test' | shasum -a 256 | cut -d' ' -f1)
PYTHONPATH=src python3 -m swarm.cli solo --data /tmp/swarm-lane --static front-solo --maze front-maze \
  --play-ante "$(command -v ante)" --play-standin
```

The page asks for the passcode once per tab and keeps it in `sessionStorage`. It only sends the passcode with the request that starts a round. The server stores just the SHA-256 hash, read from `--play-passcode-env` (default `SWARM_PLAY_PASSCODE_SHA256`).

## Round model (maze)

- Each round uses one maze (`maze.generate(params)`, sizes s/m/l as on the cartridge) and two teams:
  - `player`: the person (agent id `you`) plus, with assist on, one Ante (`ally`);
  - `rival`: one Ante (`single`) or a swarm of 4 or 16 on one team map, in the `lead` or `shared` shape (see "Swarm styles" below). The other fleet shapes of the cartridge (parallel, delegate, council) stay scripted.
- Fog of war applies per team. A team sees the union of what its members have seen, and neither team sees the other's map. `MazeModel` stores a `team` for each agent and a per-team `seen`. The `shared`/`own` behaviour that the maze eval uses is unchanged.
- The first team to reach the exit wins. A round also ends when its timeout hits (default 300 s), when its token budget runs out, or when the player leaves. Leaving means no event-stream reader for 30 s.
- Pacing runs in real time. The person moves whenever they like. Each Ante moves when its tool call lands.

### The Ante side

Each Ante runs as a normal Swarm cell. It gets its own sandbox, the cell's cgroup limits, and the maze service's token and peer-pid checks (see [maze.md](maze.md)). Its prompt asks it to solve the maze with the `./maze` client: `look`, `move N|E|S|W`, `next-edge`, `map`. The ally's client adds two read-only ops:

- `player`: the person's position, their last 10 moves, and the team map;
- `rivals`: each rival's distance-to-exit bucket (`far|mid|near`) and number of cells mapped, never positions.

The ally explores for the team. Whatever it finds shows up on the person's screen.

## Maze API (`/api/play/`, same origin as the cartridge)

| Method / path | Body / reply |
|---|---|
| `GET /api/play/status` | `{live, reason?, queue, max_antes, model, provider, ante_provider?}`; `reason` ∈ `no_passcode_configured \| credits \| limit \| signin \| busy \| memory` |
| `POST /api/play/rounds` | `{passcode, size: s\|m\|l, rival: {mode: single\|swarm, count: 1..16}, assist}` → 201 `{round, token, seed_hash, queue}` (`queue` 0 = running now, k = k-th in line) |
| `POST /api/play/rounds/<id>/move` | header `X-Round-Token`, `{dir: N\|E\|S\|W}` → `{pos, ok, open}`; a bumped wall answers 200 with `ok: false` and is not counted as a move |
| `GET /api/play/rounds/<id>/events` | Server-sent events (token in the header, or `?t=` for `EventSource`) |
| `DELETE /api/play/rounds/<id>` | give up; every cell is killed |

Refusals: 403 `wrong_passcode` comes from a constant-time compare, before any cell starts. 429 `too_many_tries` fires after 5 wrong tries in 10 minutes from one address, or 30 from everyone together. Other refusals: 422 for `bad_size`, `bad_rival`, `bad_mode`, `mode_not_live`, `bad_count`, or `bad_assist`; 409 `busy`, with the queue position; 503 `unavailable`, with a reason. A move gets 422 `bad_dir`, 409 `not_started` while queued, or 410 `over` after the end. Without the token, the event stream returns 403. An unknown round returns 404, and more than three readers returns 429.

Events:

- `state`: full snapshot on connect: `{phase: queued|running|over, queue?, t, cols, rows, start, exit, you: {pos, moves}, ally: {pos}|null, seen, rivals: [{who, progress}]}`. Positions are cell indexes (`y*cols+x`). Seen cells appear as `[i, wallbits]`, with N=1, E=2, S=4, W=8.
- `move {who, team, pos}` and `reveal {team, cells}`, sent for the player's team only.
- `rival {who, progress}`, where `progress = {calls, mapped, bucket, state?}`. It never includes a position until the end.
- `call {who, team, op, arg?, result?}`: Ante tool calls, shown in the feed. `arg` and `result` appear only for the ally.
- `end {winner: player|rival|null, reason, stats, maze}`: `reason` ∈ `exit|timeout|tokens|left|gave_up|error|limit|signin`. `maze` reveals the walls, every path, the shortest `route`, and the `seed`, `nonce`, and `seed_hash = sha256("<seed>:<nonce>")`. The page uses these to check the commitment it got at the start.

Agent names are `you`, `ally`, and the rivals: `ante` in single mode, or `a01..a16` in swarm mode. Only one round runs at a time. Up to three can wait in the queue, and each queued round gets `state` updates with its position. Without a reader for 30 s, it is dropped as `left`.

Each round writes one entry to `rounds.jsonl`, which logs the seed hash, sizes, timings, call counts, tokens, winner, and sign-in label. It stores no player identity.

## Model path

`--play-provider` picks how the Antes reach a model.

- `subscription` (default): each Ante runs on the provider and sign-in it is configured with, through the Ante gateway (Ante's built-in `anthropic-subscription` or `openai-subscription` provider and a sign-in already stored in the service's Ante home). Provider and model default to that home's `settings.json`, and only those two keys are read. You can override them with `--play-ante-provider` and `--play-model`.
- `relay`: goes through the Swarm relay to `--play-upstream`. That gateway holds the credential. Model, effort, and token checks are strict, same as in [gateway.md](gateway.md).
- `standin` (or `--play-standin`): no model at all. A scripted model drives every Ante with the maze Explorer policy, or the game's stand-in policy. The tests use this mode.

### Choosing the sign-in

`--play-signin` selects which configured sign-in a round uses. Each cell's `catalog.json` names exactly one credential, in this shape:
`{"providers": {"<ante provider>": {"credential": "oauth:<profile>/<label>"}}}`.

- A fixed `--play-signin <label>` uses that sign-in only. `--play-signin auto` reads `ante usage --json` and picks one of the provider's labelled sign-ins that is under 95% on both its session and weekly windows, preferring the one whose weekly window resets soonest.
- If usage can't be read, the fallback is `--play-signin-order`, paired with `--play-signin-reset LABEL=ISO` weekly reset times. The time zone is required, and times get rolled forward a week at a time. The picker never falls back to a list of credentials. A bare `<profile>` sign-in with no label is never used.
- Usage is read at most once every 3 minutes, and never while a round is running. The chosen label is recorded with the round, never an account e-mail.

### Credentials are shared by a directory link, not copied

Ante stores each sign-in as `<ANTE_HOME>/auth/credentials/oauth/<profile>/<label>.json`. It refreshes one by writing a new file and renaming it over the old one. Every cell's `$ANTE_HOME/auth/credentials/oauth/<profile>` is a symlink to the one real directory. So a refresh by any Ante ends up in the same store. We measured this against a mock issuer that rotates refresh tokens:

| Layout | Result |
|---|---|
| Per-file symlink | Broke on the first refresh. The rename replaces the link, so the store keeps a spent refresh token, and 16 of 17 Antes failed. Not used. |
| Directory symlink, 17 Antes starting on an expired token, 7 runs | The store stayed valid every time. Ante has no cross-process refresh lock, so 0 to 6 of the 17 lost the refresh race and stopped with an `oauth` error. |

Ante refreshes only after a 401, not ahead of `expires_at`. So the race only happens when a token expires at or during a round. To mitigate it: if the grant is already expired or within 30 s of expiry at round start, one Ante goes first, and the rest wait for its first answer (up to 45 s). A mid-round expiry can still cost a few Antes that round. The link is removed before a sandbox is deleted, and `rmtree` never follows links.

### Exposure

Each cell's agent runs with full tool permissions. It can read every sign-in of that profile in the store, and it could alter or delete them. Players send only moves (`N|E|S|W`, columns and rotations) and the passcode. No player text reaches a model, so prompt injection from players isn't possible. The remaining risk is the model itself misbehaving inside the sandbox. The sandbox is not a security boundary (see the README). The store is never exposed to the page or the logs.

### Budgets without the relay

Each Ante's `--output-format json` stream is read as it arrives. Every `UsageUpdate` counts as one response, and its `input_tokens` already includes cache reads and writes. Those tokens count against both the round cap (`--play-round-tokens`, default 400k) and the daily cap (`--play-daily-tokens`, default 5M). The round ends (`tokens`) as soon as either cap is spent. `--play-max-requests` cancels a single Ante. When a cap is hit, `status.live` turns false and gives a reason.

Ante error kinds `rate_limited|quota` end the round as `limit`, and that sign-in is skipped for 15 minutes. `oauth|auth|authentication|forbidden` with no answer from any Ante ends the round as `signin`, and that sign-in rests for 5 minutes. If some Antes did answer, the error is a lost refresh race, and it gets recorded on that Ante only.

## Games: trail and hill

Trail and hill are cartridges, like the maze (`/trail/`, `/hill/`). `/` opens trail. The stored-run replay console is at `/replay` and `/r/<run>`.

### Trail rules (`trail.py`, twin `trail-core.js`)

- One shared grid (s 20×13, m 28×18, l 36×23) with players + 1 tokens. Trails start 3 cells long in lanes. Seat k of n sits on row `rows·(k+1)/(n+1)`. Even seats head east from the left fifth; odd seats head west from the right fifth. Nobody starts facing a head or a near wall.
- Every trail moves one cell per tick. A token grows the trail by one and respawns on a seeded free cell. A wall, any trail, or two heads on one cell put a trail out, and its body leaves the board.
- Antes call `./trail look`. It returns board rows (`. * @ o H x`), heading, free runs ahead/left/right, tokens, rivals, and the queued plan. It also returns `safe` and `hits`: the ticks before the queued path, then straight on, meets a wall or trail as the board stands. A `warning` appears when `safe` drops below 8.
- `./trail plan <moves>` queues up to 12 of L/R/S, one per tick, each relative to the heading at that tick. It replaces the old plan. `./trail turn L|R|S` makes one move at the next tick and clears the plan. Both `plan` and `turn` answer with the full look, so one model request covers one re-plan.
- The person steers absolutely (N/E/S/W). A reversal is ignored.
- A round ends at the clock (300 ticks), or when at most one trail is left. With one player, it ends only at the clock. Winner: most tokens, then still alive, then out later. A tie at the top is a draw.

### Hill rules (`hill.py`, twin `hill-core.js`)

- Each player has a 9×18 well. Everyone gets the same seeded sequence, in bags of 7, of the cartridge's own pieces: pip (2 cells), bar and elbow (3), cup, flag, angle and post (5). There are no four-cell pieces.
- Antes call `./hill look`. It returns well rows, the current piece with its four rotations, the next piece, rivals' heights, and, in live rounds, the piece's random `id` and `wait_s`. Then they call `./hill drop <col> <rot> <id>`. The person moves, turns, and drops by hand.
- Clearing 1, 2, 3, or 4 rows at once sends 0, 1, 3, or 4 garbage rows to the next standing well. Each garbage row has one hole column. They rise after that well's next drop. A piece that does not fit puts the well out.
- A round ends when at most one well stands, or at the clock (180 s). Rank: standing, out later, rows cleared, lower stack. A piece left hovering too long drops where it is. The fuse is 6 s scripted. Live, it is 10 s on the person's page and 12 s on the server and for an Ante.

### Anti-scripting rules for live hill (`hill.LiveHill`)

The first real-model rounds showed an Ante writing a shell loop that issued hundreds of blind `./hill drop` commands in seconds. Live hill now gives every piece a fresh random id. It uses `secrets`, so it cannot be derived from the seed. Only `look` and the `now` view returned by a successful drop show the id. A drop must name the current piece's id, or it is refused and nothing changes. An Ante's drops must be at least 1 s apart, or they are refused with `wait_s`.

Scripts are not forbidden. But every drop has to read a fresh view, and a well cannot out-drop a person. Refusals are counted in `stats.refused` and shown in the page's call feed. The person and the scripted twin are unaffected.

### Live pace

Live trail ticks every 0.8 s, so 225 ticks take 180 s. That matches model latency of a few seconds per call. The scripted cartridge keeps 4, 6, or 9 ticks per second. Hill's scripted pace is ½, 1, or 2 pieces per second. Every live round waits for each Ante's first call (up to 20 s), then counts down 3 s.

### Scripted mode

This is the default, labelled "simulated." A deterministic JS policy in the core file plays the opponents (ante ×1 or swarm ×4). Its Python twin (`choose`) is checked against it drop for drop and tick for tick in the tests.

## Swarm styles: how a team of Antes plays

A game's swarm plays as one team wherever the work can be split. Each game declares its style. The maze and trail are **shared**: one board, shared intelligence. Hill is **pooled**: each player gets one sequential stream of decisions, so each member plays its own well and the results add up. Each game offers only the swarm sizes that help. These rules apply to live rounds. The scripted maze cartridge (`simulateFleet`) is unchanged. The scripted trail and hill twins follow the same team rules as the live ones.

### Maze: notes, shapes and a lead

- **Team notes** (`MazeModel`, team agents only; maze-eval agents get `bad_op`): `./maze note <text>` and `./maze notes`. Each team has one board, holding the newest 20 notes at 120 characters each, in plain text. Control and format characters (newlines, bidi and zero-width marks) become spaces. An empty note returns `empty_note`. A cut note answers `cut: true`. `look` carries the newest 3 notes by teammates. A `move` answer carries teammates' notes it has not shown yet. Boards never cross teams. With assist on, the player's team gets its own board, and only the ally posts to it. The person sends moves, never text.
- **Shapes** (`rival.shape`, optional; `PlayConfig.swarm_shape`, default `lead`): `lead` makes `a01` the lead and the rest workers. `shared` makes all peers on one map, with next-edge claims and notes. A lead needs at least 3 Antes. Otherwise it returns 422 `bad_shape` with `lead_min: 3`. A shape on `single`, or an unknown shape, is also 422 `bad_shape`. The 201 reply, `state`, `end`, `end.stats` and the `rounds.jsonl` row all carry the shape (`single|shared|lead`).
- **Lead ops**: `./maze assign <worker>` runs `MazeModel.assign`'s next-edge on the worker's behalf. The claim belongs to the worker. It returns `bad_worker` for the lead itself, another team, or an unknown name. It returns `path: null` when nothing is left. The worker's `look` and `move` answers then carry `assigned: {to, path, by}`, recomputed from where it stands. This continues until the worker reaches the target, asks `next-edge` itself, or gets another assignment. `./maze team` lists each teammate's `at`, `claim`, `assigned`, last op, `calls`, `notes` and `idle` (nothing assigned and no claim left), plus the newest notes. Workers keep `next-edge` as a fallback.
- **Clients and prompts**: A single rival keeps the maze eval's client byte for byte. Swarm members and the ally get `maze.live_client(ops, usage)`, built from it (`play.role_client(role)`). `play.rival_prompt(mode, count, role, who, names)` gives one team prompt. It asks agents to claim different edges, note dead ends and promising corridors, read teammates' notes, and carry on after an error. It then adds a role part. A peer gets nothing extra. A lead (`team`) assigns every idle worker, explores a few moves, and repeats, rebalancing on dead ends and idles. A worker follows `assigned`, reports by notes, and says `idle at x,y` once. After that it uses next-edge rather than waiting.
- **What the page sees**: rivals in `state` carry `role`. `rival.progress` gains `notes`, a count. A rival's note `call` carries only the board's total, never text, arguments or positions. `end` adds `shape`, `roles` and both boards (`notes: {player: [...], rival: [...]}`). `end.stats.notes` counts them. `GET /api/play/status` adds `{swarm_style: "shared", sizes: [1, 4, 16], shapes: ["lead", "shared"], swarm_shape}`. `rounds.jsonl` rows gain `shape`, `notes` per team and `ops` (honoured calls per Ante per op).

### Trail: shared (sizes 1 and 3)

- **Team score**: the sum of the members' tokens. Teams are ranked by tokens first, then by how long the team lasted (the tick its last trail went out). A member that crashes keeps its tokens for the team. The round ends when the person is out, when the whole team is out, or at the clock.
- **Sizes**: ante ×1 on the m grid, swarm ×3 on the l grid. Not ×4: in 60 scripted rounds per shape on the l grid, the share of swarm members that crash rose from 0.14 (×3) to 0.19 (×4). A real ×4 team also costs a third more tokens per round. Cooperation pays in the scripted twin. On l, ×3 cut the share of members crashing from 0.23 to 0.14 and raised team tokens per 100 ticks from 19.3 to 22.4. The comparison is the same three trails each playing for itself.
- **Shared plans**: `./trail look` adds `who`, `team` (each teammate's `at`, `heading`, queued `plan`, `tokens`, `alive`), `score`, the newest 6 notes, and `crosses`. `crosses` carries a `warning` when the queued path runs over a teammate's. Teammates are drawn as `M` (head) and `m` (trail). `./trail team` lists every member's position, heading, plan, the cells it will draw, its last 5 tokens, the team and rival totals, food and every note. `./trail note <text>` posts to a per-team board (newest 20, 120 printable ASCII characters each). `look`, `team` and `note` work before the go (`Game.free_ops`).
- **Privacy**: the page never sees plans or notes during a round. Frames carry no notes, and a note's `call` has no text. The boards are revealed in `end.notes` and kept in `rounds.jsonl`.
- **Scripted twin** (`trail.team_turns`, `TrailCore.teamTurns`): teammates split the tokens. The closest member/token pair goes first, with ties broken by name, then token order. Teammates treat the cells that earlier teammates in seat order just chose as blocked. A scripted team never meets head on. The stand-in Ante plays the same way from its `look` and pins one note.

### Hill: pooled (sizes 1 and 4)

- **Own wells, own pieces**: each swarm member's well draws its own sequence from `well_seed(seed, seat) = (seed + 0x9E3779B9 · seat) mod 2³²`. The person (seat 0) keeps the round's sequence. A lone Ante shares it.
- **Pooled score**: the team's rows are the sum over its wells. Garbage passes from team to team using the per-clear amounts. Everything a swarm well sends lands in the person's well. The person's garbage goes to the swarm's standing wells in rotation. The swarm loses when all its wells are out. It wins when the person's well tops out first. At the clock, the person's rows are compared with the swarm's rows per well, then the lower average stack decides. `frame.teams` and `end.stats.pooled` show the total and the per-well average.
- **No coordination ops**: `look` adds `team` (pooled rows, per well, teammates' rows and stacks) and lists only the person as a rival. Piece ids and the 1 s gap stay.

### Shared changes

`TrailModel(p, names, teams)` and `HillModel(seed, names, teams)` (and their JS twins) take teams. Without teams, every player is a team of one and the game is unchanged. The older parity logs still match. Live rounds seat the person as team `you`, a lone Ante as `ante`, and a swarm as `swarm`. `winner` and `leader` name teams. `state` and `end.stats` carry `swarm_style`. `end.stats.ops` and the `rounds.jsonl` row carry honoured calls per Ante per op. The row keeps the game's stats as `result`. The cartridges build their opponent keys from the server's `sizes`.

## The game interface (`arcade.py`)

A `Game` gives the host: `name`, `ops` (op → argument count). `look` is free. Acting ops wait for the start. `usage`, `new_model(seed, names)` (the rules model, `you` is seat 0), `prompt(mode, count)` (the Ante's task text), `standin(index)` (the scripted policy that drives a real Ante in stand-in rounds), `act(model, body, now)` (the person's move → `(status, reply)`), `advance(model, elapsed)` (the clock: ticks, fuses, end → events), `on_call(...)` (events for an Ante call), `frame(model)` (the full public state; nothing is hidden but the seed), `stats(model)` and `limit_s()`. The rules model exposes `call(who, op, arg)`, `look(who)`, `over`, `reason`, `winner` and `finish(reason)`.

`gamesvc.GameService` is the `./<game>` socket. It has a per-agent token bound to the Ante's process tree (the maze service's `peer_pid`, `descends_from` and `peer_check_problem`, unchanged), forbidden and stolen-token counters, and a gate that holds acting ops until the round starts. `client_source(name, ops, usage)` writes the client script.

`Arcade(manager)` runs rounds on the live maze's `PlayManager`. It shares that manager's passcode check, per-address limit, budgets, model route (configured sign-in, relay or stand-in), cgroup limits, memory check and single round slot. While an arcade round runs, it is the manager's active round, so a maze round queues behind it. An arcade round asked for while anything runs gets 409 `busy`. Arcade rounds do not queue.

Round life: starting → ready (each Ante's first call) → count (3 s, `go_in` in `state`) → running → over. Teardown kills every cell, removes the sandboxes, closes the service and appends a row with `game` to `rounds.jsonl`.

| Method / path (`<game>` = trail \| hill) | Body / reply |
|---|---|
| `GET /api/play/<game>/status` | the maze status plus `{game, swarm_style, sizes, counts: {single: [1], swarm: [n]}, max_antes}` |
| `POST /api/play/<game>/rounds` | `{passcode, rival: {mode: single\|swarm, count}}` → 201 `{round, token, seed_hash, queue: 0}`; 403 wrong passcode (before any cell), 429, 422 `bad_rival\|bad_mode\|bad_count` (a count not in `sizes`), 409 busy, 503 unavailable |
| `POST /api/play/<game>/rounds/<id>/act` | `X-Round-Token`; trail `{dir}` → `{ok, tick, next_heading, alive}`; hill `{col, rot}` → the drop result; 409 `not_started` before running, 410 over |
| `GET /api/play/<game>/rounds/<id>/events` | SSE: `state` `{round, game, phase, go_in?, mode, count, rivals, t, limit_s, seed_hash, frame, tick_s?}`, `frame`, `call {who, op, arg?}`, `end {game, winner, leader, reason, stats, frame, seed, nonce, seed_hash}` |
| `DELETE /api/play/<game>/rounds/<id>` | give up |

`winner` is set only when the game ended by its own rules. `leader` says who was ahead when a round was given up or cut short.

## Measurements

Peak RSS per Ante process over 702 lane attempts: median 48 MB, p95 74 MB, max 80 MB. The service caps a round at 16 rival Antes (plus the ally). That runs on a 2 vCPU, 8 GB machine.

The system prompt and tools alone cost Ante about 12.7k tokens per request on a small model, mostly as cache reads. In the games that grows with context to about 17k. Tuning rounds for trail and hill ran Ante ×1 on a small Claude model against a scripted stand-in for the person, with 60 s and 300k-token caps:

| Round | Result | Ante requests | Tokens |
|---|---|---:|---:|
| trail, plan replies without the board | alive all 30 s; ended `tokens` at the cap | 18 | 307,549 |
| trail, plan replies carry the look | `LLSSS` U-turn into the east wall at tick 7 | 3 | 40,625 |
| trail, plus `warning` and a U-turn hint in the prompt | out on a trail at tick 26, 1 token | 7 | 101,722 |
| hill, with piece ids | standing at 60 s, 5 rows, 15 drops, 0 refused | 16 | 299,063 |

Before the pacing change, live rounds on a real model ended almost at once. At 3–5 s per call, trail ended in 3.7 s, since 0.35 s ticks let a trail drive 10+ cells between calls. Hill ended in 28 s after a shell loop of 377 blind drops. At one request every 3–5 s, a 180 s round costs about 0.7–1M tokens per Ante. That is above the default 400k round cap. So with default caps, a live trail or hill round ends `tokens` before its clock runs out.

Team rounds on a real model (Ante 0.3.5 through the Ante gateway, a small Claude model, one fixed sign-in, 90 s clocks, a 1.5M-token round cap, and a scripted stand-in for the person):

| Round | Result | Tokens | Ante requests | What the swarm did |
|---|---|---:|---|---|
| trail swarm ×3 | whole team out at 54 s (members out at ticks 22, 36, 67); person 8 tokens, swarm 3; person won | 789,728 | 16 + 9 + 14 | 12 notes (token claims and hand-offs such as "a03 keeps 21,19 (closer); a01 pick another"), 26 plans, 8 looks, 1 turn, no `./trail team` calls |
| hill swarm ×4 | ended `tokens` at 78 s (1.53M; a request in flight overshoots the cap); all 5 wells standing; person 11 rows, swarm 16 (5/4/2/5) = 4.0 per well | 1,529,510 | 17 + 18 + 14 + 20 | 62 drops with ids, 3 refused, 7 looks (each drop's `now` view served as the next look); 2 garbage rows sent to the person |

At 17–25k tokens per request, a ×3 trail team spends about 0.8M tokens per minute of play. A ×4 hill team spends about 1.2M. A full 180 s live round therefore needs roughly 2.5–3.5M tokens, or shorter live clocks. Crash causes are now recorded per member in `stats.causes` (`{who: {cause, hit}}`, with whose trail was hit).

## Checks

- Without the passcode, the pages behave exactly like the scripted cartridges (existing tests).
- Stand-in rounds run end to end with real Ante processes and the scripted model. Covered: 1 and 4 rival Antes with assist, over HTTP and through the real `mazelive.js` client under Node. Checked team fog, rival buckets only, end reveal with every path, and every cell killed (`test_play.py`, `test_play_front.py`, `test_arcade.py`). The end-to-end rounds run only when an Ante binary is installed.
- A wrong passcode is refused before any cell starts. The passcode never appears in logs, replies, or the page.
- Browser check of the maze cartridge at 1280×800 (keys) and 390×844 (touch D-pad). No CSP errors, no exceptions, no horizontal scroll.

## Limitations

- The live maze supports only single and swarm (`lead` or `shared`). Parallel, delegate, and council stay simulated.
- There is no CLI flag for the maze swarm shape yet. The default is `lead`. `shared` is picked per round through the API.
- No accounts and no cross-visitor leaderboards.
- `openai-subscription` was checked in a cell only with a fake grant (resolved through the linked store, Responses wire). It was not checked with a real labelled sign-in.
- Mid-round token expiry can still cost a few Antes to the refresh race described above.
