// The AX-1's hill cartridge: your well beside the Antes' wells, the same pieces in the same order for everyone.
// Scripted (default): hill-core.js (the twin of src/swarm/hill.py) holds every well; the opponents drop one piece each
// per beat at the chosen pace. Live (switch + passcode): the wells live on the server and real Ante processes drop
// into theirs; arcadelive.js holds the round, this file draws its frames and sends your drops.
(() => {
  const H = window.HillCore, AL = window.ArcadeLive, A = window.Arcade;
  const { $, INK, DIM, GHOST, KEY, PRINT, put, plural } = A;
  const BEAT = 700;
  // Opponents: the sizes the server offers (GET /api/play/hill/status: swarm style "pooled"), the same keys and
  // labels in live and scripted mode. A swarm plays its own wells, each with its own pieces, and pools its rows.
  const NUM = ['', 'one', 'two', 'three', 'four', 'five'];
  function opp(n) {
    const names = n === 1 ? ['ante'] : Array.from({ length: n }, (_, k) => `a${String(k + 1).padStart(2, '0')}`);
    return { n, label: AL ? AL.label(n) : (n === 1 ? 'ante ×1' : `swarm ×${n}`), names,
      teams: ['you', ...names.map((w) => (n === 1 ? w : 'swarm'))],
      about: n === 1 ? 'one Ante in the well beside yours, fed the same pieces: it tries every column and turn and keeps its stack low and free of holes.'
        : `${NUM[n] || n} Antes, each alone in its own well with its own pieces. Their rows pool: at the clock your rows are compared with `
          + 'the swarm\'s rows per well. Every garbage row the swarm sends lands in your well; yours go round its wells in turn. '
          + 'The swarm is out only when all its wells are, and you win at once if it is.' };
  }
  const shape = () => (client ? client.shape : AL ? AL.shape('hill', null) : { style: 'pooled', sizes: [1, 4] });
  const PACES = [0.5, 1, 2];
  const saved = A.store.get('hill:settings') || {};
  const cfg = { opp: Number.isInteger(saved.opp) ? saved.opp : 1, pace: PACES.includes(saved.pace) ? saved.pace : 1,
    seed: (Math.random() * 1e9) >>> 0 };
  const played = new Set();
  let g = null, raf = 0, last = 0;

  // ---------- the hover: your piece's column and turn before it drops ----------
  const hover = { col: 0, rot: 0, k: -1, piece: 0 };
  function resetHover(piece, k) { const [col] = H.spawn(piece); hover.col = col; hover.rot = 0; hover.k = k; hover.piece = piece; }
  function clampHover() {
    const w = H.size(H.SHAPES[hover.piece][hover.rot])[0];
    hover.col = Math.max(0, Math.min(H.WIDTH - w, hover.col));
  }

  // ---------- a scripted round ----------
  function newRound() {
    if (!shape().sizes.includes(cfg.opp)) cfg.opp = 1;
    const o = opp(cfg.opp);
    g = { o, model: new H.Model(cfg.seed, ['you', ...o.names], o.teams), seed: cfg.seed, phase: 'ready', countAt: 0, t0: 0, end: 0,
      acc: 0, spawnAt: 0, events: [], pace: cfg.pace, key: `hill:best:${cfg.opp}:${cfg.pace}`, replay: played.has(`${cfg.opp}:${cfg.seed}`), newBest: false };
    g.best = A.store.get(g.key);
    resetHover(g.model.piece('you'), 0);
    cancelAnimationFrame(raf); raf = 0; last = 0;
    renderSide(); draw(performance.now());
  }

  function start() {
    if (!g || g.phase !== 'ready') return;
    g.phase = 'count'; g.countAt = performance.now();
    played.add(`${cfg.opp}:${g.seed}`);
    if (!raf) raf = requestAnimationFrame(loop);
  }

  function note(who, r) {
    if (r.cleared) g.events.push([who, `${r.piece} · ${plural(r.cleared, 'row')}${r.sent ? ` · ${r.sent} → ${r.to}` : ''}`, who === 'you' || r.to === 'you']);
    if (r.out) g.events.push([who, 'out · topped', who === 'you']);
    if (g.events.length > 60) g.events.splice(0, g.events.length - 60);
  }

  function dropYou(now) {
    const m = g.model;
    if (g.phase !== 'live' || m.by.you.out) return;
    const r = m.drop('you', hover.col, hover.rot);
    if (r.error) return;
    note('you', r);
    resetHover(m.piece('you'), m.by.you.k); g.spawnAt = now;
    after(now);
  }

  function beat(now) {
    const m = g.model;
    for (const w of m.wells) {
      if (w.who === 'you' || w.out || m.over) continue;
      const [col, rot] = H.choose(w.cells, m.piece(w.who));
      note(w.who, m.drop(w.who, col, rot));
    }
    after(now);
  }

  function after(now) {
    const m = g.model;
    if (!m.over && (now - g.t0) / 1000 >= H.CLOCK_S) m.finish('clock');
    if (m.over && g.phase === 'live') {
      g.phase = 'over'; g.end = now;
      const rows = m.by.you.rows;
      if (!g.replay && (g.best === null || rows > g.best)) { A.store.set(g.key, rows); g.newBest = rows > 0; g.best = rows; }
    }
    renderSide();
  }

  function loop(now) {
    raf = 0;
    if (live) { draw(now); if (client.r && !client.r.over) raf = requestAnimationFrame(loop); return; }
    const dt = last ? Math.min(500, now - last) : 0;
    last = now;
    if (g.phase === 'count' && now - g.countAt >= 3 * BEAT) { g.phase = 'live'; g.t0 = now; g.spawnAt = now; g.acc = 0; }
    else if (g.phase === 'live') {
      g.acc += (dt * g.pace) / 1000;
      while (g.acc >= 1 && g.phase === 'live') { g.acc -= 1; beat(now); }
      if (g.phase === 'live' && !g.model.by.you.out && now - g.spawnAt >= H.FUSE_S * 1000) dropYou(now);   // the fuse
      if (g.phase === 'live' && (now - g.t0) / 1000 >= H.CLOCK_S) after(now);   // the clock
    }
    draw(now);
    if (g.phase === 'count' || g.phase === 'live') raf = requestAnimationFrame(loop);
    else last = 0;
  }

  // ---------- your controls ----------
  function control(what) {
    if (live) { liveControl(what); return; }
    if (g.phase === 'ready') { start(); return; }
    if (g.phase !== 'live' || g.model.by.you.out) return;
    if (what === 'W') hover.col--;
    else if (what === 'E') hover.col++;
    else if (what === 'turn') { hover.rot = (hover.rot + 1) % 4; }
    else if (what === 'drop') { dropYou(performance.now()); return; }
    clampHover();
    draw(performance.now());
  }

  const playing = () => (live ? !!(client.r && client.r.phase === 'live' && !client.r.over) : g.phase === 'live');
  const pressA = () => {
    if (playing()) { control('drop'); return; }
    if (live) { liveStart(); return; }
    if (g.phase === 'over') newRound();
    if (g.phase === 'ready') start();
  };
  const pressB = () => {
    if (playing()) { control('turn'); return; }
    if (live) { liveReset(); return; }
    if (g.phase === 'count') return;
    cfg.seed = (Math.random() * 1e9) >>> 0; newRound();
  };

  // ---------- drawing ----------
  // A view: {wells: [{who, cells, piece (name), next, rows, out, pending, height}], you: well, hover?, fuse (0..1)}
  function drawWell(ctx, x0, y0, s, w, opts) {
    const lw = Math.max(1, Math.round(s * 0.12));
    ctx.strokeStyle = INK; ctx.lineWidth = lw;
    ctx.beginPath();
    ctx.moveTo(x0 - lw / 2, y0); ctx.lineTo(x0 - lw / 2, y0 + H.HEIGHT * s + lw / 2);
    ctx.lineTo(x0 + H.WIDTH * s + lw / 2, y0 + H.HEIGHT * s + lw / 2); ctx.lineTo(x0 + H.WIDTH * s + lw / 2, y0);
    ctx.stroke();
    const dot = Math.max(1, Math.min(s * 0.2, 3)), p = (s - dot) / 2;
    ctx.fillStyle = GHOST;
    for (let i = 0; i < H.WIDTH * H.HEIGHT; i++) {
      const x = x0 + (i % H.WIDTH) * s, y = y0 + Math.floor(i / H.WIDTH) * s, v = w.cells[i];
      if (!v) { ctx.fillRect(x + p, y + p, dot, dot); continue; }
      A.pixel(ctx, x, y, s, v === 2 ? DIM : INK, 0.08);
      ctx.fillStyle = GHOST;
    }
    if (opts.hover && !w.out) {   // the hovering piece at the top, and where it would land, as outlines
      const shape = H.SHAPES[opts.hover.piece][opts.hover.rot], top = H.land(w.cells, shape, opts.hover.col);
      if (top !== null) {
        ctx.strokeStyle = DIM; ctx.lineWidth = Math.max(1, s * 0.1);
        for (const [dx, dy] of shape) if (top + dy >= 0) ctx.strokeRect(x0 + (opts.hover.col + dx) * s + s * 0.18, y0 + (top + dy) * s + s * 0.18, s * 0.64, s * 0.64);
      }
      for (const [dx, dy] of shape) A.pixel(ctx, x0 + (opts.hover.col + dx) * s, y0 + dy * s, s, KEY, 0.08);
    }
    if (opts.fuse !== undefined && !w.out) {   // the fuse: a bar across the top of the well, burning down
      ctx.fillStyle = KEY;
      ctx.fillRect(x0, y0 - Math.max(2, s * 0.3), H.WIDTH * s * Math.max(0, 1 - opts.fuse), Math.max(2, s * 0.18));
    }
    if (opts.label) {
      const fs = opts.fs;
      ctx.font = `500 ${fs}px ${A.FONT}`; ctx.textAlign = 'left'; ctx.textBaseline = 'bottom'; ctx.fillStyle = INK;
      ctx.fillText(opts.label, x0, y0 - (opts.fuse !== undefined ? s * 0.4 : 2));
    }
    if (w.out) {
      ctx.fillStyle = 'rgba(196,197,191,0.72)'; ctx.fillRect(x0, y0, H.WIDTH * s, H.HEIGHT * s);
      const fs = Math.max(opts.fs || 10, Math.round(s * 1.6));
      ctx.font = `500 ${fs}px ${A.FONT}`; ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillStyle = INK;
      ctx.fillText('out', x0 + (H.WIDTH * s) / 2, y0 + (H.HEIGHT * s) / 2);
    }
  }

  function drawPiece(ctx, x0, y0, s, name) {
    const i = H.NAMES.indexOf(name);
    if (i < 0) return;
    for (const [dx, dy] of H.SHAPES[i][0]) A.pixel(ctx, x0 + dx * s, y0 + dy * s, s, INK, 0.08);
  }

  function drawBoard(v, banner, big) {
    const { ctx, w, h } = A.fit($('board'));
    const head = Math.max(12, h * 0.05);
    const s = Math.max(3, Math.floor(Math.min((h - head) / H.HEIGHT, w / 19)));
    const x0 = Math.floor(s * 0.6), y0 = Math.floor(h - H.HEIGHT * s - s * 0.3);
    const fs = Math.max(10, Math.round(h * 0.034));
    drawWell(ctx, x0, y0, s, v.you, { hover: v.hover, fuse: v.fuse, label: 'you', fs });
    // the panel: next piece, then the rival wells
    const px = x0 + H.WIDTH * s + Math.ceil(s * 1.2), pw = w - px - Math.ceil(s * 0.4);
    const ns = Math.max(3, Math.floor(s * 0.6));
    ctx.font = `400 ${fs}px ${A.FONT}`; ctx.textAlign = 'left'; ctx.textBaseline = 'top'; ctx.fillStyle = DIM;
    ctx.fillText('next', px, y0);
    drawPiece(ctx, px, y0 + fs * 1.4, ns, v.you.next);
    const rivals = v.wells.filter((x) => x.who !== 'you');
    let top = y0 + fs * 1.4 + ns * 3 + s;
    const sw = poolOf(v);
    if (sw) {   // the pool: the swarm's rows together and per well, against yours
      ctx.font = `500 ${fs}px ${A.FONT}`; ctx.fillStyle = INK; ctx.textBaseline = 'top';
      ctx.fillText(`swarm ${sw.rows} rows`, px, top);
      ctx.font = `400 ${fs}px ${A.FONT}`; ctx.fillStyle = DIM;
      ctx.fillText(`${fmtAvg(sw.per_well)} / well · you ${v.you.rows}`, px, top + fs * 1.25);
      top += fs * 2.9;
    }
    // side by side, small: every rival well in one row (a lone Ante's well gets the whole panel)
    const cols = Math.max(1, rivals.length), lf = Math.max(9, Math.round(fs * (cols > 2 ? 0.72 : 0.85)));
    const m = Math.max(2, Math.floor(Math.min(pw / (cols * H.WIDTH + (cols - 1) * 1.2), (y0 + H.HEIGHT * s - top - lf * 1.6) / (H.HEIGHT + 0.6))));
    rivals.forEach((rw, k) => {
      const cx = px + k * (H.WIDTH + 1.2) * m, cy = top + lf * 1.4;
      drawWell(ctx, cx, cy, m, rw, { label: cols > 2 ? `${rw.who.slice(-2)}·${rw.rows}` : `${rw.who} · ${rw.rows}`, fs: lf });
    });
    if (banner) A.banner(ctx, w, h, banner, big);
  }

  const fmtAvg = (x) => (Number.isInteger(x) ? String(x) : x.toFixed(2).replace(/0$/, ''));
  // The pooled side of a view: {rows, per_well, standing, members} of the team with several wells, or null.
  function poolOf(v) {
    if (Array.isArray(v.teams)) return v.teams.find((t) => t.members.length > 1) || null;
    return null;
  }

  function scriptedView(now) {
    const m = g.model, pub = (w) => ({ who: w.who, team: w.team, cells: w.cells, next: H.NAMES[m.nextPiece(w.who)], rows: w.rows, out: w.out, height: w.height(), pending: w.pending.reduce((a, b) => a + b, 0), sent: w.sent });
    const wells = m.wells.map(pub);
    const fuse = g.phase === 'live' ? (now - g.spawnAt) / (H.FUSE_S * 1000) : undefined;
    return { wells, teams: m.teams.map((t) => m.teamPublic(t)), you: wells[0], hover: g.phase === 'over' ? null : hover, fuse };
  }

  function draw(now) {
    if (live) { drawLive(now); return; }
    const m = g.model, you = m.by.you, ph = g.phase;
    let b = null, big = false;
    if (ph === 'ready') b = 'press A to start';
    else if (ph === 'count') { const left = 3 - Math.floor((now - g.countAt) / BEAT); b = left > 0 ? String(left) : 'go'; big = true; }
    else if (ph === 'over') b = m.winner === 'you' ? (g.newBest ? 'you win · best' : 'you win') : m.winner ? `${m.winner} wins` : 'draw';
    drawBoard(scriptedView(now), b, big);
    const t = ph === 'live' ? (now - g.t0) / 1000 : ph === 'over' ? (g.end - g.t0) / 1000 : 0;
    put('screen-tag', `hill · ${H.WIDTH}×${H.HEIGHT}`);
    put('screen-opp', `vs ${g.o.label} · simulated`);
    let caption;
    if (ph === 'ready') caption = `you vs ${g.o.label} · press A`;
    else if (ph === 'count') caption = 'get ready…';
    else if (ph === 'live') caption = you.out ? `your well is out · ${m.wells.filter((w) => !w.out).length} still standing` : `${H.NAMES[m.piece('you')]} · col ${hover.col} turn ${hover.rot} · ${plural(you.rows, 'row')}${g.o.n > 1 ? ` · swarm ${fmtAvg(m.perWell('swarm'))}/well` : ''}${you.pending.length ? ` · ${you.pending.reduce((a, c) => a + c, 0)} garbage coming` : ''}`;
    else caption = `${m.reason === 'clock' ? 'time up' : you.out ? 'your well is out' : g.o.n > 1 ? 'the swarm is out' : 'one well left'} · you ${plural(you.rows, 'row')}${g.o.n > 1 ? ` · swarm ${m.teamRows('swarm')} (${fmtAvg(m.perWell('swarm'))}/well)` : ''} · ${m.winner === 'you' ? 'you win' : m.winner ? `${m.winner} wins` : 'a draw'}`;
    put('screen-caption', caption);
    put('a-label', ph === 'over' ? 'again' : ph === 'ready' ? 'start' : 'drop');
    put('b-label', ph === 'live' ? 'turn' : 'new');
    put('r-time', `${Math.max(0, H.CLOCK_S - t).toFixed(1)}s`);
    put('r-score', String(you.rows).padStart(2, '0'));
    put('r-best', g.best === null || g.best === undefined ? '--' : String(g.best).padStart(2, '0'));
    lamps(ph === 'live' || ph === 'count', ph === 'over' && m.winner === 'you', ph === 'over' && m.winner !== 'you');
    $('screen').dataset.phase = ph;
    $('board').setAttribute('aria-label', `your well: ${plural(you.rows, 'row')} cleared, stack ${you.height()} high${you.out ? ', out' : ''}`);
  }

  function lamps(play, win, lost) {
    $('lamp-play').dataset.on = String(play); $('lamp-pass').dataset.on = String(win); $('lamp-fail').dataset.on = String(lost);
  }

  // Standings: you, and the ante or the swarm (its pooled rows, rows per well, wells standing), members indented.
  function standRows(v, winner) {
    const wells = v.wells, rows = [], pool = poolOf(v);
    const one = (w, who) => [who, String(w.rows), w.out ? 'out' : `stack ${w.height}${w.pending ? ` · +${w.pending}` : ''}`,
      w.who === winner ? 'win' : w.out ? 'out' : w.who === 'you' ? 'you' : 'run'];
    const solo = wells.filter((w) => !pool || !pool.members.includes(w.who));
    const sides = solo.map((w) => [w.rows, () => rows.push(one(w, w.who))]);
    if (pool) {
      sides.push([pool.per_well, () => {
        rows.push(['swarm', String(pool.rows), `${fmtAvg(pool.per_well)}/well · ${pool.standing}/${pool.members.length} up`, winner === pool.team ? 'win' : pool.standing ? 'run' : 'out']);
        for (const w of wells.filter((x) => pool.members.includes(x.who)).sort((a, b) => b.rows - a.rows)) rows.push(one(w, `·${w.who}`));
      }]);
    }
    sides.sort((a, b) => b[0] - a[0]).forEach((x) => x[1]());
    return rows;
  }

  function renderSide() {
    const m = g.model, v = scriptedView(performance.now()), you = m.by.you;
    A.standings(standRows(v, g.phase === 'over' ? m.winner : null));
    A.tape(g.events.slice(-24).reverse().map(([who, what, hit], i) => [String(g.events.length - i).padStart(3, '0'), who, what, hit]),
      g.phase === 'ready' ? 'no rows yet · press A' : 'no rows cleared yet');
    A.receipt([
      ['well', `${H.WIDTH}×${H.HEIGHT}`], ['seed', String(g.seed)], ['opponent', `${g.o.label} · simulated`],
      ['pace', `${g.pace} pieces/s each`], ['clock', `${H.CLOCK_S}s · fuse ${H.FUSE_S}s`],
      ['you', `${plural(you.rows, 'row')} · sent ${you.sent} · got ${you.got}${you.out ? ' · out' : ''}`],
      ...(g.o.n > 1 ? [['swarm', `${plural(m.teamRows('swarm'), 'row')} · ${fmtAvg(m.perWell('swarm'))} per well · sent you ${m.teamPublic('swarm').sent}`]] : []),
      ['winner', g.phase !== 'over' ? '…' : m.winner || 'draw'], ['best here', g.best == null ? '—' : `${g.best} rows`],
    ], `${g.o.label}: ${g.o.about} Pieces drop straight down from above the well. Clearing two rows at once sends one garbage `
      + `row, three send three${g.o.n > 1 ? '' : ', to the other well'}. Simulated in your browser by the same rules and policy as the server, no model called.`);
    put('opp-note', g.o.about);
  }

  // ---------- live: real Antes through /api/play/hill (arcadelive.js) ----------
  let live = false, said = null, liveKey = '', sentK = -1, giveUpAt = -1e9;
  const client = AL ? new AL.Client({ game: 'hill', fetch: (...a) => window.fetch(...a), EventSource: window.EventSource,
    storage: A.session, now: () => performance.now(), onChange: () => liveChanged() }) : null;
  const SCRIPTED_NOTE = $('masthead-note').textContent;
  const liveRunning = () => !!(client && client.r && !client.r.over);
  const say = (text) => { said = { text, at: performance.now() }; draw(performance.now()); };
  const sw = client ? A.liveSwitch({ client, onSwitch: () => setLive(!live), onUnlocked: () => say('passcode kept for this tab · press A to start') }) : null;

  function setLive(on) {
    if (on === live) return;
    if (!on && liveRunning()) {   // in a round, the live switch is how you give up: twice within 3 s
      const now = performance.now();
      if (now - giveUpAt < 3000) { giveUpAt = -1e9; client.giveUp(); } else { giveUpAt = now; say('press live again to give up this round'); }
      return;
    }
    live = on;
    cancelAnimationFrame(raf); raf = 0; last = 0;
    sw.setOn(on);
    syncKeys();
    $('masthead-note').textContent = on ? 'Live: the rival wells are real Ante processes on this server, placing pieces with ./hill look and ./hill drop. A lone Ante gets your pieces; a swarm plays its own wells with their own pieces and pools its rows.' : SCRIPTED_NOTE;
    put('tape-tag', on ? 'calls · live' : 'rows · simulated');
    put('r-best-tag', on ? 'last call' : 'best');
    if (on) { client.r = null; client.notice = null; said = null; liveSide(); draw(performance.now()); } else newRound();
  }

  async function liveStart() {
    if (liveRunning()) return;
    if (client.r && client.r.over) client.r = null;
    if (!client.passcode) { sw.showPass(true); $('pass-in').focus(); say('enter the passcode to start a live round'); return; }
    said = null; sentK = -1;
    const res = await client.start({ count: cfg.opp });
    if (res.need || res.denied) { sw.showPass(true); $('pass-in').value = ''; }
    client.status();
    if (!raf) raf = requestAnimationFrame(loop);
  }

  function liveReset() { if (liveRunning()) return; client.r = null; said = null; liveSide(); draw(performance.now()); }

  const liveYou = () => { const f = client.r && client.r.frame; return f ? f.wells.find((w) => w.who === 'you') : null; };

  function liveControl(what) {
    const r = client.r, you = liveYou();
    if (!r || r.over) { say(client.passcode ? 'press A to start a live round' : 'enter the passcode, then press A'); return; }
    if (r.phase !== 'live' || !you || you.out) return;
    if (what === 'W') hover.col--;
    else if (what === 'E') hover.col++;
    else if (what === 'turn') hover.rot = (hover.rot + 1) % 4;
    else if (what === 'drop') {
      if (sentK === you.k || r.inflight) return;   // one drop per piece: wait for the server's frame
      sentK = you.k;
      client.act({ col: hover.col, rot: hover.rot });
      return;
    }
    clampHover();
    draw(performance.now());
  }

  function liveChanged() {
    buildOpps();
    if (!live) return;
    if (client.denied) sw.showPass(true);
    const you = liveYou();
    if (you && you.k !== hover.k) resetHover(H.NAMES.indexOf(you.piece), you.k);
    const r = client.r, key = r ? `${r.version}:${r.feed.length}:${r.phase}` : '';
    if (key !== liveKey) { liveKey = key; liveSide(); }
    if (!raf) raf = requestAnimationFrame(loop);
  }

  function liveFuse(now) {
    const r = client.r, f = r && r.frame, you = liveYou();
    if (!f || !you || r.phase !== 'live' || !f.spawned) return undefined;
    const el = client.elapsed(now);
    if (el === null) return undefined;
    const frac = (el - (f.spawned.you || 0)) / (f.fuse_s || H.FUSE_S);
    if (frac >= 1 && sentK !== you.k && !r.inflight) liveControl('drop');   // the fuse: your piece drops where it hovers
    return frac;
  }

  function drawLive(now) {
    const r = client.r, f = r && r.frame, o = opp(cfg.opp);
    const blank = (who) => ({ who, cells: new Array(H.WIDTH * H.HEIGHT).fill(0), next: '', rows: 0, out: false, height: 0, pending: 0 });
    const wells = f ? f.wells : ['you', ...o.names].map(blank), you = wells.find((w) => w.who === 'you') || wells[0];
    const teams = f && Array.isArray(f.teams) ? f.teams : o.n > 1 ? [{ team: 'you', members: ['you'], rows: 0, per_well: 0, standing: 1 }, { team: 'swarm', members: o.names, rows: 0, per_well: 0, standing: o.n }] : null;
    const pool = teams ? poolOf({ teams }) : null;
    const fuse = liveFuse(now);
    let b = null, big = false;
    const st = client.st, off = st && !st.live && (st.error || (st.reason && st.reason !== 'busy'));
    if (!r) b = off ? 'live off' : client.passcode ? 'press A · live' : 'passcode needed';
    else if (r.phase === 'posting' || r.phase === 'starting') b = r.gaveUp ? 'cancelling…' : 'antes starting…';
    else if (r.phase === 'count') { const left = Math.ceil((r.goAt - now) / 1000); b = left > 0 ? String(left) : 'go'; big = true; }
    else if (r.over) b = r.reason === 'gave_up' ? 'gave up' : r.winner === 'you' ? 'you win' : r.winner ? `${r.winner} wins` : 'round over';
    drawBoard({ wells, teams, you, hover: r && r.phase === 'live' && f ? hover : null, fuse }, b, big);
    const count = o.names.length;
    put('screen-tag', `hill · ${H.WIDTH}×${H.HEIGHT}`);
    put('screen-opp', `vs ${o.label} · live`);
    put('live-status', client.notice || AL.statusLine(client.st, count));
    let caption;
    if (said && now - said.at < 4000) caption = said.text;
    else if (!r) caption = client.passcode ? `you vs ${count} real ${count === 1 ? 'ante' : 'antes'} · press A` : 'live · enter the passcode';
    else if (r.phase === 'posting' || r.phase === 'starting') caption = 'starting the Antes on the server · the clock waits for their first look';
    else if (r.phase === 'count') caption = 'the Antes are looking · get ready';
    else if (r.over) {
      const why = { clock: 'time up', out: 'one well left', timeout: 'time up', tokens: 'token budget spent', left: 'you left', gave_up: 'you gave up', error: 'server error' }[r.reason] || r.reason || 'over';
      caption = `${why} · ${r.winner ? `${r.winner} wins` : 'no winner'} · you ${plural(you.rows, 'row')}${pool ? ` · swarm ${pool.rows} (${fmtAvg(pool.per_well)}/well)` : ''}${r.local ? ' · no end from the server' : ''}`;
    } else {
      const parts = [you.out ? 'your well is out' : `${you.piece} · col ${hover.col} turn ${hover.rot} · ${plural(you.rows, 'row')}`];
      if (pool) parts.push(`swarm ${fmtAvg(pool.per_well)}/well`);
      if (you.pending) parts.push(`${you.pending} garbage coming`);
      parts.push(r.rtt === null ? 'rtt -' : `rtt ${Math.round(r.rtt)}ms`);
      if (r.stream === 'reconnecting') parts.unshift('stream reconnecting…');
      else if (r.stream === 'lost') parts.unshift('lost the stream · live twice gives up');
      if (r.actError) parts.unshift(r.actError);
      caption = parts.join(' · ');
    }
    put('screen-caption', caption);
    const playingNow = !!(r && r.phase === 'live' && !r.over);
    put('a-label', playingNow ? 'drop' : r && r.over ? 'again' : 'start');
    put('b-label', playingNow ? 'turn' : 'reset');
    const el = client.elapsed(now);
    put('r-time', el === null || !r ? '-.-s' : `${Math.max(0, (r.limit || H.CLOCK_S) - el).toFixed(1)}s`);
    put('r-score', String(you.rows).padStart(2, '0'));
    const age = r && !r.over && r.lastCall !== null ? (now - r.lastCall) / 1000 : null;
    put('r-best', age === null ? '--' : `${age.toFixed(1)}s`);
    lamps(liveRunning(), !!(r && r.over && r.winner === 'you'), !!(r && r.over && r.winner && r.winner !== 'you'));
    $('screen').dataset.phase = playingNow ? 'live' : r && r.over ? 'over' : 'ready';
  }

  function liveSide() {
    const r = client.r, f = r && r.frame, o = opp(cfg.opp);
    A.standings(f ? standRows(f, r.over ? r.winner : null) : [['you', '0', 'waiting', 'you'], ...(o.n > 1 ? [['swarm', '0', `${o.n} wells`, 'run']] : []), ...o.names.map((n) => [o.n > 1 ? `·${n}` : n, '0', 'waiting', 'run'])]);
    const feed = r ? r.feed.slice(-24).reverse() : [];
    A.tape(feed.map((c) => [`${r.t0 === null ? 0 : Math.max(0, Math.round((c.at - r.t0) / 1000))}s`, c.who, (c.arg ? `${c.op} ${c.arg}` : c.op) + (c.refused ? ' · refused' : ''), false]),
      r ? 'waiting for the first call…' : 'no calls yet · live rounds show the Antes\' ./hill calls here');
    const rows = [['opponent', `${o.label} · live · ${AL.modelName(client.st)}`]];
    if (r && r.id) rows.push(['round', r.id], ['seed hash', r.seedHash ? r.seedHash.slice(0, 16) : '—']);
    if (f && f.fuse_s) rows.push(['fuse', `${f.fuse_s}s`]);
    if (r && r.over) {
      rows.push(['winner', r.winner || 'none'], ['ended', r.reason || '—']);
      if (r.seed !== null) rows.push(['seed', String(r.seed)]);
      if (r.stats && r.stats.pooled && r.stats.pooled.swarm) rows.push(['pooled', `swarm ${r.stats.pooled.swarm.rows} rows · ${fmtAvg(r.stats.pooled.swarm.per_well)} per well · you ${(r.stats.rows || {}).you ?? 0}`]);
      if (r.stats && r.stats.rows) rows.push(['rows', Object.entries(r.stats.rows).map(([k, n]) => `${k} ${n}`).join(' · ')]);
      if (r.stats && r.stats.calls) rows.push(['calls', Object.entries(r.stats.calls).map(([k, n]) => `${k} ${n}`).join(' · ')]);
    }
    A.receipt(rows, `${o.label}, live: real Ante processes on this server, on ${AL.modelName(client.st)}. Each calls ./hill look `
      + `(its well as text, the piece drawn in four turns) and ./hill drop <col> <rot>. ${o.n > 1 ? 'Each swarm well draws its own sequence from the round seed and its seat; yours is the round\'s own. ' : 'You both draw from one seeded sequence. '}`
      + 'The seed is committed by its hash at the start and shown at the end. In a round, press live twice to give up.');
    put('opp-note', o.about);
  }

  // ---------- settings ----------
  let oppKey = '';
  function buildOpps() {
    const sizes = shape().sizes, key = sizes.join(',');
    if (!sizes.includes(cfg.opp)) { cfg.opp = 1; if (!live && g && g.phase === 'ready') newRound(); }
    if (key === oppKey) return;
    oppKey = key;
    $('opp').replaceChildren(...sizes.map((n) => {
      const b = document.createElement('button');
      b.className = 'key'; b.setAttribute('role', 'radio'); b.dataset.v = String(n); b.textContent = opp(n).label;
      return b;
    }));
    syncKeys();
  }

  function syncKeys() {
    A.syncOpts('opp', String(cfg.opp));
    A.syncOpts('pace', String(cfg.pace), () => live);
    put('opp-aside', live ? 'live · real antes' : 'simulated');
    put('pace-aside', live ? 'real time in live' : 'pieces / s, each');
    put('pace-note', live ? 'Live: everyone\'s piece drops by itself after the server\'s fuse.' : `Your piece drops by itself after a ${H.FUSE_S} s fuse.`);
  }
  for (const [id, parse] of [['opp', Number], ['pace', Number]]) {
    $(id).addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      if (e.detail) b.blur();
      if (live && liveRunning()) { say('a live round is running · press live twice to give up'); return; }
      if (b.getAttribute('aria-disabled') === 'true') { put('opp-note', 'live rounds run in real time: each Ante drops when its call lands.'); return; }
      if (!live && g && (g.phase === 'live' || g.phase === 'count')) { put('screen-caption', 'finish this round first'); return; }
      cfg[id] = parse(b.dataset.v);
      A.store.set('hill:settings', { opp: cfg.opp, pace: cfg.pace });
      syncKeys();
      if (live) liveReset(); else newRound();
    });
  }

  A.input({
    dir: (d) => control(d === 'N' ? 'turn' : d === 'S' ? 'drop' : d),
    a: pressA, b: pressB,
    repeat: (d) => d === 'E' || d === 'W',
    tap: () => { if (playing()) control('turn'); },
    swipe: (dx, dy) => {
      if (!playing()) return false;
      if (Math.abs(dx) > Math.abs(dy)) {
        const cell = $('board').getBoundingClientRect().height / (H.HEIGHT + 1);
        const n = Math.max(1, Math.round(Math.abs(dx) / cell));
        for (let i = 0; i < n; i++) control(dx > 0 ? 'E' : 'W');
      } else control(dy > 0 ? 'drop' : 'turn');
      return true;
    },
    key: (e) => {
      if ((e.key === 'r' || e.key === 'R') && !live && g.phase !== 'live' && g.phase !== 'count') { newRound(); return true; }
      if ((e.key === 'x' || e.key === 'X') && playing()) { control('turn'); return true; }
      return false;
    },
  });
  if (!client) $('live').setAttribute('aria-disabled', 'true');
  new ResizeObserver(() => draw(performance.now())).observe($('screen'));
  if (document.fonts) document.fonts.ready.then(() => draw(performance.now()));
  buildOpps();
  syncKeys();
  newRound();
  if (client) client.status();   // the sizes the server offers, for the opponent keys (scripted mode too)
})();
