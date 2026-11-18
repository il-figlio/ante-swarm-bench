/* Hill core for the browser and for node: the wells, the piece sequence, garbage, the scripted policy and an
 * all-scripted simulation.
 *
 * A port of src/swarm/hill.py; it must stay drop for drop identical to it (tests/test_hill.py runs both on seeded
 * games and compares the logs and the wells). Change one, change the other.
 *
 * A well is WIDTH x HEIGHT cells, row 0 at the top: 0 empty, 1 a locked block, 2 garbage.
 *
 * Teams (swarm style "pooled"): `new Model(seed, names, teams)`. A member of a team of several wells draws its own
 * sequence (`wellSeed`), the team's rows are pooled, garbage goes team to team (to the human's well; the human's
 * round the swarm's standing wells in turn), and the clock compares rows per well (hill.py `team_rank`).
 */
(function (root, factory) {
  const api = factory();
  if (typeof module === 'object' && module.exports) module.exports = api;
  else root.HillCore = api;
})(typeof self !== 'undefined' ? self : this, function () {
  'use strict';

  const WIDTH = 9, HEIGHT = 18;
  const PIECES = [
    ['pip', [[0, 0], [1, 0]]],
    ['bar', [[0, 0], [1, 0], [2, 0]]],
    ['elbow', [[0, 0], [0, 1], [1, 1]]],
    ['cup', [[0, 0], [2, 0], [0, 1], [1, 1], [2, 1]]],
    ['flag', [[0, 0], [1, 0], [0, 1], [1, 1], [0, 2]]],
    ['angle', [[0, 0], [0, 1], [0, 2], [1, 2], [2, 2]]],
    ['post', [[0, 0], [1, 0], [2, 0], [1, 1], [1, 2]]],
  ];
  const NAMES = PIECES.map((p) => p[0]);
  const SEND = [0, 0, 1, 3];
  const MAX_PLAYERS = 5, CLOCK_S = 180, FUSE_S = 6;

  function rng(seed) {
    let a = seed >>> 0;
    return () => { a = (a + 0x6D2B79F5) >>> 0; let t = a; t = Math.imul(t ^ (t >>> 15), t | 1);
      t ^= t + Math.imul(t ^ (t >>> 7), t | 61); return ((t ^ (t >>> 14)) >>> 0) / 4294967296; };
  }
  function cmp(a, b) { for (let i = 0; i < a.length; i++) if (a[i] !== b[i]) return a[i] > b[i] ? 1 : -1; return 0; }

  function norm(cells) {
    const mx = Math.min(...cells.map((c) => c[0])), my = Math.min(...cells.map((c) => c[1]));
    return cells.map(([x, y]) => [x - mx, y - my]).sort((a, b) => a[1] - b[1] || a[0] - b[0]);
  }
  function rotate(cells) {
    const h = Math.max(...cells.map((c) => c[1])) + 1;
    return norm(cells.map(([x, y]) => [h - 1 - y, x]));
  }
  const SHAPES = PIECES.map(([, cells]) => {
    const rots = [norm(cells)];
    for (let i = 0; i < 3; i++) rots.push(rotate(rots[rots.length - 1]));
    return rots;
  });
  const size = (shape) => [Math.max(...shape.map((c) => c[0])) + 1, Math.max(...shape.map((c) => c[1])) + 1];
  const spawn = (piece) => [Math.floor((WIDTH - size(SHAPES[piece][0])[0]) / 2), 0];

  // hill.py `well_seed`: the round seed plus the seat, spread over 32 bits.
  const wellSeed = (seed, seat) => (seed + Math.imul(0x9E3779B9 | 0, seat)) >>> 0;

  class Sequence {
    constructor(seed) { this.rand = rng(seed); this.items = []; }
    at(k) {
      while (this.items.length <= k) {
        const bag = PIECES.map((_, i) => i);
        for (let i = bag.length - 1; i > 0; i--) { const j = Math.floor(this.rand() * (i + 1)); [bag[i], bag[j]] = [bag[j], bag[i]]; }
        this.items.push(...bag);
      }
      return this.items[k];
    }
  }

  // The row the shape's top lands on when dropped at col (may be negative), or null for a column off the well.
  function land(cells, shape, col) {
    const [w, h] = size(shape);
    if (col < 0 || col > WIDTH - w) return null;
    const fits = (top) => {
      for (const [x, y] of shape) { const r = top + y; if (r >= HEIGHT || (r >= 0 && cells[r * WIDTH + col + x])) return false; }
      return true;
    };
    let top = -h;
    while (fits(top + 1)) top++;
    return top;
  }

  function place(cells, shape, col) {
    const top = land(cells, shape, col);
    if (top === null) return null;
    const out = cells.slice();
    let over = false;
    for (const [x, y] of shape) { const r = top + y; if (r < 0) over = true; else out[r * WIDTH + col + x] = 1; }
    const kept = [];
    for (let r = 0; r < HEIGHT; r++) { const row = out.slice(r * WIDTH, (r + 1) * WIDTH); if (!row.every((v) => v)) kept.push(row); }
    const cleared = HEIGHT - kept.length, rows = [];
    for (let i = 0; i < cleared; i++) rows.push(new Array(WIDTH).fill(0));
    return [[].concat(...rows, ...kept), cleared, over];
  }

  function heights(cells) {
    const out = [];
    for (let x = 0; x < WIDTH; x++) {
      let h = 0;
      for (let r = 0; r < HEIGHT; r++) if (cells[r * WIDTH + x]) { h = HEIGHT - r; break; }
      out.push(h);
    }
    return out;
  }
  function holes(cells) {
    let n = 0;
    for (let x = 0; x < WIDTH; x++) { let roof = false; for (let r = 0; r < HEIGHT; r++) { if (cells[r * WIDTH + x]) roof = true; else if (roof) n++; } }
    return n;
  }

  class Well {
    constructor(who, seat, seed, team, seq) {
      this.who = who; this.seat = seat; this.team = team || who; this.seq = seq || new Sequence(seed);
      this.cells = new Array(WIDTH * HEIGHT).fill(0);
      this.k = 0; this.rows = 0; this.sent = 0; this.got = 0; this.pending = []; this.out = false; this.outAt = null; this.drops = 0;
      this.grand = rng((seed + 1013 * (seat + 1)) >>> 0);
    }
    height() { return Math.max(...heights(this.cells)); }
    rise(n) {
      const hole = Math.floor(this.grand() * WIDTH), spill = this.cells.slice(0, n * WIDTH).some((v) => v);
      const row = new Array(WIDTH).fill(2); row[hole] = 0;
      const add = [];
      for (let i = 0; i < n; i++) add.push(...row);
      this.cells = this.cells.slice(n * WIDTH).concat(add);
      this.got += n;
      return !spill;
    }
  }

  class Model {
    constructor(seed, names, teams) {
      if (names.length < 1 || names.length > MAX_PLAYERS) throw new Error('hill players must be 1..5');
      if (teams && teams.length !== names.length) throw new Error('one team per player');
      this.seed = seed >>> 0; this.seq = new Sequence(this.seed);
      teams = teams ? teams.slice() : names.slice();
      const size = {};
      for (const t of teams) size[t] = (size[t] || 0) + 1;
      this.wells = names.map((who, k) => new Well(who, k, this.seed, teams[k],
        size[teams[k]] > 1 && k > 0 ? new Sequence(wellSeed(this.seed, k)) : this.seq));
      this.by = {}; for (const w of this.wells) this.by[w.who] = w;
      this.teams = [];
      for (const w of this.wells) if (!this.teams.includes(w.team)) this.teams.push(w.team);
      this.turn = {}; for (const t of this.teams) this.turn[t] = -1;
      this.drops = 0; this.over = false; this.reason = null; this.winner = null;
    }
    piece(who) { const w = this.by[who]; return w.seq.at(w.k); }
    nextPiece(who) { const w = this.by[who]; return w.seq.at(w.k + 1); }
    members(team) { return this.wells.filter((w) => w.team === team); }
    // hill.py `target`: the next team round the ring with a well standing, its standing wells in rotation.
    target(w) {
      const n = this.teams.length, at = this.teams.indexOf(w.team);
      for (let i = 1; i < n; i++) {
        const team = this.teams[(at + i) % n], standing = this.members(team).filter((t) => !t.out);
        if (standing.length) { const pick = standing.find((t) => t.seat > this.turn[team]) || standing[0]; this.turn[team] = pick.seat; return pick; }
      }
      return null;
    }
    drop(who, col, rot) {
      const w = this.by[who];
      if (this.over) return { error: 'over', done: true };
      if (w.out) return { error: 'out', done: true };
      if (!Number.isInteger(col) || !Number.isInteger(rot)) return { error: 'usage: ./hill drop <col> <rot>' };
      if (rot < 0 || rot > 3) return { error: 'rot must be 0, 1, 2 or 3' };
      const p = this.piece(who), shape = SHAPES[p][rot], got = place(w.cells, shape, col);
      if (got === null) return { error: `col must be 0..${WIDTH - size(shape)[0]} for ${NAMES[p]} at rot ${rot}` };
      const [cells, cleared, topped] = got;
      w.cells = cells; this.drops++; w.drops++; w.k++; w.rows += cleared;
      const sent = cleared < SEND.length ? SEND[cleared] : cleared;
      let target = null;
      if (topped) this.outWell(w);
      else {
        if (sent) { target = this.target(w); if (target) { target.pending.push(sent); w.sent += sent; } }
        while (w.pending.length && !w.out) if (!w.rise(w.pending.shift())) this.outWell(w);
      }
      this.check();
      return { ok: true, piece: NAMES[p], col, rot, cleared, sent: target ? sent : 0, to: target ? target.who : null, out: w.out, done: this.over || w.out };
    }
    outWell(w) { w.out = true; w.outAt = this.drops; w.pending = []; }
    check() {
      const standing = new Set(this.wells.filter((w) => !w.out).map((w) => w.team)).size;
      if (standing === 0 || (this.teams.length > 1 && standing <= 1)) this.finish('out');
    }
    finish(reason) { if (this.over) return; this.over = true; this.reason = reason; this.winner = this.leader(); }
    rank(w) { return [w.out ? 0 : 1, w.outAt || 0, w.rows, -w.height()]; }
    standings() {
      return this.wells.map((w, i) => [w, i]).sort((a, b) => cmp(this.rank(b[0]), this.rank(a[0])) || a[1] - b[1]).map((x) => x[0]);
    }
    teamRows(team) { return this.members(team).reduce((a, w) => a + w.rows, 0); }
    perWell(team) { const ms = this.members(team); return ms.reduce((a, w) => a + w.rows, 0) / ms.length; }
    // hill.py `team_rank`: standing, last out, rows per well, minus the average stack.
    teamRank(team) {
      const ms = this.members(team), standing = ms.some((w) => !w.out);
      const outAt = standing ? 0 : Math.max(...ms.map((w) => w.outAt || 0));
      return [standing ? 1 : 0, outAt, ms.reduce((a, w) => a + w.rows, 0) / ms.length, -ms.reduce((a, w) => a + w.height(), 0) / ms.length];
    }
    teamStandings() {
      return this.teams.map((t, i) => [t, i]).sort((a, b) => cmp(this.teamRank(b[0]), this.teamRank(a[0])) || a[1] - b[1]).map((x) => x[0]);
    }
    teamPublic(team) {
      const ms = this.members(team), rows = ms.reduce((a, w) => a + w.rows, 0);
      return { team, members: ms.map((w) => w.who), rows, per_well: rows / ms.length, standing: ms.filter((w) => !w.out).length, sent: ms.reduce((a, w) => a + w.sent, 0) };
    }
    leader() {
      const r = this.teamStandings();
      if (r.length > 1 && cmp(this.teamRank(r[0]), this.teamRank(r[1])) === 0) return null;
      return r[0];
    }
  }

  // ---------- the scripted policy (the twin of hill.py `choose`) ----------
  function score(cells, cleared) {
    const hs = heights(cells);
    let bump = 0, agg = 0;
    for (let i = 0; i < WIDTH; i++) { agg += hs[i]; if (i < WIDTH - 1) bump += Math.abs(hs[i] - hs[i + 1]); }
    return 76 * cleared - 51 * agg - 36 * holes(cells) - 18 * bump;
  }
  function choose(cells, piece) {
    let best = null, bestKey = null;
    for (let rot = 0; rot < 4; rot++) {
      const shape = SHAPES[piece][rot];
      for (let col = 0; col <= WIDTH - size(shape)[0]; col++) {
        const [c2, cleared, out] = place(cells, shape, col), key = [out ? 0 : 1, score(c2, cleared)];
        if (bestKey === null || cmp(key, bestKey) > 0) { best = [col, rot]; bestKey = key; }
      }
    }
    return best;
  }

  function simulate(seed, players, maxBeats, teams) {
    maxBeats = maxBeats == null ? 120 : maxBeats;
    const names = Array.from({ length: players }, (_, k) => `p${k}`), m = new Model(seed, names, teams), log = [];
    let beat = 0;
    while (!m.over && beat < maxBeats) {
      beat++;
      const moves = [];
      for (const w of m.wells) {
        if (w.out || m.over) { moves.push('-'); continue; }
        const [col, rot] = choose(w.cells, m.piece(w.who)), r = m.drop(w.who, col, rot);
        moves.push(`${NAMES[w.seq.at(w.k - 1)][0]}${col}${rot}${r.cleared}${r.sent}${r.to || ''}`);
      }
      log.push(moves.join(' '));
    }
    if (!m.over) m.finish('clock');
    return { beats: beat, over: m.over, reason: m.reason, winner: m.winner,
      wells: m.wells.map((w) => ({ who: w.who, rows: w.rows, sent: w.sent, got: w.got, out: w.out, k: w.k, cells: w.cells.join('') })),
      teams: m.teams.map((t) => m.teamPublic(t)), log };
  }

  return { WIDTH, HEIGHT, PIECES, NAMES, SEND, MAX_PLAYERS, CLOCK_S, FUSE_S, SHAPES, rng, wellSeed, size, spawn, land, place, heights, holes, Sequence, Well, Model, score, choose, simulate };
});
