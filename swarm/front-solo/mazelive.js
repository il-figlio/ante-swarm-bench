// The AX-1's maze cartridge, the live half: a round against real Ante processes on this server, through /api/play/*
// (docs/live.md). No DOM and no drawing: maze.js draws what this holds, and tests/test_play_front.py runs it in
// node against a stubbed fetch, EventSource and storage.
//
// What the page may know during a round is what the server sends the player's team: your own moves (the move reply),
// the ally's moves and both of your discoveries (`move` and `reveal` for team "player"), each rival's bucketed
// progress (`rival`) and the call feed (`call`). Rival positions arrive only with `end`; a rival `move` or `reveal`
// sent by mistake is dropped here, never drawn.
//
// The passcode lives in sessionStorage under one key and is sent only in POST /api/play/rounds. A 403 clears it.
//
// The rival swarm plays as one team: a lead hands out work and the workers report on a shared board of notes. The page
// learns each rival's role, and how many notes their board holds, never a note's text, until `end`, which carries
// both boards. The ally's own notes are your team's and show in the call feed as they are posted.
(() => {
  const BIT = { N: 1, E: 2, S: 4, W: 8 };
  const STEP = { N: [0, -1], E: [1, 0], S: [0, 1], W: [-1, 0] };
  const BUCKETS = ['far', 'mid', 'near'];
  // The model the server's Antes run, as /api/play/status names it (its config, not this page). Shown as text only.
  const modelName = (st) => (st && typeof st.model === 'string' && /^[A-Za-z0-9._:-]{1,64}$/.test(st.model) ? st.model : 'model');
  const PASS_KEY = 'maze-live:passcode';
  const EVENTS = ['state', 'move', 'reveal', 'rival', 'call', 'end'];
  // Live v1 runs single and swarm only (docs/live.md, "Round model"); the other cartridge opponents stay scripted.
  const LIVE = { solo: { mode: 'single', count: 1 }, swarm4: { mode: 'swarm', count: 4 }, swarm16: { mode: 'swarm', count: 16 } };
  const FEED = 32;           // call feed rows kept
  const NOTE_CHARS = 120;    // the server's note length; anything longer is cut here too
  const ROLES = ['lead', 'worker', 'rival'];
  const SIZES = [1, 4, 16];  // the live counts the maze offers when the server does not say
  const QUEUE = 2;           // moves waiting behind the one in flight; more presses are dropped, not stacked up
  const END_WAIT = 4000;     // after a win reply or a give-up, how long to wait for the server's `end` before closing locally

  const isInt = (v) => typeof v === 'number' && Number.isInteger(v);

  // A position as a cell index (y * cols + x, as maze-core.js). The server may send an index, [x, y] or {x, y}.
  function cellOf(p, cols) {
    if (isInt(p)) return p;
    if (Array.isArray(p) && p.length === 2 && isInt(p[0]) && isInt(p[1])) return p[1] * cols + p[0];
    if (p && isInt(p.x) && isInt(p.y)) return p.y * cols + p.x;
    return null;
  }

  // Wall bits (N=1 E=2 S=4 W=8, set = wall) from the open sides of the move reply.
  function wallsFromOpen(open) {
    let w = 15;
    for (const d of open || []) if (BIT[d]) w &= ~BIT[d];
    return w;
  }

  // One revealed cell: [i, walls] or {i|cell|pos, walls}. Plain indexes carry no walls and are refused.
  function revealed(c, cols) {
    if (Array.isArray(c) && c.length === 2 && isInt(c[1])) { const i = cellOf(c[0], cols); return i === null ? null : [i, c[1] & 15]; }
    if (c && typeof c === 'object' && isInt(c.walls)) { const i = cellOf(c.i ?? c.cell ?? c.pos, cols); return i === null ? null : [i, c.walls & 15]; }
    return null;
  }

  const antes = (opts) => (LIVE[opts.opp] ? LIVE[opts.opp].count : 0) + (opts.assist ? 1 : 0);

  // The live counts the server offers for the maze (status.sizes), or the maze's own when it does not say.
  function sizesOf(st) {
    const s = st && Array.isArray(st.sizes) ? st.sizes.filter((n) => isInt(n) && n > 0) : [];
    return s.length ? s : SIZES.slice();
  }
  // Whether a cartridge opponent can be raced live on this server.
  const offered = (st, opp) => !!LIVE[opp] && sizesOf(st).includes(LIVE[opp].count);

  const plainText = (v, n) => (typeof v === 'string' ? v.replace(/[\u0000-\u001f\u007f-\u009f\u200b-\u200f\u202a-\u202e\u2066-\u2069]/g, ' ').replace(/\s+/g, ' ').trim().slice(0, n) : '');

  // A team board from `end`: [{who, text}], plain text, at most 20 notes of NOTE_CHARS.
  function board(list) {
    const out = [];
    for (const n of Array.isArray(list) ? list : []) {
      const text = plainText(n && n.text, NOTE_CHARS), who = plainText(n && n.who, 8);
      if (text) out.push({ who: who || '?', text });
    }
    return out.slice(-20);
  }

  // The status line under the live switch: what /api/play/status says, in words.
  function statusLine(st, count) {
    if (!st) return 'live · checking the server…';
    if (st.error === 'absent') return 'live off · this server has no live rounds';
    if (st.error) return 'live off · server unreachable';
    if (st.live) return `live · ${modelName(st)} · ${count} ${count === 1 ? 'ante' : 'antes'}${st.queue ? ` · ${st.queue} waiting` : ''}`;
    if (st.reason === 'no_passcode_configured') return 'live off · no passcode configured on this server';
    if (st.reason === 'credits') return 'live off · out of model credits';
    if (st.reason === 'busy') return `live busy · a round is running · queue ${st.queue || 0}`;
    if (st.reason === 'memory') return 'live off · the server is short of memory';
    if (st.reason === 'limit') return 'live off · model usage limit reached · back after it resets';
    if (st.reason === 'signin') return 'live off · the server\'s model sign-in needs renewing';
    if (st.reason === 'tokens' || st.reason === 'budget') return 'live off · token budget spent for today';
    return 'live off';
  }

  const winnerOf = (w) => (w === 'player' || w === 'you' || w === 'ally' ? 'you' : w === 'rival' ? 'opp' : null);

  class Client {
    constructor(o) {
      this.fetch = o.fetch; this.ES = o.EventSource; this.storage = o.storage; this.now = o.now;
      this.onChange = o.onChange || (() => {});
      this.setTimeout = o.setTimeout || ((f, ms) => setTimeout(f, ms));
      this.clearTimeout = o.clearTimeout || ((t) => clearTimeout(t));
      this.base = o.base || '';
      this.st = null; this.r = null; this.denied = false; this.notice = null;
    }

    emit() { try { this.onChange(this); } catch (e) { /* drawing must not break the client */ } }

    // ---------- passcode: sessionStorage only ----------
    get passcode() { try { return this.storage.getItem(PASS_KEY) || ''; } catch (e) { return ''; } }
    setPasscode(p) {
      const v = String(p || '').trim();
      if (!v) return false;
      try { this.storage.setItem(PASS_KEY, v); } catch (e) { return false; }
      this.denied = false; this.emit();
      return true;
    }
    clearPasscode() { try { this.storage.removeItem(PASS_KEY); } catch (e) { /* nothing kept */ } }

    // ---------- status ----------
    async status() {
      let st;
      try {
        const res = await this.fetch(`${this.base}/api/play/status`, { cache: 'no-store' });
        if (res.status === 404) st = { error: 'absent' };
        else if (!res.ok) st = { error: `http ${res.status}` };
        else st = await res.json();
      } catch (e) { st = { error: 'network' }; }
      this.st = st; this.emit();
      return st;
    }

    // ---------- a round ----------
    // opts: {size, opp, assist, dims: {cols, rows, start, exit}}. Resolves to {ok} | {need: 'passcode'} | {denied} |
    // {busy, queue} | {limited} | {error}.
    async start(opts) {
      if (this.r && !this.r.over) return { error: 'running' };
      const rival = LIVE[opts.opp];
      if (!rival) return { error: 'scripted only' };
      const passcode = this.passcode;
      if (!passcode) return { need: 'passcode' };
      const r = this.r = this.blank(opts);
      this.notice = null; this.emit();
      let res, body = null;
      try {
        res = await this.fetch(`${this.base}/api/play/rounds`, {
          method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ passcode, size: opts.size, rival: { mode: rival.mode, count: rival.count, ...(opts.shape && rival.mode === 'swarm' ? { shape: opts.shape } : {}) }, assist: !!opts.assist }),
        });
        try { body = await res.json(); } catch (e) { body = null; }
      } catch (e) {
        if (this.r === r) this.r = null;
        this.notice = 'server unreachable'; this.emit();
        return { error: 'network' };
      }
      if (this.r !== r) return { error: 'cancelled' };
      if (res.status === 201 && body && body.round && body.token && r.gaveUp) {
        // Given up while the round was being made: end it at once rather than leave Antes running for nobody.
        try { await this.fetch(`${this.base}/api/play/rounds/${encodeURIComponent(body.round)}`, { method: 'DELETE', headers: { 'X-Round-Token': String(body.token) } }); } catch (e) { /* the 30 s reader timeout ends it */ }
        this.r = null; this.emit();
        return { error: 'cancelled' };
      }
      if (res.status === 201 && body && body.round && body.token) {
        r.id = String(body.round); r.token = String(body.token); r.seedHash = body.seed_hash || null;
        r.shape = typeof body.shape === 'string' ? body.shape : null;
        r.phase = isInt(body.queue) && body.queue > 0 ? 'queued' : 'starting'; r.queuePos = isInt(body.queue) ? body.queue : null;
        this.denied = false;
        this.listen(r);
        this.emit();
        return { ok: true };
      }
      this.r = null;
      let out;
      if (res.status === 403) { this.clearPasscode(); this.denied = true; this.notice = 'wrong passcode'; out = { denied: true }; }
      else if (res.status === 409) { const q = body && isInt(body.queue) ? body.queue : null; this.notice = `busy · queue ${q ?? '?'}`; out = { busy: true, queue: q }; }
      else if (res.status === 429) { this.notice = 'too many tries · wait a few minutes'; out = { limited: true }; }
      else if (body && typeof body.reason === 'string') { this.notice = statusLine({ live: false, reason: body.reason }, 0); out = { error: body.reason }; }
      else { this.notice = `could not start (http ${res.status})`; out = { error: `http ${res.status}` }; }
      this.emit();
      return out;
    }

    blank(opts) {
      const { cols, rows } = opts.dims, n = cols * rows;
      const rival = LIVE[opts.opp];
      return {
        id: null, token: null, seedHash: null, opts, phase: 'posting', over: false,
        cols, rows, start: opts.dims.start, exit: opts.dims.exit,
        walls: new Int8Array(n).fill(-1),         // -1: not seen by your team
        you: { pos: opts.dims.start, trail: [opts.dims.start], moves: 0, bumps: 0 },
        ally: opts.assist ? { pos: null, trail: [] } : null,
        rivals: new Map(), expect: rival.count, feed: [], queue: [], inflight: false,
        t0: null, end: null, lastCall: null, lastAlly: null, rtt: null, bumpAt: -1e9, moveError: null,
        stream: 'connecting', es: null, queuePos: null, won: false, gaveUp: false, timer: null,
        winner: null, reason: null, stats: null, maze: null, paths: null,
        shape: null, rivalNotes: 0, allyNotes: 0, notes: null,
      };
    }

    listen(r) {
      if (!this.ES) { r.stream = 'unsupported'; return; }
      const url = `${this.base}/api/play/rounds/${encodeURIComponent(r.id)}/events?t=${encodeURIComponent(r.token)}`;
      const es = r.es = new this.ES(url);
      for (const type of EVENTS) {
        es.addEventListener(type, (m) => {
          if (this.r !== r) return;
          let d;
          try { d = JSON.parse(m.data); } catch (e) { return; }
          this.handle(type, d);
        });
      }
      es.onopen = () => { if (this.r === r && !r.over) { r.stream = 'open'; this.emit(); } };
      es.onerror = () => {
        if (this.r !== r || r.over) return;
        // EventSource reconnects by itself (readyState CONNECTING) and the server resends `state`. CLOSED is final.
        r.stream = es.readyState === 2 ? 'lost' : 'reconnecting';
        this.emit();
      };
    }

    known(i) { return this.r && this.r.walls[i] >= 0; }

    setCell(r, i, walls) {
      if (!isInt(i) || i < 0 || i >= r.walls.length) return;
      r.walls[i] = walls & 15;
    }

    // One server-sent event. Public so tests (and a reconnecting stream) can feed it directly.
    handle(type, d) {
      const r = this.r;
      if (!r || !d || typeof d !== 'object') return;
      const now = this.now();
      if (type === 'end') return this.finish(r, d);
      if (r.over) return;
      if (type === 'state') {
        if (isInt(d.cols) && isInt(d.rows) && (d.cols !== r.cols || d.rows !== r.rows)) {
          r.cols = d.cols; r.rows = d.rows; r.walls = new Int8Array(d.cols * d.rows).fill(-1);
        }
        if (isInt(d.start)) r.start = d.start;
        if (isInt(d.exit)) r.exit = d.exit;
        for (const c of d.seen || []) { const v = revealed(c, r.cols); if (v) this.setCell(r, v[0], v[1]); }
        const you = d.you || {};
        const pos = cellOf(you.pos, r.cols);
        if (pos !== null && !r.inflight) { r.you.pos = pos; if (r.you.trail[r.you.trail.length - 1] !== pos) r.you.trail.push(pos); }
        if (isInt(you.moves)) r.you.moves = you.moves;
        if (r.ally && d.ally) { const a = cellOf(d.ally.pos, r.cols); if (a !== null) { r.ally.pos = a; if (!r.ally.trail.length) r.ally.trail.push(a); } }
        for (const v of d.rivals || []) if (v && v.who != null) this.rival(r, v.who, { ...(v.progress || v), role: v.role }, now, false);
        if (typeof d.shape === 'string') r.shape = d.shape;
        r.stream = 'open';
        if (d.phase === 'queued') {   // waiting behind another round: nothing runs yet, the clock has not started
          r.phase = 'queued'; r.queuePos = isInt(d.queue) ? d.queue : r.queuePos;
        } else {
          const el = typeof d.t === 'number' ? d.t : typeof d.elapsed_s === 'number' ? d.elapsed_s : null;
          if (r.t0 === null) r.t0 = el !== null ? now - el * 1000 : now;
          r.phase = d.phase === 'over' ? 'finishing' : 'live';
        }
      } else if (type === 'move') {
        if (d.team !== 'player') return;   // rival positions are never drawn before the end
        const pos = cellOf(d.pos, r.cols);
        if (pos === null) return;
        if (d.who === 'you') {
          if (!r.inflight && !r.queue.length && pos !== r.you.pos) { r.you.pos = pos; r.you.trail.push(pos); }
        } else if (r.ally) {
          r.ally.pos = pos; r.ally.trail.push(pos); r.lastAlly = now;
          if (r.ally.trail.length > 4000) r.ally.trail.splice(0, r.ally.trail.length - 4000);
        }
      } else if (type === 'reveal') {
        if (d.team !== 'player') return;
        for (const c of d.cells || []) { const v = revealed(c, r.cols); if (v) this.setCell(r, v[0], v[1]); }
      } else if (type === 'rival') {
        if (d.who == null) return;
        this.rival(r, d.who, d.progress || {}, now, true);
      } else if (type === 'call') {
        if (d.who == null || !d.op) return;
        const ally = d.team === 'player' || d.who === 'ally';
        // A rival's call goes in the feed as its op alone: its arguments and results could name where it is.
        // A note: the ally's text is your team's and is shown; a rival's is only the count on their board.
        const row = { who: String(d.who), op: String(d.op), arg: ally && d.arg != null ? String(d.arg) : null,
          result: ally && d.result != null ? String(d.result) : null, team: ally ? 'player' : 'rival', at: now,
          text: ally && d.op === 'note' ? plainText(d.text, NOTE_CHARS) || null : null,
          notes: !ally && d.op === 'note' && isInt(d.notes) ? d.notes : null };
        if (row.notes !== null) r.rivalNotes = Math.max(r.rivalNotes, row.notes);
        if (row.text) r.allyNotes++;
        r.feed.push(row);
        if (r.feed.length > FEED) r.feed.splice(0, r.feed.length - FEED);
        r.lastCall = now;
        if (ally) r.lastAlly = now;
        else { const v = r.rivals.get(row.who); if (v) v.at = now; }
      } else return;
      this.emit();
    }

    rival(r, who, p, now, live) {
      const k = String(who), prev = r.rivals.get(k) || { who: k, role: null, calls: 0, mapped: 0, notes: 0, bucket: null, state: 'run', at: null };
      const v = { ...prev };
      if (isInt(p.calls)) v.calls = p.calls;
      if (isInt(p.mapped)) v.mapped = p.mapped;
      if (BUCKETS.includes(p.bucket)) v.bucket = p.bucket;
      if (typeof p.state === 'string') v.state = p.state;
      if (isInt(p.notes)) v.notes = p.notes;
      if (ROLES.includes(p.role)) v.role = p.role;
      if (live && v.calls > prev.calls) { v.at = now; r.lastCall = now; }
      r.rivals.set(k, v);
    }

    // ---------- your moves: one request in flight, in order ----------
    move(dir) {
      const r = this.r;
      if (!r || r.phase !== 'live' || r.won || !BIT[dir]) return false;
      if (r.queue.length >= QUEUE) return false;
      r.queue.push(dir);
      this.pump(r);
      return true;
    }

    async pump(r) {
      while (this.r === r && !r.inflight && r.queue.length && r.phase === 'live' && !r.won) {
        const dir = r.queue.shift(), w = r.walls[r.you.pos];
        if (w >= 0 && (w & BIT[dir])) {   // a wall you can see: a bump, and the server is not asked
          r.you.bumps++; r.bumpAt = this.now(); this.emit();
          continue;
        }
        r.inflight = true;
        const t = this.now();
        try {
          const res = await this.fetch(`${this.base}/api/play/rounds/${encodeURIComponent(r.id)}/move`, {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Round-Token': r.token },
            body: JSON.stringify({ dir }),
          });
          r.rtt = this.now() - t;
          if (res.ok) {
            const d = await res.json(), pos = cellOf(d.pos, r.cols);
            if (pos !== null) {
              if (d.ok && pos !== r.you.pos) { r.you.moves++; r.you.trail.push(pos); }
              else if (!d.ok) { r.you.bumps++; r.bumpAt = this.now(); }
              r.you.pos = pos;
              if (Array.isArray(d.open)) this.setCell(r, pos, wallsFromOpen(d.open));
            }
            r.moveError = null;
            if (d.won) { r.won = true; r.queue.length = 0; this.wait(r, { winner: 'player', reason: 'exit' }); }
          } else {
            r.moveError = res.status === 403 ? 'round token refused' : `move refused (http ${res.status})`;
            if (res.status === 404 || res.status === 410 || res.status === 409) r.queue.length = 0;
          }
        } catch (e) {
          r.moveError = 'network';
          r.queue.length = 0;
        }
        r.inflight = false;
        this.emit();
      }
    }

    // ---------- the end ----------
    // Give up: DELETE the round. The server's `end` (with the maze) is the round's real end; without one soon after,
    // the round is closed here.
    async giveUp() {
      const r = this.r;
      if (!r || r.over) return false;
      r.gaveUp = true; r.queue.length = 0; this.emit();
      if (!r.id) return true;   // still being made: start() ends it when the server answers
      try {
        await this.fetch(`${this.base}/api/play/rounds/${encodeURIComponent(r.id)}`, { method: 'DELETE', headers: { 'X-Round-Token': r.token } });
      } catch (e) { /* the server also ends a round with no reader after 30 s */ }
      this.wait(r, { winner: null, reason: 'gave_up' });
      return true;
    }

    // The page is going away: tell the server without waiting (fetch keepalive).
    leave() {
      const r = this.r;
      if (!r || r.over || !r.id) return;
      try { this.fetch(`${this.base}/api/play/rounds/${encodeURIComponent(r.id)}`, { method: 'DELETE', keepalive: true, headers: { 'X-Round-Token': r.token } }); } catch (e) { /* best effort */ }
      if (r.es) r.es.close();
    }

    wait(r, fallback) {
      if (r.timer) return;
      r.phase = 'finishing';
      r.timer = this.setTimeout(() => { r.timer = null; if (!r.over && this.r === r) this.finish(r, { ...fallback, local: true }); }, END_WAIT);
    }

    finish(r, d) {
      if (r.over) return;
      r.over = true; r.phase = 'over'; r.end = this.now(); r.inflight = false; r.queue.length = 0;
      if (r.timer) { this.clearTimeout(r.timer); r.timer = null; }
      if (r.es) { r.es.close(); r.stream = 'closed'; }
      r.winner = winnerOf(d.winner); r.reason = typeof d.reason === 'string' ? d.reason : null;
      r.stats = d.stats && typeof d.stats === 'object' ? d.stats : null;
      r.local = !!d.local;
      if (typeof d.shape === 'string') r.shape = d.shape;
      // Both teams' boards, now the round is over: the rivals' chatter is the player's to read.
      if (d.notes && typeof d.notes === 'object') r.notes = { rival: board(d.notes.rival), player: board(d.notes.player) };
      if (d.roles && typeof d.roles === 'object') {
        for (const [who, role] of Object.entries(d.roles)) { const v = r.rivals.get(who); if (v && ROLES.includes(role)) v.role = role; }
      }
      const m = d.maze;
      if (m && isInt(m.cols) && isInt(m.rows) && Array.isArray(m.walls) && m.walls.length === m.cols * m.rows) {
        r.maze = { cols: m.cols, rows: m.rows, walls: Uint8Array.from(m.walls, (w) => w & 15),
          start: isInt(m.start) ? m.start : r.start, exit: isInt(m.exit) ? m.exit : r.exit };
        r.cols = m.cols; r.rows = m.rows; r.exit = r.maze.exit;
      }
      const paths = (m && m.paths) || d.paths;
      if (paths && typeof paths === 'object') {
        r.paths = {};
        const cols = r.cols;
        for (const [who, cells] of Object.entries(paths)) {
          if (!Array.isArray(cells)) continue;
          const out = cells.map((c) => cellOf(c, cols)).filter((c) => c !== null);
          if (out.length) r.paths[who] = out;
        }
      }
      if (r.t0 === null) r.t0 = r.end;
      this.emit();
    }

    // Seconds since the round went live, or its length once over.
    elapsed(now) {
      const r = this.r;
      if (!r || r.t0 === null) return null;
      return ((r.over ? r.end : now) - r.t0) / 1000;
    }
  }

  const MazeLive = { BIT, STEP, BUCKETS, ROLES, SIZES, NOTE_CHARS, modelName, PASS_KEY, LIVE, EVENTS, cellOf, wallsFromOpen, revealed, statusLine, antes, sizesOf, offered, board, plainText, Client };
  if (typeof window !== 'undefined') window.MazeLive = MazeLive;
  if (typeof module !== 'undefined') module.exports = MazeLive;
})();
