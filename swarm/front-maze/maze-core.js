/* Maze core for the browser and for node: the generator, the call model and the scripted explorer.
 *
 * This is a port of src/swarm/maze.py and must stay cell for cell and call for call identical to it
 * (tests/test_maze.py runs both and compares). Change one, change the other.
 *
 * Cell layout: index = y * cols + x. Wall bits N=1, E=2, S=4, W=8, set = wall. y grows downward.
 * The page steps a run one tick at a time (`stepRun`) so it can draw it; `simulate` is the same stepping with
 * the drawing left out.
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.MazeCore = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const DIRS = [
    { name: 'N', bit: 1, dx: 0, dy: -1 }, { name: 'E', bit: 2, dx: 1, dy: 0 },
    { name: 'S', bit: 4, dx: 0, dy: 1 }, { name: 'W', bit: 8, dx: -1, dy: 0 },
  ];
  const OPP = { 1: 4, 2: 8, 4: 1, 8: 2 };
  const OPPOSITE = { N: 'S', E: 'W', S: 'N', W: 'E' };
  const PARAMS_DEFAULTS = { cols: 40, rows: 25, seed: 2026, generator: 'prim', loops: 0.05, start: 'center',
    map: 'shared', step_limit: 4000, call_budget: 0 };

  const isInt = (v) => typeof v === 'number' && Number.isInteger(v);

  function validateParams(d) {
    const unknown = Object.keys(d || {}).filter((k) => !(k in PARAMS_DEFAULTS));
    if (unknown.length) throw new Error('unknown maze params: ' + unknown.join(', '));
    const p = Object.assign({}, PARAMS_DEFAULTS, d);
    for (const [key, lo, hi] of [['cols', 4, 128], ['rows', 4, 128], ['seed', 0, 4294967295], ['step_limit', 1, 1000000],
      ['call_budget', 0, 10000000]]) {
      if (!isInt(p[key]) || p[key] < lo || p[key] > hi) throw new Error(`maze ${key} must be an integer in ${lo}..${hi}`);
    }
    if (typeof p.loops !== 'number' || !(p.loops >= 0 && p.loops <= 0.3)) throw new Error('maze loops must be in 0..0.3');
    for (const [key, allowed] of [['generator', ['prim', 'backtracker', 'serpentine']], ['start', ['center', 'corner']], ['map', ['shared', 'own']]]) {
      if (!allowed.includes(p[key])) throw new Error(`maze ${key} must be one of ${allowed.join(', ')}`);
    }
    return p;
  }

  // mulberry32
  function rng(seed) {
    let a = seed >>> 0;
    return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  }

  const inBounds = (cols, rows, x, y) => x >= 0 && y >= 0 && x < cols && y < rows;
  function open(walls, i, d, j) { walls[i] &= ~d.bit; walls[j] &= ~OPP[d.bit]; }

  // Randomized Prim over an edge list with swap-remove: many short branches.
  function prim(cols, rows, start, rand) {
    const n = cols * rows, walls = new Uint8Array(n).fill(15), inMaze = new Uint8Array(n), edges = [];
    const add = (i) => {
      inMaze[i] = 1;
      const x = i % cols, y = (i / cols) | 0;
      for (const d of DIRS) { const nx = x + d.dx, ny = y + d.dy; if (inBounds(cols, rows, nx, ny) && !inMaze[ny * cols + nx]) edges.push([i, d]); }
    };
    add(start);
    while (edges.length) {
      const k = (rand() * edges.length) | 0, [i, d] = edges[k];
      edges[k] = edges[edges.length - 1]; edges.pop();
      const j = i + d.dy * cols + d.dx;
      if (inMaze[j]) continue;
      open(walls, i, d, j); add(j);
    }
    return walls;
  }

  // Seeded depth-first carve: long corridors, few branches.
  function backtracker(cols, rows, start, rand) {
    const n = cols * rows, walls = new Uint8Array(n).fill(15), visited = new Uint8Array(n), stack = [start];
    visited[start] = 1;
    while (stack.length) {
      const i = stack[stack.length - 1], x = i % cols, y = (i / cols) | 0;
      const opts = DIRS.filter((d) => inBounds(cols, rows, x + d.dx, y + d.dy) && !visited[(y + d.dy) * cols + x + d.dx]);
      if (!opts.length) { stack.pop(); continue; }
      const d = opts[(rand() * opts.length) | 0], j = (y + d.dy) * cols + x + d.dx;
      open(walls, i, d, j); visited[j] = 1; stack.push(j);
    }
    return walls;
  }

  // One path through every cell, row by row: nothing to split (the negative control). No random draws.
  function serpentine(cols, rows) {
    const walls = new Uint8Array(cols * rows).fill(15);
    const E = DIRS[1], S = DIRS[2];
    for (let y = 0; y < rows; y++) {
      for (let x = 0; x < cols - 1; x++) open(walls, y * cols + x, E, y * cols + x + 1);
      if (y < rows - 1) { const x = y % 2 === 0 ? cols - 1 : 0; open(walls, y * cols + x, S, (y + 1) * cols + x); }
    }
    return walls;
  }

  function generate(params) {
    const p = validateParams(params);
    const { cols, rows } = p, n = cols * rows;
    const start = p.start === 'center' ? ((rows / 2) | 0) * cols + ((cols / 2) | 0) : 0;
    const rand = rng(p.seed);
    const walls = (p.generator === 'prim' ? prim : p.generator === 'serpentine' ? serpentine : backtracker)(cols, rows, start, rand);
    for (let t = 0; t < n * p.loops; t++) {   // extra openings: more than one way through
      const i = (rand() * n) | 0, x = i % cols, y = (i / cols) | 0, d = DIRS[(rand() * 4) | 0];
      if (inBounds(cols, rows, x + d.dx, y + d.dy)) open(walls, i, d, (y + d.dy) * cols + x + d.dx);
    }
    return { cols, rows, walls, start, exit: n - 1, seed: p.seed };
  }

  // The shortest route from start to exit, as cells.
  function shortestRoute(maze) {
    const { cols, walls } = maze, prev = new Int32Array(walls.length).fill(-1), q = [maze.start];
    prev[maze.start] = maze.start;
    for (let h = 0; h < q.length; h++) {
      const i = q[h], x = i % cols, y = (i / cols) | 0;
      for (const d of DIRS) { if (walls[i] & d.bit) continue; const j = (y + d.dy) * cols + x + d.dx; if (prev[j] < 0) { prev[j] = i; q.push(j); } }
    }
    const path = []; for (let c = maze.exit; c !== maze.start; c = prev[c]) path.push(c);
    path.push(maze.start); return path.reverse();
  }

  // The rules: what each `./maze` command does. No drawing state in here beyond the agents' own fields.
  class Model {
    constructor(maze, params) {
      this.maze = maze; this.params = params; this.shared = params.map === 'shared';
      const n = maze.cols * maze.rows;
      this.seen = new Uint8Array(n); this.seen[maze.start] = 1;
      this.by = new Int16Array(n).fill(-1); this.by[maze.start] = 0;   // the start is credited to agent 0, never a "first"
      this.unique = 0; this.calls = 0; this.exitBy = null; this.agents = [];
    }
    register(a) {
      a.pos = this.maze.start; a.calls = 0; a.refused = 0; a.firsts = 0; a.reached = false; a.claim = null;
      a.own = null;
      if (!this.shared) { a.own = new Uint8Array(this.seen.length); a.own[this.maze.start] = 1; }
      this.agents.push(a);
    }
    done(a) { return this.shared ? this.exitBy !== null : a.reached; }
    error(a, error) { return { error, done: this.done(a) }; }
    known(a) { return this.shared ? this.seen : a.own; }

    // Limits look at the counts before this call, so call number `limit` is the last one allowed.
    refusal(a) {
      const p = this.params;
      if (a.calls >= p.step_limit) return 'step_limit';
      if (p.call_budget && this.calls >= p.call_budget) return 'budget';
      return null;
    }

    call(a, op, arg) {
      const refusal = this.refusal(a);
      a.calls++; this.calls++;
      if (refusal) { a.refused++; return this.error(a, refusal); }
      let result;
      if (op === 'look') result = this.view(a);
      else if (op === 'move') result = this.move(a, arg);
      else if (op === 'next-edge') result = this.nextEdge(a);
      else result = this.error(a, 'bad_op');
      if (result.error) a.refused++;
      return result;
    }

    // Delegate mode: the lead runs next-edge on behalf of a worker. The call (and any refusal) is the lead's;
    // the claim, the path and the starting point are the worker's.
    assign(lead, worker) {
      const refusal = this.refusal(lead);
      lead.calls++; this.calls++;
      if (refusal) { lead.refused++; return this.error(worker, refusal); }
      const result = this.nextEdge(worker);
      if (result.error) lead.refused++;
      return result;
    }

    view(a) {
      const { cols, walls, exit } = this.maze, known = this.known(a), x = a.pos % cols, y = (a.pos / cols) | 0;
      const openSides = DIRS.filter((d) => !(walls[a.pos] & d.bit));
      return { at: [x, y], open: openSides.map((d) => d.name),
        new: openSides.filter((d) => !known[(y + d.dy) * cols + x + d.dx]).map((d) => d.name),
        exit: a.pos === exit, steps: a.calls, done: this.done(a) };
    }

    move(a, arg) {
      const { cols, walls, exit } = this.maze, d = DIRS.find((q) => q.name === arg);
      if (!d) return this.error(a, 'bad_arg');
      if (walls[a.pos] & d.bit) return this.error(a, 'wall');
      const j = a.pos + d.dy * cols + d.dx;
      a.pos = j;
      if (!this.seen[j]) { this.seen[j] = 1; this.by[j] = a.k; a.firsts++; this.unique++; }
      if (a.own) a.own[j] = 1;
      if (j === exit) { a.reached = true; if (this.exitBy === null) this.exitBy = a; }
      return this.view(a);
    }

    isFrontier(i) {
      const { cols, walls } = this.maze, x = i % cols, y = (i / cols) | 0;
      return DIRS.some((d) => !(walls[i] & d.bit) && !this.seen[(y + d.dy) * cols + x + d.dx]);
    }

    // Nearest explored cell with an open side into unexplored maze that no other agent claimed; if every one is
    // claimed, the nearest claimed one. The claim is released and replaced on each call.
    nextEdge(a) {
      if (!this.shared) return this.error(a, 'own_map');
      const { cols, walls } = this.maze;
      a.claim = null;
      const taken = new Set();
      for (const o of this.agents) if (o.claim !== null) taken.add(o.claim);
      const prev = new Map([[a.pos, a.pos]]), q = [a.pos];
      let target = null, fallback = null;
      for (let h = 0; h < q.length; h++) {
        const i = q[h];
        if (i !== a.pos && this.isFrontier(i)) {
          if (!taken.has(i)) { target = i; break; }
          if (fallback === null) fallback = i;
        }
        const x = i % cols, y = (i / cols) | 0;
        for (const d of DIRS) {
          if (walls[i] & d.bit) continue;
          const j = (y + d.dy) * cols + x + d.dx;
          if (!prev.has(j) && this.seen[j]) { prev.set(j, i); q.push(j); }
        }
      }
      if (target === null) target = fallback;
      if (target === null) return { path: null, done: this.done(a) };
      a.claim = target;
      const cells = [target];
      while (cells[cells.length - 1] !== a.pos) cells.push(prev.get(cells[cells.length - 1]));
      cells.reverse();
      const names = { [-cols]: 'N', 1: 'E', [cols]: 'S', '-1': 'W' };
      const path = [];
      for (let n = 1; n < cells.length; n++) path.push(names[cells[n] - cells[n - 1]]);
      return { path, to: [target % cols, (target / cols) | 0], done: this.done(a) };
    }

    repeatPct() { return this.calls ? 100 * Math.max(0, this.calls - this.unique) / this.calls : 0; }

    // Route cells credited to whoever entered them first; empty until somebody reaches the exit.
    // On private maps (map="own"), agents share nothing, so the entire route belongs to the winner.
    credit() {
      const out = {};
      if (this.exitBy === null) return out;
      if (!this.shared) { out[this.exitBy.k] = shortestRoute(this.maze).length; return out; }
      for (const cell of shortestRoute(this.maze)) {
        const k = this.by[cell];
        if (this.agents.some((a) => a.k === k)) out[k] = (out[k] || 0) + 1;
      }
      return out;
    }
  }

  // The prototype's rule as a pure policy: result in, next command out. Mirrors Explorer in maze.py.
  class Explorer {
    constructor(seed, index, shared) {
      this.rand = rng((seed * 31 + index * 7919 + 1) >>> 0);
      this.shared = shared; this.stack = []; this.pending = [];
    }
    bash(c) { return { op: 'bash', command: './maze ' + c }; }
    final(text) { return { op: 'final', text }; }
    next(last) {
      if (last === null || last === undefined) return this.bash('look');
      let r;
      try { r = JSON.parse(last); } catch (e) { return this.final('unreadable maze output'); }
      if (r === null || typeof r !== 'object' || Array.isArray(r)) return this.final('unreadable maze output');
      if ('error' in r) return this.final('error: ' + r.error);
      if (r.done) return this.final('done: the exit has been reached');
      if ('path' in r) {
        if (r.path === null) return this.final('no open edge left');
        this.pending = r.path.slice();
        return this.pending.length ? this.bash('move ' + this.pending.shift()) : this.bash('look');
      }
      const fresh = r.new || [];
      if (fresh.length) {
        this.pending = [];
        const d = fresh[(this.rand() * fresh.length) | 0];
        this.stack.push(d);
        return this.bash('move ' + d);
      }
      if (!this.shared) return this.stack.length ? this.bash('move ' + OPPOSITE[this.stack.pop()]) : this.final('nothing left to explore');
      if (this.pending.length) return this.bash('move ' + this.pending.shift());
      return this.bash('next-edge');
    }
  }

  // ---------- stepping ----------
  // One run = one maze, N agents, one model. `stepRun` is one tick: every started agent makes one call, in index
  // order, and agent k starts at tick floor(k / 4), like process spawns. The run stops at the call that finds the exit.
  function createRun(params, count, opts) {
    const p = validateParams(params), maze = generate(p), label = (opts && opts.label) || 'swarm';
    const model = new Model(maze, p), n = maze.cols * maze.rows;
    const run = { maze, params: p, label, shared: model.shared, model, tick: 0, calls: 0, done: null, seenCount: 1, log: [],
      seen: model.seen, by: model.by, visits: new Uint16Array(n), agents: [], explorers: [] };
    run.visits[maze.start] = 1;
    for (let k = 0; k < count; k++) {
      const a = { k, id: (label === 'solo' ? 's' : 'a') + String(k + 1).padStart(2, '0'), state: 'wait', startTick: Math.floor(k / 4),
        trail: [maze.start], finished: false, last: null };
      model.register(a);
      run.agents.push(a);
      run.explorers.push(new Explorer(p.seed, k, model.shared));
    }
    return run;
  }

  function parseCommand(command) {
    const parts = command.split(/\s+/);
    return [parts[1], parts.length > 2 ? parts[2] : null];
  }

  function note(run, a, call, result, hit) {
    run.log.push({ tick: run.tick, who: a.id, k: a.k, run: run.label, call, result, hit: !!hit });
    if (run.log.length > 400) run.log.splice(0, run.log.length - 400);
  }

  function stepRun(run) {
    if (run.done) return;
    const { model, maze } = run, { cols } = maze;
    for (const a of run.agents) {
      if (run.done) break;
      if (a.finished || run.tick < a.startTick) continue;
      if (a.state === 'wait') a.state = 'explore';
      const step = run.explorers[a.k].next(a.last);
      if (step.op === 'final') {
        a.finished = true; a.state = a.reached ? 'found' : 'idle';
        note(run, a, 'final', step.text);
        continue;
      }
      const [op, arg] = parseCommand(step.command), d = DIRS.find((q) => q.name === arg);
      // Entering a cell this agent has not been to is exploring; any other move is travel.
      const fresh = op === 'move' && d && !(maze.walls[a.pos] & d.bit) && !model.known(a)[a.pos + d.dy * cols + d.dx];
      const result = model.call(a, op, arg);
      a.last = JSON.stringify(result);
      run.calls = model.calls; run.seenCount = model.unique + 1;
      if (result.error) { note(run, a, step.command.slice(7), result.error); continue; }
      if (op === 'move') {
        a.trail.push(a.pos); run.visits[a.pos]++;
        a.state = a.pos === maze.exit ? 'found' : fresh ? 'explore' : 'back';
        note(run, a, step.command.slice(7), `→ ${a.pos % cols},${(a.pos / cols) | 0}`, a.pos === maze.exit);
      } else if (op === 'next-edge') {
        note(run, a, 'next-edge', result.path ? `→ ${result.to[0]},${result.to[1]} · ${result.path.length} moves` : 'no open edge left');
      } else note(run, a, op, `${result.new.length} new`);
      if (model.exitBy !== null) {
        run.done = { tick: run.tick + 1, agent: model.exitBy, route: shortestRoute(maze) };
      }
    }
    run.tick++;
  }

  function finished(run) { return !!run.done || run.agents.every((a) => a.finished); }

  function simulate(params, agents) {
    const run = createRun(params, agents);
    while (!finished(run)) stepRun(run);
    const model = run.model;
    return {
      exit_tick: run.done ? run.done.tick : null,
      exit_agent: run.done ? run.done.agent.k : null,
      calls: model.calls,
      unique_cells: model.unique,
      repeat_pct: model.repeatPct(),
      credit: model.credit(),
      per_agent_calls: run.agents.map((a) => a.calls),
    };
  }

  // ---------- fleet modes ----------
  // A desktop agent client's orchestration shapes on the same maze. A port of simulate_fleet in maze.py: read the comments
  // there for each mode's rules; this must match it key for key and log row for log row.
  const FLEET_MODES = ['single', 'swarm', 'parallel', 'delegate', 'council'];
  const FLEET_COUNTS = { single: [1, 1], swarm: [1, 256], parallel: [2, 6], delegate: [1, 6], council: [2, 6] };
  const PARALLEL_CONCURRENCY = 3;

  function fleetAgents(model, p, count, shared) {
    const agents = [], explorers = [];
    for (let k = 0; k < count; k++) {
      const a = { k, id: String(k), last: null };
      model.register(a); agents.push(a);
      explorers.push(new Explorer(p.seed, k, shared));
    }
    return { agents, explorers };
  }

  // One turn: 'final', 'call', or 'wait' when the explorer asked for next-edge and holdNextEdge keeps it back.
  function fleetTurn(model, explorer, a, tick, log, holdNextEdge) {
    const step = explorer.next(a.last);
    if (step.op === 'final') { log.push([tick, a.k, 'final', null]); return 'final'; }
    const [op, arg] = parseCommand(step.command);
    if (holdNextEdge && op === 'next-edge') return 'wait';
    log.push([tick, a.k, op, arg]);
    a.last = JSON.stringify(model.call(a, op, arg));
    return 'call';
  }

  // Shortest start -> exit cells through open sides between known cells, or null.
  function knownRoute(maze, known) {
    const { cols, walls } = maze;
    if (!known[maze.start] || !known[maze.exit]) return null;
    const prev = new Map([[maze.start, maze.start]]), q = [maze.start];
    for (let h = 0; h < q.length; h++) {
      const i = q[h];
      if (i === maze.exit) break;
      const x = i % cols, y = (i / cols) | 0;
      for (const d of DIRS) {
        if (walls[i] & d.bit) continue;
        const j = (y + d.dy) * cols + x + d.dx;
        if (known[j] && !prev.has(j)) { prev.set(j, i); q.push(j); }
      }
    }
    if (!prev.has(maze.exit)) return null;
    const route = [maze.exit];
    while (route[route.length - 1] !== maze.start) route.push(prev.get(route[route.length - 1]));
    return route.reverse();
  }

  // Checked against the walls: start to exit, one open side per step.
  function routeIsLegal(maze, route) {
    if (!route || !route.length || route[0] !== maze.start || route[route.length - 1] !== maze.exit) return false;
    const { cols, walls } = maze;
    for (let n = 1; n < route.length; n++) {
      const i = route[n - 1], x = i % cols, y = (i / cols) | 0;
      if (!DIRS.some((d) => !(walls[i] & d.bit) && (y + d.dy) * cols + x + d.dx === route[n])) return false;
    }
    return true;
  }

  // single and swarm: exactly simulate's loop, with the log added.
  function fleetLockstep(model, p, count, log, out) {
    const { agents, explorers } = fleetAgents(model, p, count, model.shared);
    const finished = new Array(count).fill(false), started = new Array(count).fill(false);
    let tick = 0;
    while (out.exit_tick === null && !finished.every(Boolean)) {
      for (let k = 0; k < count; k++) {
        if (finished[k] || tick < Math.floor(k / 4)) continue;
        if (!started[k]) { started[k] = true; log.push([tick, k, 'start', null]); }
        if (fleetTurn(model, explorers[k], agents[k], tick, log) === 'final') { finished[k] = true; continue; }
        if (model.exitBy !== null) { out.exit_tick = tick + 1; out.exit_agent = k; break; }
      }
      tick++;
    }
    out.ticks = tick;
    return agents;
  }

  // Independent own-map runs, PARALLEL_CONCURRENCY at a time, first exit wins. With concurrency 3, runs 3..5 can
  // only start after one has finished, which in a race is after the winner: expected, not a bug.
  function fleetParallel(model, p, count, log, out) {
    const { agents, explorers } = fleetAgents(model, p, count, false);
    const started = new Array(count).fill(false), running = new Array(count).fill(false);
    let tick = 0;
    while (out.exit_tick === null) {
      for (let k = 0; k < count; k++) {
        if (running.filter(Boolean).length >= PARALLEL_CONCURRENCY) break;
        if (!started[k]) { started[k] = running[k] = true; log.push([tick, k, 'start', null]); }
      }
      if (!running.some(Boolean)) break;
      for (let k = 0; k < count; k++) {
        if (!running[k]) continue;
        if (fleetTurn(model, explorers[k], agents[k], tick, log) === 'final') { running[k] = false; continue; }
        if (model.exitBy !== null) {
          out.exit_tick = tick + 1; out.exit_agent = k; running[k] = false;
          out.cancelled = [];
          for (let j = 0; j < count; j++) if (running[j]) out.cancelled.push(j);
          for (const j of out.cancelled) log.push([tick, j, 'cancel', null]);
          break;
        }
      }
      tick++;
    }
    out.ticks = tick;
    if (out.cancelled === null) out.cancelled = [];
    out.concurrency = PARALLEL_CONCURRENCY;
    return agents;
  }

  // A lead and `count` workers on one shared map. Each tick the lead first serves at most one waiting worker (the
  // lowest index), then the workers take their turns in index order; a worker that would call next-edge waits.
  function fleetDelegate(model, p, count, log, out) {
    const { agents, explorers } = fleetAgents(model, p, count, true);
    const lead = { k: count, id: 'lead' };
    model.register(lead);
    const finished = new Array(count).fill(false), started = new Array(count).fill(false);
    const waiting = new Array(count).fill(false);
    let waitTicks = 0, tick = 0;
    log.push([0, 'lead', 'start', null]);
    while (out.exit_tick === null && !finished.every(Boolean)) {
      const w = waiting.indexOf(true);
      if (w >= 0) {
        log.push([tick, 'lead', 'assign', w]);
        agents[w].last = JSON.stringify(model.assign(lead, agents[w]));
        waiting[w] = false;
      }
      for (let k = 0; k < count; k++) {
        if (finished[k] || tick < Math.floor(k / 4)) continue;
        if (!started[k]) { started[k] = true; log.push([tick, k, 'start', null]); }
        if (waiting[k]) { waitTicks++; continue; }
        const done = fleetTurn(model, explorers[k], agents[k], tick, log, true);
        if (done === 'final') { finished[k] = true; continue; }
        if (done === 'wait') { waiting[k] = true; waitTicks++; continue; }
        if (model.exitBy !== null) { out.exit_tick = tick + 1; out.exit_agent = k; break; }
      }
      tick++;
    }
    out.ticks = tick;
    out.lead_calls = lead.calls;
    out.wait_ticks = waitTicks;
    return agents;
  }

  // Own-map members from tick 0 until each is final or out of calls, then one synthesizer call on the union of
  // their maps. Credit: the member whose move first entered each route cell (ties within a tick go to the lowest
  // index, who moves first; the start cell is member 0's).
  function fleetCouncil(model, p, count, memberBudget, log, out) {
    const maze = model.maze;
    const { agents, explorers } = fleetAgents(model, p, count, false);
    const finished = new Array(count).fill(false);
    for (let k = 0; k < count; k++) log.push([0, k, 'start', null]);
    let tick = 0;
    while (!finished.every(Boolean)) {
      for (let k = 0; k < count; k++) {
        if (finished[k]) continue;
        if (memberBudget !== null && agents[k].calls >= memberBudget) {
          finished[k] = true; log.push([tick, k, 'final', 'budget']); continue;
        }
        if (fleetTurn(model, explorers[k], agents[k], tick, log) === 'final') finished[k] = true;
      }
      tick++;
    }
    // The loop's last tick is the one in which the final member returned final: it makes no call, so it is the
    // synthesizer's tick, not an extra one. Members' calls fill ticks 0..tick-2; synth runs at tick-1.
    tick--;
    // The synthesizer: one call, not subject to the maze's limits (it never touches the maze service).
    log.push([tick, 'synth', 'synthesize', null]);
    const union = new Uint8Array(model.seen.length), memberLens = [];
    for (const a of agents) {
      for (let i = 0; i < union.length; i++) union[i] |= a.own[i];
      const r = knownRoute(maze, a.own);
      if (r !== null) memberLens.push(r.length);
    }
    const route = knownRoute(maze, union), best = memberLens.length ? Math.min(...memberLens) : null;
    out.ticks = tick + 1;
    out.extra_calls = 1;
    out.route = route;
    out.route_len = route === null ? null : route.length;
    out.best_member_route_len = best;
    out.union_shorter = route !== null && (best === null || route.length < best);
    out.verified = routeIsLegal(maze, route);
    out.member_budget = memberBudget;
    if (route !== null) {
      out.exit_tick = tick + 1;
      // Credit: the member whose move first entered the cell (model.by). Members have their own maps and only
      // their own moves set `by`, so every route cell was entered by a member who knows it; nothing else is credited.
      const credit = {};
      for (const cell of route) { const k = model.by[cell]; if (k >= 0) credit[k] = (credit[k] || 0) + 1; }
      out.credit = credit;
    }
    return agents;
  }

  function simulateFleet(params, mode, count, memberBudget, log) {
    if (!FLEET_MODES.includes(mode)) throw new Error('fleet mode must be one of ' + FLEET_MODES.join(', '));
    const [lo, hi] = FLEET_COUNTS[mode];
    if (!isInt(count) || count < lo || count > hi) throw new Error(`fleet ${mode} count must be an integer in ${lo}..${hi}`);
    if (memberBudget === undefined) memberBudget = null;
    if (mode === 'council') {
      if (memberBudget !== null && (!isInt(memberBudget) || memberBudget < 1)) throw new Error('council member_budget must be a positive integer or null');
    } else if (memberBudget !== null) throw new Error('member_budget applies to council only');
    const p = validateParams(params);
    if (mode === 'swarm' || mode === 'delegate') p.map = 'shared';
    else if (mode === 'parallel' || mode === 'council') p.map = 'own';
    const model = new Model(generate(p), p);
    if (!log) log = [];
    const out = { mode, count, exit_tick: null, exit_agent: null, ticks: 0, credit: null, lead_calls: null, wait_ticks: null,
      concurrency: null, cancelled: null, member_budget: null, route: null, route_len: null, best_member_route_len: null,
      union_shorter: null, verified: null, extra_calls: 0 };
    let agents;
    if (mode === 'single' || mode === 'swarm') agents = fleetLockstep(model, p, count, log, out);
    else if (mode === 'parallel') agents = fleetParallel(model, p, count, log, out);
    else if (mode === 'delegate') agents = fleetDelegate(model, p, count, log, out);
    else agents = fleetCouncil(model, p, count, memberBudget, log, out);
    // Totals cover every call: the lead's assignments are model calls already; the synthesizer's is added here.
    const calls = model.calls + out.extra_calls;
    delete out.extra_calls;
    out.calls = calls;
    out.per_agent_calls = agents.map((a) => a.calls);
    out.unique_cells = model.unique;
    out.repeat_pct = calls ? 100 * Math.max(0, calls - model.unique) / calls : 0;
    if (out.credit === null && mode === 'parallel') {
      // Independent attempts, best one kept: the route belongs to the winning run alone (`by` is global).
      out.credit = {};
      if (out.exit_agent !== null) out.credit[out.exit_agent] = shortestRoute(model.maze).length;
    }
    if (out.credit === null) {
      if (mode === 'council') out.credit = {};
      else { out.credit = model.credit(); delete out.credit[count]; }   // never the lead's: it does not move
    }
    return out;
  }

  return { PARAMS_DEFAULTS, validateParams, rng, generate, shortestRoute, Explorer, createRun, stepRun, finished, simulate,
    FLEET_MODES, PARALLEL_CONCURRENCY, simulateFleet };
});
