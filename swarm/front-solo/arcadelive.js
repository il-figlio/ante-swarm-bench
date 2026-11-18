// The AX-1's trail and hill cartridges, the live half: a round against real Ante processes on this server, through
// /api/play/<game>/* (docs/live.md, "Games: trail and hill"). No DOM and no drawing: trail.js and hill.js draw what
// this holds, and tests/test_arcade_front.py runs it in node against a stubbed fetch, EventSource and storage.
//
// The same patterns as mazelive.js: the passcode lives in sessionStorage under the same key (one unlock serves every
// cartridge in the tab) and is sent only in POST rounds; a 403 clears it. Trail and hill have no hidden board, so
// every `frame` the server sends is drawn as it comes.
//
// Opponents: each game declares a swarm style and the sizes it offers in GET /api/play/<game>/status
// ({swarm_style: shared|pooled, sizes: [1, n...]}); `shape` reads them (the built-in default when the server has no
// live rounds), and the cartridge builds its opponent keys from them in live and scripted mode alike, labelled by
// `label`: 1 is "ante ×1", more is "swarm ×n".
(() => {
  const PASS_KEY = 'maze-live:passcode';
  const EVENTS = ['state', 'frame', 'call', 'end'];
  const SHAPES = { trail: { style: 'shared', sizes: [1, 3] }, hill: { style: 'pooled', sizes: [1, 4] } };
  const label = (n) => (n === 1 ? 'ante ×1' : `swarm ×${n}`);
  const rival = (n) => (n === 1 ? { mode: 'single', count: 1 } : { mode: 'swarm', count: n });
  // The game's swarm style and sizes from a status answer, else the built-in default for the game.
  function shape(game, st) {
    const d = SHAPES[game] || { style: 'shared', sizes: [1] };
    if (!st || st.error || !Array.isArray(st.sizes)) return { style: d.style, sizes: d.sizes.slice(), server: false };
    const sizes = st.sizes.filter((n) => isInt(n) && n >= 1 && n <= 16).slice(0, 6);
    if (!sizes.includes(1)) return { style: d.style, sizes: d.sizes.slice(), server: false };
    const style = st.swarm_style === 'shared' || st.swarm_style === 'pooled' ? st.swarm_style : d.style;
    return { style, sizes, server: true };
  }
  const FEED = 32, QUEUE = 2, END_WAIT = 4000;
  const isInt = (v) => typeof v === 'number' && Number.isInteger(v);
  const modelName = (st) => (st && typeof st.model === 'string' && /^[A-Za-z0-9._:-]{1,64}$/.test(st.model) ? st.model : 'model');

  function statusLine(st, count) {
    if (!st) return 'live · checking the server…';
    if (st.error === 'absent') return 'live off · this server has no live rounds';
    if (st.error) return 'live off · server unreachable';
    if (st.live) return `live · ${modelName(st)} · ${count} ${count === 1 ? 'ante' : 'antes'}`;
    if (st.reason === 'no_passcode_configured') return 'live off · no passcode configured on this server';
    if (st.reason === 'credits') return 'live off · out of model credits';
    if (st.reason === 'busy') return `live busy · a round is running · queue ${st.queue || 0}`;
    if (st.reason === 'memory') return 'live off · the server is short of memory';
    if (st.reason === 'limit') return 'live off · model usage limit reached · back after it resets';
    if (st.reason === 'signin') return 'live off · the server\'s model sign-in needs renewing';
    if (st.reason === 'tokens' || st.reason === 'budget') return 'live off · token budget spent for today';
    return 'live off';
  }

  class Client {
    constructor(o) {
      this.game = o.game; this.fetch = o.fetch; this.ES = o.EventSource; this.storage = o.storage; this.now = o.now;
      this.onChange = o.onChange || (() => {});
      this.setTimeout = o.setTimeout || ((f, ms) => setTimeout(f, ms));
      this.clearTimeout = o.clearTimeout || ((t) => clearTimeout(t));
      this.base = `${o.base || ''}/api/play/${o.game}`;
      this.st = null; this.r = null; this.denied = false; this.notice = null;
    }

    get shape() { return shape(this.game, this.st); }

    emit() { try { this.onChange(this); } catch (e) { /* drawing must not break the client */ } }

    get passcode() { try { return this.storage.getItem(PASS_KEY) || ''; } catch (e) { return ''; } }
    setPasscode(p) {
      const v = String(p || '').trim();
      if (!v) return false;
      try { this.storage.setItem(PASS_KEY, v); } catch (e) { return false; }
      this.denied = false; this.emit();
      return true;
    }
    clearPasscode() { try { this.storage.removeItem(PASS_KEY); } catch (e) { /* nothing kept */ } }

    async status() {
      let st;
      try {
        const res = await this.fetch(`${this.base}/status`, { cache: 'no-store' });
        if (res.status === 404) st = { error: 'absent' };
        else if (!res.ok) st = { error: `http ${res.status}` };
        else st = await res.json();
      } catch (e) { st = { error: 'network' }; }
      this.st = st; this.emit();
      return st;
    }

    // opts: {count}. Resolves to {ok} | {need: 'passcode'} | {denied} | {busy, queue} | {limited} | {error}.
    async start(opts) {
      if (this.r && !this.r.over) return { error: 'running' };
      if (!this.shape.sizes.includes(opts.count)) return { error: 'not offered' };
      const want = rival(opts.count);
      const passcode = this.passcode;
      if (!passcode) return { need: 'passcode' };
      const r = this.r = this.blank(opts);
      this.notice = null; this.emit();
      let res, body = null;
      try {
        res = await this.fetch(`${this.base}/rounds`, { method: 'POST', headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ passcode, rival: { mode: want.mode, count: want.count } }) });
        try { body = await res.json(); } catch (e) { body = null; }
      } catch (e) {
        if (this.r === r) this.r = null;
        this.notice = 'server unreachable'; this.emit();
        return { error: 'network' };
      }
      if (this.r !== r) return { error: 'cancelled' };
      if (res.status === 201 && body && body.round && body.token) {
        r.id = String(body.round); r.token = String(body.token); r.seedHash = body.seed_hash || null;
        if (r.gaveUp) { this.sendDelete(r); this.r = null; this.emit(); return { error: 'cancelled' }; }
        r.phase = 'starting'; this.denied = false;
        this.listen(r); this.emit();
        return { ok: true };
      }
      this.r = null;
      let out;
      if (res.status === 403) { this.clearPasscode(); this.denied = true; this.notice = 'wrong passcode'; out = { denied: true }; }
      else if (res.status === 409) { const q = body && isInt(body.queue) ? body.queue : null; this.notice = `busy · another round is running${q ? ` · queue ${q}` : ''}`; out = { busy: true, queue: q }; }
      else if (res.status === 429) { this.notice = 'too many tries · wait a few minutes'; out = { limited: true }; }
      else if (body && typeof body.reason === 'string') { this.notice = statusLine({ live: false, reason: body.reason }, 0); out = { error: body.reason }; }
      else { this.notice = `could not start (http ${res.status})`; out = { error: `http ${res.status}` }; }
      this.emit();
      return out;
    }

    blank(opts) {
      return { id: null, token: null, seedHash: null, opts, phase: 'posting', over: false, frame: null, rivals: [],
        feed: [], queue: [], inflight: false, t0: null, goAt: null, end: null, limit: null, tickS: null, lastCall: null,
        rtt: null, actError: null, stream: 'connecting', es: null, gaveUp: false, timer: null, winner: null,
        leader: null, reason: null, stats: null, seed: null, nonce: null, notes: null, local: false, frameAt: null, version: 0 };
    }

    listen(r) {
      if (!this.ES) { r.stream = 'unsupported'; return; }
      const es = r.es = new this.ES(`${this.base}/rounds/${encodeURIComponent(r.id)}/events?t=${encodeURIComponent(r.token)}`);
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
        r.stream = es.readyState === 2 ? 'lost' : 'reconnecting';
        this.emit();
      };
    }

    handle(type, d) {
      const r = this.r;
      if (!r || !d || typeof d !== 'object') return;
      const now = this.now();
      if (type === 'end') return this.finish(r, d);
      if (r.over) return;
      if (type === 'state') {
        if (d.frame && typeof d.frame === 'object') this.setFrame(r, d.frame, now);
        if (Array.isArray(d.rivals)) r.rivals = d.rivals.map(String);
        if (typeof d.limit_s === 'number') r.limit = d.limit_s;
        if (typeof d.tick_s === 'number') r.tickS = d.tick_s;
        r.stream = 'open';
        if (d.phase === 'count') { r.phase = 'count'; r.goAt = now + (typeof d.go_in === 'number' ? d.go_in : 3) * 1000; }
        else if (d.phase === 'running') { r.phase = 'live'; if (r.t0 === null) r.t0 = now - (typeof d.t === 'number' ? d.t : 0) * 1000; }
        else if (d.phase === 'over') r.phase = 'finishing';
        else r.phase = 'starting';
      } else if (type === 'frame') {
        this.setFrame(r, d, now);
        if (r.phase !== 'live' && r.phase !== 'finishing') { r.phase = 'live'; if (r.t0 === null) r.t0 = now; }
      } else if (type === 'call') {
        if (d.who == null || !d.op) return;
        r.feed.push({ who: String(d.who), op: String(d.op), arg: d.arg != null ? String(d.arg) : null, refused: d.refused === true, at: now });
        if (r.feed.length > FEED) r.feed.splice(0, r.feed.length - FEED);
        r.lastCall = now;
      } else return;
      this.emit();
    }

    setFrame(r, f, now) { r.frame = f; r.frameAt = now; r.version++; }

    // ---------- your actions: one request in flight, in order ----------
    act(body) {
      const r = this.r;
      if (!r || r.phase !== 'live' || r.over) return false;
      if (r.queue.length >= QUEUE) return false;
      r.queue.push(body);
      this.pump(r);
      return true;
    }

    async pump(r) {
      while (this.r === r && !r.inflight && r.queue.length && r.phase === 'live' && !r.over) {
        const body = r.queue.shift();
        r.inflight = true;
        const t = this.now();
        try {
          const res = await this.fetch(`${this.base}/rounds/${encodeURIComponent(r.id)}/act`, {
            method: 'POST', headers: { 'Content-Type': 'application/json', 'X-Round-Token': r.token }, body: JSON.stringify(body) });
          r.rtt = this.now() - t;
          let d = null;
          try { d = await res.json(); } catch (e) { d = null; }
          r.last = d;
          if (res.ok) r.actError = null;
          else {
            r.actError = res.status === 403 ? 'round token refused' : d && d.error ? String(d.error) : `refused (http ${res.status})`;
            if (res.status === 404 || res.status === 410 || res.status === 409) r.queue.length = 0;
          }
        } catch (e) { r.actError = 'network'; r.queue.length = 0; }
        r.inflight = false;
        this.emit();
      }
    }

    sendDelete(r, keepalive) {
      try { return this.fetch(`${this.base}/rounds/${encodeURIComponent(r.id)}`, { method: 'DELETE', keepalive: !!keepalive, headers: { 'X-Round-Token': r.token } }); } catch (e) { return null; }
    }

    async giveUp() {
      const r = this.r;
      if (!r || r.over) return false;
      r.gaveUp = true; r.queue.length = 0; this.emit();
      if (!r.id) return true;
      try { await this.sendDelete(r); } catch (e) { /* the server ends a round with no reader after 30 s */ }
      this.wait(r, { winner: null, reason: 'gave_up' });
      return true;
    }

    leave() {
      const r = this.r;
      if (!r || r.over || !r.id) return;
      this.sendDelete(r, true);
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
      r.winner = typeof d.winner === 'string' ? d.winner : null;
      r.leader = typeof d.leader === 'string' ? d.leader : null;
      r.reason = typeof d.reason === 'string' ? d.reason : null;
      r.stats = d.stats && typeof d.stats === 'object' ? d.stats : null;
      r.seed = isInt(d.seed) ? d.seed : null; r.nonce = typeof d.nonce === 'string' ? d.nonce : null;
      // a shared-style team's notes board, private during the round and revealed in `end`: {team: ["a01 t12: ...", ...]}
      r.notes = d.notes && typeof d.notes === 'object' ? Object.fromEntries(Object.entries(d.notes)
        .filter(([, v]) => Array.isArray(v)).map(([k, v]) => [String(k), v.filter((x) => typeof x === 'string').slice(-20)])) : null;
      if (d.frame && typeof d.frame === 'object') this.setFrame(r, d.frame, r.end);
      r.local = !!d.local;
      if (r.t0 === null) r.t0 = r.end;
      this.emit();
    }

    elapsed(now) {
      const r = this.r;
      if (!r || r.t0 === null) return null;
      return ((r.over ? r.end : now) - r.t0) / 1000;
    }
  }

  const ArcadeLive = { PASS_KEY, EVENTS, SHAPES, label, rival, shape, modelName, statusLine, Client };
  if (typeof window !== 'undefined') window.ArcadeLive = ArcadeLive;
  if (typeof module !== 'undefined') module.exports = ArcadeLive;
})();
