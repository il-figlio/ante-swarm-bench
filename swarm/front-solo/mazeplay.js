// The AX-1's maze cartridge, the rules half: round settings, the opponents, and the replay of a simulated fleet's
// call log tick by tick. No DOM, no clock, no randomness: maze.js draws, and tests/test_solo.py runs this in node
// against front-maze/maze-core.js.
//
// The opponents are not real Ante processes. Each is maze-core.js's simulateFleet (the maze eval's scripted explorer,
// in one of the fleet orchestration shapes) run to the end when the round is set up; the page then replays its
// log at the chosen pace, one tick per beat, so what you race is exactly what the simulation did.
(() => {
  const SIZES = { s: [16, 10], m: [26, 16], l: [40, 25] };
  const PACES = [3, 6, 12];

  // The first three are the race's original opponents; their best-time keys (1, 4, 16) are kept so bests set on
  // the first race page still count here.
  const OPPONENTS = [
    { id: 'solo', mode: 'single', count: 1, key: '1', label: 'ante ×1', team: 'ante',
      about: 'one Ante on one map: it steps into sides it has not seen and asks next-edge for the nearest unexplored edge when it has none.' },
    { id: 'swarm4', mode: 'swarm', count: 4, key: '4', label: 'swarm ×4', team: 'the swarm',
      about: 'four Antes on one shared map; next-edge hands each a different unexplored edge.' },
    { id: 'swarm16', mode: 'swarm', count: 16, key: '16', label: 'swarm ×16', team: 'the swarm',
      about: 'sixteen Antes on one shared map, four more starting each tick; next-edge spreads them out.' },
    { id: 'parallel3', mode: 'parallel', count: 3, key: 'parallel3', label: 'parallel ×3', team: 'parallel',
      about: 'three independent Antes, each on its own map with no next-edge (they back up at dead ends); the first out wins and the rest are cancelled.' },
    { id: 'delegate4', mode: 'delegate', count: 4, key: 'delegate4', label: 'delegate 1+4', team: 'delegate',
      about: 'a lead and four workers on one shared map; only the lead runs next-edge, for one waiting worker per tick.' },
    { id: 'council3', mode: 'council', count: 3, key: 'council3', label: 'council ×3', team: 'the council',
      about: 'three Antes each map the maze alone until they reach the exit; then one synthesizer call joins their maps into a route. It finishes when the last member does.' },
  ];
  const CALLS = new Set(['look', 'move', 'next-edge', 'assign', 'synthesize']);
  const STEP = { N: [0, -1, 1], E: [1, 0, 2], S: [0, 1, 4], W: [-1, 0, 8] };
  const PREFIX = { swarm: 'a', parallel: 'r', delegate: 'w', council: 'm' };

  const opponent = (id) => OPPONENTS.find((o) => o.id === id) || OPPONENTS[0];
  const params = (size, seed) => {
    const [cols, rows] = SIZES[size] || SIZES.m;
    return { cols, rows, seed, generator: 'prim', loops: 0.05, start: 'center', map: 'shared' };
  };
  const bestKey = (size, id, pace) => `maze-race:${size}:${opponent(id).key}:${pace}`;
  const name = (o, who) => (typeof who !== 'number' ? who : o.mode === 'single' ? 'ante' : PREFIX[o.mode] + String(who + 1).padStart(2, '0'));

  // The opponent run to its end, once. `endTick` is when the race is over for them: the tick after the call that
  // reached the exit (or, for a council, the synthesizer's), else the tick they all stopped.
  function plan(Core, prm, id) {
    const o = opponent(id), log = [];
    const out = Core.simulateFleet(prm, o.mode, o.count, null, log);
    return { opp: o, out, log, stepLimit: Core.validateParams(prm).step_limit,
      found: out.exit_tick !== null, endTick: out.exit_tick !== null ? out.exit_tick : out.ticks };
  }

  // Where every simulated Ante stands after `tick` ticks, rebuilt from the log alone: a move goes through unless a
  // wall or the step limit refuses it, exactly as the model decided when the log was written.
  class Replay {
    constructor(maze, pl) {
      this.maze = maze; this.plan = pl; this.at = 0; this.tick = 0; this.calls = 0; this.route = null; this.recent = [];
      this.seen = new Uint8Array(maze.cols * maze.rows); this.seen[maze.start] = 1;
      this.agents = [];
      for (let k = 0; k < pl.opp.count; k++) this.agents.push({ k, name: name(pl.opp, k), pos: maze.start, trail: [maze.start], state: 'wait', calls: 0 });
      this.lead = { calls: 0 };
    }

    mapped() {
      let n = 0;
      for (const v of this.seen) n += v;
      return n;
    }

    advance(tick) {
      const { log } = this.plan, { cols, walls, exit } = this.maze;
      while (this.at < log.length && log[this.at][0] < tick) {
        const [t, who, op, arg] = log[this.at++];
        const a = typeof who === 'number' ? this.agents[who] : null;
        if (op === 'start') { if (a) a.state = 'run'; continue; }
        if (op === 'final' || op === 'cancel') {
          if (a) a.state = op === 'cancel' ? 'cancelled' : a.pos === exit ? 'found' : 'stopped';
          this.note(t, who, op === 'final' && arg ? `final ${arg}` : op, false);
          continue;
        }
        if (!CALLS.has(op)) continue;
        this.calls++;
        if (op === 'synthesize') { this.route = this.plan.out.route; this.note(t, who, 'synthesize', this.route !== null); continue; }
        const caller = who === 'lead' ? this.lead : a;
        const refused = caller.calls >= this.plan.stepLimit;
        caller.calls++;
        let hit = false;
        if (op === 'move' && a && !refused) {
          const d = STEP[arg];
          if (d && !(walls[a.pos] & d[2])) {
            a.pos += d[1] * cols + d[0];
            a.trail.push(a.pos);
            this.seen[a.pos] = 1;
            hit = a.pos === exit;
          }
        }
        this.note(t, who, op === 'assign' ? `assign ${name(this.plan.opp, arg)}` : arg ? `${op} ${arg}` : op, hit);
      }
      this.tick = Math.max(this.tick, tick);
    }

    note(tick, who, call, hit) {
      this.recent.push({ tick, who: name(this.plan.opp, who), call, hit });
      if (this.recent.length > 48) this.recent.splice(0, this.recent.length - 48);
    }
  }

  const MazePlay = { SIZES, PACES, OPPONENTS, opponent, params, bestKey, plan, Replay };
  if (typeof window !== 'undefined') window.MazePlay = MazePlay;
  if (typeof module !== 'undefined') module.exports = MazePlay;
})();
