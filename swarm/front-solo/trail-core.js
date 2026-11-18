/* Trail core for the browser and for node: the rules, the scripted policy and an all-scripted simulation.
 *
 * A port of src/swarm/trail.py; it must stay tick for tick identical to it (tests/test_trail.py runs both on seeded
 * games and compares the logs). Change one, change the other.
 *
 * Cell layout: index = y * cols + x, y grows downward. Headings N, E, S, W.
 *
 * Teams (swarm style "shared"): `new Model(p, names, teams)`; without teams each player is a team of one. A team's
 * score is its members' tokens; standings and the end compare teams (trail.py `team_rank`).
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.TrailCore = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const SIZES = { s: [20, 13], m: [28, 18], l: [36, 23] };
  const HEADINGS = ['N', 'E', 'S', 'W'];
  const DELTA = { N: [0, -1], E: [1, 0], S: [0, 1], W: [-1, 0] };
  const LEFT = { N: 'W', W: 'S', S: 'E', E: 'N' };
  const RIGHT = { W: 'N', S: 'W', E: 'S', N: 'E' };
  const OPPOSITE = { N: 'S', E: 'W', S: 'N', W: 'E' };
  const MAX_PLAYERS = 5, START_LEN = 3, FLOOD_CAP = 48, DEFAULT_CLOCK = 300, MAX_PLAN = 12;

  // Seat k of n: [x, y, heading] of the head. Lanes across the grid, alternating east and west (trail.py `seat`).
  function seat(cols, rows, k, n) {
    const y = Math.floor(rows * (k + 1) / (n + 1)), margin = Math.floor(cols * 2 / 10);
    return k % 2 === 0 ? [margin, y, 'E'] : [cols - 1 - margin, y, 'W'];
  }

  function rng(seed) {
    let a = seed >>> 0;
    return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  }

  function params(size, seed, players, clock, tokens) {
    if (!SIZES[size]) throw new Error('trail size must be s, m or l');
    const [cols, rows] = SIZES[size];
    players = players || 2;
    return { size, cols, rows, seed: seed >>> 0, players, clock: clock || DEFAULT_CLOCK, tokens: tokens == null ? players + 1 : tokens };
  }

  // Lexicographic compare of two equal-length number arrays (Python tuple order).
  function cmp(a, b) { for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] > b[i] ? 1 : -1; return 0; }

  class Model {
    constructor(p, names, teams) {
      if (names.length !== p.players) throw new Error('one name per player');
      if (teams && teams.length !== names.length) throw new Error('one team per player');
      this.p = p; this.cols = p.cols; this.rows = p.rows; this.clock = p.clock;
      this.rand = rng(p.seed); this.tick = 0;
      this.players = []; this.by = {};
      names.forEach((who, k) => {
        const [x, y, h] = seat(this.cols, this.rows, k, names.length), [dx, dy] = DELTA[h];
        const body = [];
        for (let i = 0; i < START_LEN; i++) body.push((y - dy * i) * this.cols + (x - dx * i));
        const pl = { who, seat: k, team: teams ? teams[k] : who, body, heading: h, pending: null, plan: [], alive: true, tokens: 0, died: null, cause: null };
        this.players.push(pl); this.by[who] = pl;
      });
      this.teams = [];
      for (const pl of this.players) if (!this.teams.includes(pl.team)) this.teams.push(pl.team);
      this.tokens = []; this.over = false; this.reason = null; this.winner = null;
      this.last = { eaten: [], out: [] };
      while (this.tokens.length < p.tokens && this.spawn());
    }

    xy(i) { return [i % this.cols, Math.floor(i / this.cols)]; }

    members(team) { return this.players.filter((pl) => pl.team === team); }
    teamTokens(team) { return this.members(team).reduce((a, pl) => a + pl.tokens, 0); }

    occupied() {
      const s = new Set();
      for (const pl of this.players) if (pl.alive) for (const c of pl.body) s.add(c);
      return s;
    }

    spawn() {
      const taken = this.occupied();
      for (const t of this.tokens) taken.add(t);
      const free = [];
      for (let i = 0; i < this.cols * this.rows; i++) if (!taken.has(i)) free.push(i);
      if (!free.length) return false;
      this.tokens.push(free[Math.floor(this.rand() * free.length)]);
      return true;
    }

    turn(who, rel) {
      const pl = this.by[who];
      if (this.over || !pl.alive) return { error: pl.alive ? 'over' : 'out', done: true };
      if (rel !== 'L' && rel !== 'R' && rel !== 'S') return { error: 'usage: ./trail turn L|R|S' };
      pl.pending = rel === 'L' ? LEFT[pl.heading] : rel === 'R' ? RIGHT[pl.heading] : null;
      pl.plan = [];
      return { ok: true, tick: this.tick, next_heading: pl.pending || pl.heading };
    }

    // An agent's plan (trail.py `set_plan`): up to MAX_PLAN relative moves, one taken per tick.
    setPlan(who, moves) {
      const pl = this.by[who];
      if (this.over || !pl.alive) return { error: pl.alive ? 'over' : 'out', done: true };
      moves = typeof moves === 'string' ? moves.trim().toUpperCase() : '';
      if (moves.length < 1 || moves.length > MAX_PLAN || /[^LRS]/.test(moves)) return { error: `usage: ./trail plan <moves>, 1 to ${MAX_PLAN} of L, R, S (e.g. SSLSSR)` };
      pl.pending = null; pl.plan = moves.split('');
      return { ok: true, tick: this.tick, plan: moves };
    }

    // The human's absolute steer; reversing onto yourself is ignored.
    steer(who, d) {
      const pl = this.by[who];
      if (this.over || !pl.alive || !DELTA[d] || d === OPPOSITE[pl.heading]) return false;
      pl.pending = d === pl.heading ? null : d;
      pl.plan = [];
      return true;
    }

    // The heading the trail will take at the next tick.
    nextHeading(who) { const pl = this.by[who]; return pl.pending || pl.heading; }

    step() {
      if (this.over) return this.last;
      const live = this.players.filter((pl) => pl.alive), heads = {};
      for (const pl of live) {
        if (pl.pending) { pl.heading = pl.pending; pl.pending = null; }
        else if (pl.plan.length) { const rel = pl.plan.shift(); pl.heading = rel === 'L' ? LEFT[pl.heading] : rel === 'R' ? RIGHT[pl.heading] : pl.heading; }
        const [x, y] = this.xy(pl.body[0]), [dx, dy] = DELTA[pl.heading], nx = x + dx, ny = y + dy;
        heads[pl.who] = nx >= 0 && nx < this.cols && ny >= 0 && ny < this.rows ? ny * this.cols + nx : null;
      }
      const tokens = new Set(this.tokens), grow = {};
      for (const pl of live) grow[pl.who] = heads[pl.who] !== null && tokens.has(heads[pl.who]);
      for (const pl of live) if (!grow[pl.who]) pl.body.pop();
      const occupied = new Set();
      for (const pl of live) for (const c of pl.body) occupied.add(c);
      const count = new Map();
      for (const pl of live) { const c = heads[pl.who]; if (c !== null) count.set(c, (count.get(c) || 0) + 1); }
      const eaten = [], out = [];
      for (const pl of live) {
        const c = heads[pl.who];
        const cause = c === null ? 'wall' : occupied.has(c) ? 'trail' : count.get(c) > 1 ? 'head' : null;
        if (cause) { pl.alive = false; pl.died = this.tick + 1; pl.cause = cause; out.push(pl.who); }
      }
      for (const pl of live) {
        if (!pl.alive) { pl.body = []; continue; }
        const c = heads[pl.who];
        pl.body.unshift(c);
        const at = this.tokens.indexOf(c);
        if (grow[pl.who] && at >= 0) { this.tokens.splice(at, 1); pl.tokens++; eaten.push([pl.who, c]); }
      }
      while (this.tokens.length < this.p.tokens && this.spawn());
      this.tick++;
      const running = new Set(this.players.filter((pl) => pl.alive).map((pl) => pl.team)).size;
      if (running === 0 || (this.teams.length > 1 && running <= 1)) this.finish('out');
      else if (this.tick >= this.clock) this.finish('clock');
      this.last = { tick: this.tick, eaten, out };
      return this.last;
    }

    finish(reason) {
      if (this.over) return;
      this.over = true; this.reason = reason; this.winner = this.leader();
    }

    rank(pl) { return [pl.tokens, pl.died !== null ? pl.died : this.clock + 1]; }

    standings() {
      return this.players.map((pl, i) => [pl, i]).sort((a, b) => cmp(this.rank(b[0]), this.rank(a[0])) || a[1] - b[1]).map((x) => x[0]);
    }

    // [team tokens, how long the team lasted] (trail.py `team_rank`).
    teamRank(team) {
      const ms = this.members(team);
      const lasted = ms.some((pl) => pl.alive) ? this.clock + 1 : Math.max(...ms.map((pl) => pl.died || 0));
      return [ms.reduce((a, pl) => a + pl.tokens, 0), lasted];
    }

    teamStandings() {
      return this.teams.map((t, i) => [t, i]).sort((a, b) => cmp(this.teamRank(b[0]), this.teamRank(a[0])) || a[1] - b[1]).map((x) => x[0]);
    }

    leader() {
      const r = this.teamStandings();
      if (r.length > 1 && cmp(this.teamRank(r[0]), this.teamRank(r[1])) === 0) return null;
      return r[0];
    }
  }

  // ---------- the scripted policy (the twin of trail.py `choose`) ----------
  function viewOf(m, who) {
    const me = m.by[who];
    return { cols: m.cols, rows: m.rows, head: me.body[0], heading: me.heading, len: me.body.length, blocked: m.occupied(),
      tokens: m.tokens.slice(), heads: m.players.filter((pl) => pl.alive && pl !== me).map((pl) => [pl.body[0], pl.heading]) };
  }

  function flood(v, start, cap) {
    const { cols, rows, blocked } = v, seen = new Set([start]), queue = [start];
    let k = 0;
    while (k < queue.length && seen.size < cap) {
      const c = queue[k++], x = c % cols, y = Math.floor(c / cols);
      for (const h of HEADINGS) {
        const [dx, dy] = DELTA[h], nx = x + dx, ny = y + dy;
        if (nx >= 0 && nx < cols && ny >= 0 && ny < rows) {
          const n = ny * cols + nx;
          if (!blocked.has(n) && !seen.has(n)) {
            seen.add(n); queue.push(n);
            if (seen.size >= cap) break;
          }
        }
      }
    }
    return Math.min(seen.size, cap);
  }

  function choose(v) {
    const { cols, rows } = v, hx = v.head % cols, hy = Math.floor(v.head / cols);
    const threat = new Set();
    for (const [c, h] of v.heads) {
      const x = c % cols, y = Math.floor(c / cols);
      for (const h2 of [h, LEFT[h], RIGHT[h]]) {
        const [dx, dy] = DELTA[h2], nx = x + dx, ny = y + dy;
        if (nx >= 0 && nx < cols && ny >= 0 && ny < rows) threat.add(ny * cols + nx);
      }
    }
    const need = Math.min(FLOOD_CAP, v.len + 4);
    let best = 'S', bestKey = null;
    ['S', 'L', 'R'].forEach((rel, k) => {
      const h = rel === 'S' ? v.heading : rel === 'L' ? LEFT[v.heading] : RIGHT[v.heading];
      const [dx, dy] = DELTA[h], nx = hx + dx, ny = hy + dy;
      if (!(nx >= 0 && nx < cols && ny >= 0 && ny < rows)) return;
      const c = ny * cols + nx;
      if (v.blocked.has(c) || (v.avoid && v.avoid.has(c))) return;
      const space = flood(v, c, FLOOD_CAP);
      let dist = null;
      for (const t of v.tokens) { const d = Math.abs((t % cols) - nx) + Math.abs(Math.floor(t / cols) - ny); if (dist === null || d < dist) dist = d; }
      const key = [space >= need ? 1 : 0, threat.has(c) ? 0 : 1, -(dist || 0), space, -k];
      if (bestKey === null || cmp(key, bestKey) > 0) { best = rel; bestKey = key; }
    });
    return best;
  }

  function stepTo(cols, rows, head, heading, rel) {
    const h = rel === 'S' ? heading : rel === 'L' ? LEFT[heading] : RIGHT[heading];
    const [dx, dy] = DELTA[h], x = head % cols + dx, y = Math.floor(head / cols) + dy;
    return x >= 0 && x < cols && y >= 0 && y < rows ? y * cols + x : null;
  }

  // Split the tokens among teammates (trail.py `claims`): closest (member, token) pair first. heads: [[who, cell]].
  function claims(cols, heads, tokens) {
    const pairs = [];
    for (const [who, h] of heads) tokens.forEach((t, k) => pairs.push([Math.abs((t % cols) - (h % cols)) + Math.abs(Math.floor(t / cols) - Math.floor(h / cols)), who, k]));
    pairs.sort((a, b) => a[0] - b[0] || (a[1] < b[1] ? -1 : a[1] > b[1] ? 1 : 0) || a[2] - b[2]);
    const out = {}, taken = new Set();
    for (const [, who, k] of pairs) if (!(who in out) && !taken.has(k)) { out[who] = tokens[k]; taken.add(k); }
    return out;
  }

  // The scripted move of every running trail not in skip (trail.py `team_turns`): teammates split the tokens and
  // never take a cell a teammate earlier in seat order has just chosen.
  function teamTurns(m, skip) {
    skip = skip || ['you'];
    const out = {}, chosen = {}, split = {};
    for (const pl of m.players) {
      if (!pl.alive || skip.includes(pl.who)) continue;
      const v = viewOf(m, pl.who);
      const mates = m.players.filter((q) => q.alive && q.team === pl.team && q !== pl);
      if (mates.length) {
        if (!(pl.team in split)) {
          const running = m.players.filter((q) => q.alive && q.team === pl.team).map((q) => [q.who, q.body[0]])
            .sort((a, b) => (a[0] < b[0] ? -1 : a[0] > b[0] ? 1 : a[1] - b[1]));
          split[pl.team] = claims(m.cols, running, m.tokens.slice());
        }
        const mine = split[pl.team][pl.who];
        if (mine !== undefined) v.tokens = [mine];
        v.avoid = chosen[pl.team] || new Set();
      }
      const rel = choose(v);
      out[pl.who] = rel;
      if (mates.length) {
        const nxt = stepTo(m.cols, m.rows, pl.body[0], pl.heading, rel);
        if (nxt !== null) (chosen[pl.team] = chosen[pl.team] || new Set()).add(nxt);
      }
    }
    return out;
  }

  function simulate(p, names, maxTicks, teams) {
    names = names || Array.from({ length: p.players }, (_, k) => `p${k}`);
    const m = new Model(p, names, teams), log = [];
    while (!m.over && (maxTicks == null || m.tick < maxTicks)) {
      const moves = [], turns = teamTurns(m, []);
      for (const pl of m.players) {
        if (pl.alive) { const rel = turns[pl.who]; m.turn(pl.who, rel); moves.push(rel); } else moves.push('-');
      }
      const ev = m.step();
      log.push(moves.join('') + '|' + ev.eaten.map(([w, c]) => `${w}:${c}`).join(',') + '|' + ev.out.join(','));
    }
    return { tick: m.tick, over: m.over, reason: m.reason, winner: m.winner, tokens: m.tokens.slice(),
      players: m.players.map((pl) => ({ who: pl.who, tokens: pl.tokens, alive: pl.alive, died: pl.died, cause: pl.cause, len: pl.body.length })),
      teams: m.teams.map((t) => ({ team: t, tokens: m.teamTokens(t) })), log };
  }

  return { SIZES, HEADINGS, DELTA, LEFT, RIGHT, OPPOSITE, MAX_PLAYERS, MAX_PLAN, seat, START_LEN, FLOOD_CAP, DEFAULT_CLOCK, rng, params, Model, viewOf, flood, choose, claims, teamTurns, simulate };
});
