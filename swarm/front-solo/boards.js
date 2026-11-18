// The boards: pure functions from an attempt's events (and a play position) to a frame the page draws. No DOM, no
// clock, no randomness: the same events and position always give the same frame, so tests/test_solo.py runs them in
// node and checks each board against the receipt.
//
// An event is {t, kind: read|edit|exec|test|done|model|verify, arg?, ok?, bytes?}. A position `at` is a number of
// events: 2.5 means two events are done and the third is half played.
(() => {
  const hash = (s) => {
    let h = 0x811c9dc5;
    for (const ch of String(s)) { h ^= ch.codePointAt(0); h = Math.imul(h, 0x01000193) >>> 0; }
    return h >>> 0;
  };
  const split = (events, at) => {
    const n = events.length;
    const pos = Math.max(0, Math.min(n, Number(at) || 0));
    const done = Math.floor(pos);
    return { n, done, part: done < n ? pos - done : 0 };
  };
  const isFile = (e) => (e.kind === 'read' || e.kind === 'edit') && e.arg;

  // Trail (snake genre). The ant leaves the nest, walks to each file it reads (a nibble) or edits (a bite), to the
  // shell for each command, and home to the nest when it is done. Every finished step grows the trail by one; a
  // failed step knocks a segment off.
  const Trail = {
    W: 16, H: 11,
    layout(events) {
      const { W, H } = Trail;
      const nest = { x: 1, y: H - 2 }, shell = { x: W - 2, y: H - 2 };
      const taken = new Set([`${nest.x},${nest.y}`, `${shell.x},${shell.y}`]);
      const food = [];
      for (const e of events) {
        if (!isFile(e) || food.some((f) => f.label === e.arg)) continue;
        let h = hash(e.arg), x, y, guard = 0;
        do { x = 2 + (h % (W - 4)); y = 1 + (Math.floor(h / (W - 4)) % (H - 4)); h = hash(h + ':' + ++guard); }
        while (taken.has(`${x},${y}`) && guard < 64);
        taken.add(`${x},${y}`);
        food.push({ x, y, label: e.arg });
      }
      return { nest, shell, food };
    },
    target(e, lay) {
      if (isFile(e)) return lay.food.find((f) => f.label === e.arg);
      if (e.kind === 'exec' || e.kind === 'test') return lay.shell;
      return lay.nest; // done, verify, a bare model turn
    },
    // The walk: one list of cells, and for each event the slice of it that event walks. A step to where the ant
    // already stands still takes one beat (it stays put), so every event is visible.
    route(events, lay) {
      const cells = [{ x: lay.nest.x, y: lay.nest.y }];
      const spans = [];
      for (const e of events) {
        const to = Trail.target(e, lay);
        const from = cells.length;
        let { x, y } = cells[cells.length - 1];
        while (x !== to.x) { x += Math.sign(to.x - x); cells.push({ x, y }); }
        while (y !== to.y) { y += Math.sign(to.y - y); cells.push({ x, y }); }
        if (cells.length === from) cells.push({ x, y });
        spans.push([from, cells.length - 1]);
      }
      return { cells, spans };
    },
    frame(events, at) {
      const { n, done, part } = split(events, at);
      const lay = Trail.layout(events);
      const { cells, spans } = Trail.route(events, lay);
      let head = 0;
      if (done > 0) head = spans[done - 1][1];
      if (done < n && part > 0) {
        const [a, b] = spans[done];
        head = a + Math.min(b - a, Math.floor(part * (b - a + 1)));
      }
      let len = 3, knocks = 0;
      const state = new Map();
      for (let i = 0; i < done; i++) {
        const e = events[i];
        if (e.ok === false) { len = Math.max(2, len - 1); knocks++; } else if (e.kind !== 'verify') len++;
        if (isFile(e)) state.set(e.arg, e.kind === 'edit' ? 'eaten' : (state.get(e.arg) === 'eaten' ? 'eaten' : 'nibbled'));
      }
      const body = [];
      for (let i = head; i >= 0 && body.length < len; i--) {
        const c = cells[i];
        if (!body.some((b) => b.x === c.x && b.y === c.y)) body.push(c);
      }
      const last = done > 0 ? events[done - 1] : null;
      const verify = events.find((e) => e.kind === 'verify');
      return {
        board: 'trail', w: Trail.W, h: Trail.H, body, nest: lay.nest,
        shell: { ...lay.shell, lit: !!last && (last.kind === 'exec' || last.kind === 'test') },
        food: lay.food.map((f) => ({ ...f, state: state.get(f.label) || 'fresh' })),
        count: done, steps: events.filter((e) => e.kind !== 'verify').length, len, knocks,
        verified: done === n && verify ? verify.ok === true : null,
      };
    },
  };

  // Hill (stacking genre). Every model turn drops a bar; its width is how much that turn did (a read is small, a
  // command wide, the closing turn widest). A row that fills is a commit and clears. A passing verify plants a flag.
  const WIDTH = { read: 2, edit: 3, model: 2, exec: 4, test: 4, done: 5 };
  const Hill = {
    W: 10, H: 14,
    drops(events) {
      const { W } = Hill;
      const height = new Array(W).fill(0);
      let filled = [];
      const out = [];
      let cleared = 0;
      for (let i = 0; i < events.length; i++) {
        const e = events[i];
        if (e.kind === 'verify') { out.push(null); continue; }
        const w = WIDTH[e.kind] || 2;
        const x = hash(`${i}:${e.kind}:${e.arg || ''}`) % (W - w + 1);
        let y = 0;
        for (let c = x; c < x + w; c++) y = Math.max(y, height[c]);
        const cells = [];
        for (let c = x; c < x + w; c++) { height[c] = y + 1; cells.push({ x: c, y, kind: e.kind, ok: e.ok !== false }); }
        filled = filled.concat(cells);
        // A full row clears: the cells above it come down one.
        let rows = 0;
        for (let r = 0; r <= Math.max(...height); r++) {
          while (filled.filter((c) => c.y === r).length >= W) {
            filled = filled.filter((c) => c.y !== r).map((c) => (c.y > r ? { ...c, y: c.y - 1 } : c));
            for (let c = 0; c < W; c++) height[c] = Math.max(0, ...filled.filter((f) => f.x === c).map((f) => f.y + 1));
            rows++;
          }
        }
        cleared += rows;
        out.push({ x, y, w, kind: e.kind, ok: e.ok !== false, cells: filled.slice(), cleared, top: Math.max(...height) });
      }
      return out;
    },
    frame(events, at) {
      const { n, done, part } = split(events, at);
      const drops = Hill.drops(events);
      let landed = null;
      for (let i = done - 1; i >= 0; i--) if (drops[i]) { landed = drops[i]; break; }
      const next = done < n ? drops[done] : null;
      const verify = events.find((e) => e.kind === 'verify');
      return {
        board: 'hill', w: Hill.W, h: Hill.H,
        cells: landed ? landed.cells : [],
        falling: next && part > 0 ? { x: next.x, w: next.w, kind: next.kind, ok: next.ok,
          y: next.y + (Hill.H - next.y) * (1 - part) } : null,
        cleared: landed ? landed.cleared : 0, top: landed ? landed.top : 0,
        count: done, steps: events.filter((e) => e.kind !== 'verify').length,
        verified: done === n && verify ? verify.ok === true : null,
      };
    },
  };

  // The swarm: every attempt of the run as one cell, by where the run's clock stands (seconds from its first request).
  const Grid = {
    cols(n) { return Math.max(1, Math.min(16, Math.ceil(Math.sqrt(n)))); },
    state(a, t) {
      if (a.start == null || a.end == null) return a.passed ? 'pass' : 'fail';
      if (t < a.start) return 'wait';
      if (t < a.end) return 'run';
      return a.passed ? 'pass' : 'fail';
    },
    frame(attempts, t) {
      const cols = Grid.cols(attempts.length);
      const tally = { wait: 0, run: 0, pass: 0, fail: 0 };
      const cells = attempts.map((a, i) => {
        const s = Grid.state(a, t);
        tally[s]++;
        return { x: i % cols, y: Math.floor(i / cols), state: s, id: a.id };
      });
      return { cols, rows: Math.ceil(attempts.length / cols), cells, tally };
    },
    span(attempts) {
      const ends = attempts.map((a) => a.end).filter((v) => v != null);
      return ends.length ? Math.max(...ends) : 0;
    },
  };

  const Boards = { trail: Trail, hill: Hill, grid: Grid, hash, frame: (board, events, at) => (board === 'hill' ? Hill : Trail).frame(events, at) };
  if (typeof window !== 'undefined') window.Boards = Boards;
  if (typeof module !== 'undefined') module.exports = Boards;
})();
