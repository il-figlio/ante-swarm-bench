// The AX-1's trail cartridge: you steer a trail on one grid against Antes, eating tokens to grow.
// Scripted (default): the rules and the opponents' policy come from trail-core.js (the twin of src/swarm/trail.py), run
// here one tick per beat at the chosen pace. Live (switch + passcode): the rivals are real Ante processes on the
// server; arcadelive.js holds the round and this file draws the frames the server sends.
(() => {
  const T = window.TrailCore, AL = window.ArcadeLive, A = window.Arcade;
  const { $, INK, DIM, TRAIL, GHOST, KEY, put, plural } = A;
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const BEAT = 700;
  // Opponents: the sizes the server offers (GET /api/play/trail/status: swarm style "shared"), the same keys and
  // labels in live and scripted mode. A swarm is ONE team: its tokens add up and you are compared with the total.
  const NUM = ['', 'one', 'two', 'three', 'four', 'five'];
  function opp(n) {
    const names = n === 1 ? ['ante'] : Array.from({ length: n }, (_, k) => `a${String(k + 1).padStart(2, '0')}`);
    return { n, label: AL ? AL.label(n) : (n === 1 ? 'ante ×1' : `swarm ×${n}`), names, team: n === 1 ? 'ante' : 'swarm',
      teams: ['you', ...names.map((w) => (n === 1 ? w : 'swarm'))],
      about: n === 1 ? 'one Ante on the grid with you: it steers for the nearest token, keeps room to move and stays out of reach of your head.'
        : `${NUM[n] || n} Antes on the grid as ONE team: their tokens add up and you are compared with the team's total. `
          + 'They split the tokens, keep off each other\'s paths and try to cut you off. The round ends when you are out, when the whole team is out, or at the clock.' };
  }
  const shape = () => (client ? client.shape : AL ? AL.shape('trail', null) : { style: 'shared', sizes: [1, 3] });
  const PACES = [4, 6, 9];
  const saved = A.store.get('trail:settings') || {};
  const cfg = { opp: Number.isInteger(saved.opp) ? saved.opp : 1, pace: PACES.includes(saved.pace) ? saved.pace : 6,
    size: T.SIZES[saved.size] ? saved.size : 'm', seed: (Math.random() * 1e9) >>> 0 };
  const played = new Set();
  let g = null, raf = 0, last = 0;

  // ---------- a scripted round ----------
  function newRound() {
    if (!shape().sizes.includes(cfg.opp)) cfg.opp = 1;
    const o = opp(cfg.opp), names = ['you', ...o.names];
    const p = T.params(cfg.size, cfg.seed, names.length);
    g = { p, o, model: new T.Model(p, names, o.teams), phase: 'ready', countAt: 0, acc: 0, end: 0, events: [], size: cfg.size,
      pace: cfg.pace, key: `trail:best:${cfg.size}:${cfg.opp}:${cfg.pace}`, replay: played.has(`${cfg.size}:${cfg.opp}:${cfg.seed}`),
      newBest: false };
    g.best = A.store.get(g.key);
    cancelAnimationFrame(raf); raf = 0; last = 0;
    renderSide(); draw(performance.now());
  }

  function start() {
    if (!g || g.phase !== 'ready') return;
    g.phase = 'count'; g.countAt = performance.now();
    played.add(`${g.size}:${cfg.opp}:${g.p.seed}`);
    if (!raf) raf = requestAnimationFrame(loop);
  }

  function steer(d) {
    if (live) { liveSteer(d); return; }
    if (!g) return;
    if (g.phase === 'over') return;
    g.model.steer('you', d);
    if (g.phase === 'ready') start();
  }

  function tickOnce() {
    const m = g.model, turns = T.teamTurns(m, ['you']);
    for (const pl of m.players) if (pl.who !== 'you' && pl.alive) m.turn(pl.who, turns[pl.who]);
    const ev = m.step();
    for (const [who, c] of ev.eaten) g.events.push([m.tick, who, `token · ${m.by[who].tokens}`, who === 'you']);
    for (const who of ev.out) g.events.push([m.tick, who, `out · ${m.by[who].cause}`, who === 'you']);
    if (g.events.length > 60) g.events.splice(0, g.events.length - 60);
    if (ev.eaten.length || ev.out.length) renderSide();
    if (m.over) finish();
  }

  function finish() {
    g.phase = 'over'; g.end = performance.now();
    const you = g.model.by.you.tokens;
    if (!g.replay && (g.best === null || you > g.best)) { A.store.set(g.key, you); g.newBest = you > 0; g.best = you; }
    renderSide();
  }

  function loop(now) {
    raf = 0;
    if (live) { draw(now); if (liveAnimating()) raf = requestAnimationFrame(loop); return; }
    const dt = last ? Math.min(250, now - last) : 0;
    last = now;
    if (g.phase === 'count' && now - g.countAt >= 3 * BEAT) { g.phase = 'live'; g.acc = 0; }
    else if (g.phase === 'live') {
      g.acc += (dt * g.pace) / 1000;
      while (g.acc >= 1 && g.phase === 'live') { g.acc -= 1; tickOnce(); }
    }
    draw(now);
    if (g.phase === 'count' || g.phase === 'live') raf = requestAnimationFrame(loop);
    else last = 0;
  }

  const pressA = () => {
    if (live) { liveStart(); return; }
    if (g.phase === 'over') newRound();
    if (g.phase === 'ready') start();
  };
  const pressB = () => {
    if (live) { liveB(); return; }
    if (g.phase === 'live' || g.phase === 'count') { put('screen-caption', 'a round is on · it ends at the clock or when a side is out'); return; }
    cfg.seed = (Math.random() * 1e9) >>> 0; newRound();
  };

  // ---------- drawing (scripted and live share it: a view is {cols, rows, tokens, players, tick, clock}) ----------
  function geom(w, h, v) {
    const s = Math.max(2, Math.floor(Math.min(w / v.cols, h / v.rows)));
    return { s, ox: Math.floor((w - s * v.cols) / 2), oy: Math.floor((h - s * v.rows) / 2), cols: v.cols };
  }

  function drawGrid(v, banner, big, flash) {
    const { ctx, w, h } = A.fit($('board')), gm = geom(w, h, v), s = gm.s;
    const X = (i) => gm.ox + (i % gm.cols) * s, Y = (i) => gm.oy + Math.floor(i / gm.cols) * s;
    // the grid's edge: a hairline frame, then unlit dots on every cell
    ctx.strokeStyle = INK; ctx.lineWidth = Math.max(1, Math.round(s * 0.1));
    ctx.strokeRect(gm.ox - ctx.lineWidth / 2, gm.oy - ctx.lineWidth / 2, s * v.cols + ctx.lineWidth, s * v.rows + ctx.lineWidth);
    const dot = Math.max(1, Math.min(s * 0.22, 4)), p = (s - dot) / 2;
    ctx.fillStyle = GHOST;
    for (let i = 0; i < v.cols * v.rows; i++) ctx.fillRect(X(i) + p, Y(i) + p, dot, dot);
    for (const t of v.tokens) {   // a token: a hollow square with a lit centre
      ctx.strokeStyle = INK; ctx.lineWidth = Math.max(1, s * 0.12);
      ctx.strokeRect(X(t) + s * 0.24, Y(t) + s * 0.24, s * 0.52, s * 0.52);
      ctx.fillStyle = INK; ctx.fillRect(X(t) + s * 0.42, Y(t) + s * 0.42, s * 0.16, s * 0.16);
    }
    for (const pl of v.players) {
      if (!pl.alive || !pl.body.length) continue;
      const mine = pl.who === 'you';
      pl.body.forEach((c, k) => { if (k) A.pixel(ctx, X(c), Y(c), s, mine ? 'rgba(232,80,30,0.55)' : TRAIL, 0.14); });
      A.pixel(ctx, X(pl.body[0]), Y(pl.body[0]), s, mine ? KEY : INK, flash && mine ? 0.24 : 0.06);
      if (!mine && s >= 9) {   // a rival's head: a notch toward where it is going
        const [dx, dy] = T.DELTA[pl.heading];
        ctx.fillStyle = '#c4c5bf';
        ctx.fillRect(X(pl.body[0]) + s * (0.42 + dx * 0.22), Y(pl.body[0]) + s * (0.42 + dy * 0.22), s * 0.16, s * 0.16);
      }
    }
    if (banner) A.banner(ctx, w, h, banner, big);
  }

  const timeLeft = (v, tickS) => Math.max(0, (v.clock - v.tick) * tickS);
  // A view's teams: [{team, members, tokens, alive}], from the frame or worked out from the players.
  function teamsOf(v) {
    if (Array.isArray(v.teams) && v.teams.length) return v.teams;
    const out = [];
    for (const pl of v.players) {
      const t = pl.team || pl.who;
      let row = out.find((x) => x.team === t);
      if (!row) { row = { team: t, members: [], tokens: 0, alive: 0 }; out.push(row); }
      row.members.push(pl.who); row.tokens += pl.tokens; row.alive += pl.alive ? 1 : 0;
    }
    return out;
  }
  const teamScore = (v, team) => { const t = teamsOf(v).find((x) => x.team === team); return t ? t.tokens : 0; };
  const swarmOf = (v) => teamsOf(v).find((t) => t.members.length > 1) || null;

  function draw(now) {
    if (live) { drawLive(now); return; }
    const m = g.model, you = m.by.you, ph = g.phase;
    let b = null, big = false;
    if (ph === 'ready') b = 'press A to start';
    else if (ph === 'count') { const left = 3 - Math.floor((now - g.countAt) / BEAT); b = left > 0 ? String(left) : 'go'; big = true; }
    else if (ph === 'over') b = m.winner === 'you' ? (g.newBest ? 'you win · best' : 'you win') : m.winner ? `${m.winner} wins` : 'draw';
    else if (!you.alive) b = 'you are out';
    const v = { cols: m.cols, rows: m.rows, tokens: m.tokens, players: m.players, tick: m.tick, clock: m.clock };
    drawGrid(v, b, big);
    const sw = swarmOf(v);
    put('screen-tag', `trail · ${g.size} ${m.cols}×${m.rows}`);
    put('screen-opp', `vs ${g.o.label} · simulated`);
    let caption;
    const alive = m.players.filter((pl) => pl.alive).length;
    if (ph === 'ready') caption = `you vs ${g.o.label} · press A or a direction`;
    else if (ph === 'count') caption = 'get ready…';
    else if (ph === 'live') caption = sw ? `you ${you.tokens} · swarm ${sw.tokens} (${sw.alive}/${sw.members.length} running)` : you.alive ? `you ${you.tokens} tokens · length ${you.body.length} · ${alive} trails running` : `you are out with ${you.tokens} · ${alive} trails still running`;
    else caption = `${m.reason === 'clock' ? 'time up' : !you.alive ? 'you are out' : 'the others are out'} · you ${you.tokens} tokens · ${m.winner === 'you' ? 'you win' : m.winner ? `${m.winner} wins with ${teamScore(v, m.winner)}` : 'a draw'}`;
    put('screen-caption', caption);
    put('a-label', ph === 'over' ? 'again' : ph === 'ready' ? 'start' : 'race');
    put('b-label', 'new');
    put('r-time', `${timeLeft(m, 1 / g.pace).toFixed(1)}s`);
    put('r-score', String(you.tokens).padStart(2, '0'));
    put('r-best', g.best === null || g.best === undefined ? '--' : String(g.best).padStart(2, '0'));
    lamps(ph === 'live' || ph === 'count', ph === 'over' && m.winner === 'you', ph === 'over' && m.winner !== 'you');
    $('screen').dataset.phase = ph;
    $('board').setAttribute('aria-label', `trail grid ${m.cols} by ${m.rows}, tick ${m.tick} of ${m.clock}: you ${you.alive ? 'running' : 'out'}, ${plural(you.tokens, 'token')}`);
  }

  function lamps(play, win, lost) {
    $('lamp-play').dataset.on = String(play); $('lamp-pass').dataset.on = String(win); $('lamp-fail').dataset.on = String(lost);
  }

  // Standings: one row per side (you, the ante, or the swarm with its total), each swarm member indented under it.
  function standRows(v, winner) {
    const players = v.players, rows = [];
    const sides = teamsOf(v).slice().sort((a, b) => b.tokens - a.tokens);
    for (const t of sides) {
      const ms = players.filter((pl) => (pl.team || pl.who) === t.team);
      if (ms.length === 1) {
        const pl = ms[0];
        rows.push([pl.who, String(pl.tokens), pl.alive ? `len ${pl.len ?? pl.body.length}` : `out · ${pl.cause || ''}`.trim(),
          t.team === winner ? 'win' : pl.alive ? (pl.who === 'you' ? 'you' : 'run') : 'out']);
        continue;
      }
      rows.push([t.team, String(t.tokens), `team · ${t.alive}/${ms.length} running`, t.team === winner ? 'win' : t.alive ? 'run' : 'out']);
      for (const pl of ms.slice().sort((a, b) => b.tokens - a.tokens || a.seat - b.seat)) {
        rows.push([`·${pl.who}`, String(pl.tokens), pl.alive ? `len ${pl.len ?? pl.body.length}` : `out · ${pl.cause || ''}`.trim(), pl.alive ? 'run' : 'out']);
      }
    }
    return rows;
  }

  function renderSide() {
    const m = g.model;
    A.standings(standRows({ players: m.players }, g.phase === 'over' ? m.winner : null));
    A.tape(g.events.slice(-24).reverse().map(([t, who, what, hit]) => [String(t).padStart(3, '0'), who, what, hit]), g.phase === 'ready' ? 'no events yet · press A' : 'no tokens eaten yet');
    const you = m.by.you;
    A.receipt([
      ['grid', `${g.size} · ${m.cols}×${m.rows}`], ['seed', String(g.p.seed)], ['opponent', `${g.o.label} · simulated`],
      ['pace', `${g.pace} ticks/s`], ['clock', `${m.clock} ticks · ${(m.clock / g.pace).toFixed(0)}s`],
      ['you', `${plural(you.tokens, 'token')} · ${you.alive ? 'running' : `out at tick ${you.died} (${you.cause})`}`],
      ...(g.o.n > 1 ? [['swarm', `${plural(m.teamTokens('swarm'), 'token')} together · ${m.members('swarm').filter((pl) => pl.alive).length} of ${g.o.n} running`]] : []),
      ['winner', g.phase !== 'over' ? '…' : m.winner || 'draw'],
      ['best here', g.best == null ? '—' : String(g.best)],
    ], `${g.o.label}: ${g.o.about} Each tick every trail moves one cell; a token makes it one longer. Out on a wall, a trail `
      + '(your own too) or a head-on meeting. Simulated in your browser by the same rules and policy as the server (a scripted swarm '
      + 'splits the tokens and keeps off its own members\' next cells), no model called.');
    put('opp-note', g.o.about);
  }

  // ---------- live: real Antes through /api/play/trail (arcadelive.js) ----------
  let live = false, said = null, liveKey = '';
  const client = AL ? new AL.Client({ game: 'trail', fetch: (...a) => window.fetch(...a), EventSource: window.EventSource,
    storage: A.session, now: () => performance.now(), onChange: () => liveChanged() }) : null;
  const SCRIPTED_NOTE = $('masthead-note').textContent;
  const liveRunning = () => !!(client && client.r && !client.r.over);
  const liveAnimating = () => !!(client && client.r && (!client.r.over));
  const say = (text) => { said = { text, at: performance.now() }; draw(performance.now()); };
  const sw = client ? A.liveSwitch({ client, onSwitch: () => setLive(!live), onUnlocked: () => say('passcode kept for this tab · press A to start') }) : null;

  function setLive(on) {
    if (on === live) return;
    if (!on && liveRunning()) { say('a live round is running · B twice gives up'); return; }
    live = on;
    cancelAnimationFrame(raf); raf = 0; last = 0;
    sw.setOn(on);
    syncKeys();
    $('masthead-note').textContent = on ? 'Live: the rivals are real Ante processes on this server, steering with ./trail look and ./trail plan. Every trail moves a cell per tick; an Ante\'s plan runs until its next call lands. A swarm plays as one team: it shares its plans and a notes board, and its tokens add up.' : SCRIPTED_NOTE;
    put('tape-tag', on ? 'calls · live' : 'events · simulated');
    put('r-best-tag', on ? 'last call' : 'best');
    if (on) { client.r = null; client.notice = null; said = null; liveSide(); draw(performance.now()); } else newRound();
  }

  async function liveStart() {
    if (liveRunning()) return;
    if (client.r && client.r.over) client.r = null;
    if (!client.passcode) { sw.showPass(true); $('pass-in').focus(); say('enter the passcode to start a live round'); return; }
    said = null;
    const res = await client.start({ count: cfg.opp });
    if (res.need || res.denied) { sw.showPass(true); $('pass-in').value = ''; }
    client.status();
    if (!raf) raf = requestAnimationFrame(loop);
  }

  let quitAt = -1e9;
  function liveB() {
    const now = performance.now();
    if (liveRunning()) {
      if (now - quitAt < 3000) { quitAt = -1e9; client.giveUp(); return; }
      quitAt = now; say('press B again to give up');
      return;
    }
    client.r = null; said = null; liveSide(); draw(now);
  }

  function liveSteer(d) {
    const r = client.r;
    if (!r || r.over) { say(client.passcode ? 'press A to start a live round' : 'enter the passcode, then press A'); return; }
    if (r.phase === 'live') client.act({ dir: d });
  }

  function liveChanged() {
    buildOpps();
    if (!live) return;
    if (client.denied) sw.showPass(true);
    const r = client.r, key = r ? `${r.version}:${r.feed.length}:${r.phase}` : '';
    if (key !== liveKey) { liveKey = key; liveSide(); }
    if (!raf) raf = requestAnimationFrame(loop);
  }

  function liveView() {
    const r = client.r, f = r && r.frame;
    if (f) return { cols: f.cols, rows: f.rows, tokens: f.tokens || [], players: f.players || [], teams: f.teams, tick: f.tick, clock: f.clock };
    const [cols, rows] = T.SIZES[cfg.opp > 1 ? 'l' : 'm'];
    return { cols, rows, tokens: [], players: [], tick: 0, clock: 300 };
  }

  function drawLive(now) {
    const r = client.r, v = liveView(), you = v.players.find((pl) => pl.who === 'you');
    let b = null, big = false;
    const st = client.st, off = st && !st.live && (st.error || (st.reason && st.reason !== 'busy'));
    if (!r) b = off ? 'live off' : client.passcode ? 'press A · live' : 'passcode needed';
    else if (r.phase === 'posting' || r.phase === 'starting') b = r.gaveUp ? 'cancelling…' : 'antes starting…';
    else if (r.phase === 'count') { const left = Math.ceil((r.goAt - now) / 1000); b = left > 0 ? String(left) : 'go'; big = true; }
    else if (r.over) b = r.reason === 'gave_up' ? 'gave up' : r.winner === 'you' ? 'you win' : r.winner ? `${r.winner} wins` : r.leader ? 'round over' : 'draw';
    else if (you && !you.alive) b = 'you are out';
    drawGrid(v, b, big);
    const o = opp(cfg.opp), count = o.names.length, sw = swarmOf(v);
    put('screen-tag', `trail · ${v.cols}×${v.rows}`);
    put('screen-opp', `vs ${o.label} · live`);
    put('live-status', client.notice || AL.statusLine(client.st, count));
    let caption;
    if (said && now - said.at < 4000) caption = said.text;
    else if (!r) caption = client.passcode ? `you vs ${count} real ${count === 1 ? 'ante' : 'antes'} · press A` : 'live · enter the passcode';
    else if (r.phase === 'posting' || r.phase === 'starting') caption = 'starting the Antes on the server · the clock waits for their first look';
    else if (r.phase === 'count') caption = 'the Antes are looking · get ready';
    else if (r.over) {
      const why = { clock: 'time up', out: 'the others are out', timeout: 'time up', tokens: 'token budget spent', left: 'you left', gave_up: 'you gave up', error: 'server error' }[r.reason] || r.reason || 'over';
      caption = `${why} · ${r.winner ? `${r.winner} wins` : 'no winner'}${you ? ` · you ${you.tokens}` : ''}${sw ? ` · swarm ${sw.tokens}` : ''}${r.local ? ' · no end from the server' : ''}`;
    } else {
      const parts = [you ? (you.alive ? `you ${you.tokens} tokens · length ${you.len}` : `you are out with ${you.tokens}`) : 'you'];
      if (sw) parts.push(`swarm ${sw.tokens} (${sw.alive}/${sw.members.length})`);
      parts.push(r.rtt === null ? 'rtt -' : `rtt ${Math.round(r.rtt)}ms`);
      if (r.stream === 'reconnecting') parts.unshift('stream reconnecting…');
      else if (r.stream === 'lost') parts.unshift('lost the stream · B twice gives up');
      if (r.actError) parts.unshift(r.actError);
      caption = parts.join(' · ');
    }
    put('screen-caption', caption);
    put('a-label', liveRunning() ? 'race' : r && r.over ? 'again' : 'start');
    put('b-label', liveRunning() ? 'quit' : 'reset');
    put('r-time', r && r.tickS ? `${timeLeft(v, r.tickS).toFixed(1)}s` : '-.-s');
    put('r-score', String(you ? you.tokens : 0).padStart(2, '0'));
    const age = r && !r.over && r.lastCall !== null ? (now - r.lastCall) / 1000 : null;
    put('r-best', age === null ? '--' : `${age.toFixed(1)}s`);
    lamps(liveRunning(), !!(r && r.over && r.winner === 'you'), !!(r && r.over && r.winner && r.winner !== 'you'));
    $('screen').dataset.phase = r && r.phase === 'live' ? 'live' : r && r.over ? 'over' : 'ready';
  }

  function liveSide() {
    const r = client.r, v = liveView(), o = opp(cfg.opp);
    A.standings(r && v.players.length ? standRows(v, r.over ? r.winner : null) : [['you', '0', 'waiting', 'you'], ...(o.n > 1 ? [['swarm', '0', `team of ${o.n}`, 'run']] : []), ...o.names.map((n) => [o.n > 1 ? `·${n}` : n, '0', 'waiting', 'run'])]);
    const feed = r ? r.feed.slice(-24).reverse() : [];
    A.tape(feed.map((c) => [`${r.t0 === null ? 0 : Math.max(0, Math.round((c.at - r.t0) / 1000))}s`, c.who, c.arg ? `${c.op} ${c.arg}` : c.op, false]),
      r ? 'waiting for the first call…' : 'no calls yet · live rounds show the Antes\' ./trail calls here');
    const rows = [['opponent', `${o.label} · live · ${AL.modelName(client.st)}`]];
    if (r && r.id) rows.push(['round', r.id], ['seed hash', r.seedHash ? r.seedHash.slice(0, 16) : '—']);
    if (r && r.tickS) rows.push(['tick', `${r.tickS}s`]);
    if (r && r.over) {
      rows.push(['winner', r.winner || 'none'], ['ended', r.reason || '—']);
      if (r.seed !== null) rows.push(['seed', String(r.seed)]);
      if (r.stats && r.stats.teams) rows.push(['tokens', Object.entries(r.stats.teams).map(([k, n]) => `${k} ${n}`).join(' · ')]);
      if (r.stats && r.stats.calls) rows.push(['calls', Object.entries(r.stats.calls).map(([k, n]) => `${k} ${n}`).join(' · ')]);
      if (r.stats && r.stats.ops && o.n > 1) {
        const teamOps = ['team', 'note'].map((op) => [op, Object.values(r.stats.ops).reduce((a, x) => a + (x[op] || 0), 0)]);
        rows.push(['team calls', teamOps.map(([op, n]) => `${op} ${n}`).join(' · ')]);
      }
      if (r.stats && typeof r.stats.seconds === 'number') rows.push(['seconds', r.stats.seconds.toFixed(1)]);
      for (const [team, lines] of Object.entries(r.notes || {})) lines.forEach((line, i) => rows.push([i ? '' : `${team} notes`, line]));
    }
    A.receipt(rows, `${o.label}, live: real Ante processes on this server, on ${AL.modelName(client.st)}. Each calls ./trail look `
      + '(the board as text) and ./trail plan; the server moves every trail one cell per tick and an Ante\'s plan runs until its next '
      + `call lands.${o.n > 1 ? ' A swarm is one team: each member sees its teammates\' heads and queued plans, can call ./trail team and pin notes only the team reads, and the team\'s tokens add up.' : ''} `
      + 'The seed is committed by its hash at the start and shown at the end.');
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
    A.syncOpts('size', cfg.size, () => live);
    put('opp-aside', live ? 'live · real antes' : 'simulated');
    put('pace-aside', live ? 'server tick in live' : 'ticks / s');
  }
  for (const [id, parse] of [['opp', Number], ['pace', Number], ['size', String]]) {
    $(id).addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      if (e.detail) b.blur();
      if (live && liveRunning()) { say('a live round is running · B twice gives up'); return; }
      if (b.getAttribute('aria-disabled') === 'true') { put('opp-note', 'live rounds run on the server\'s grid and tick.'); return; }
      if (!live && g && (g.phase === 'live' || g.phase === 'count')) { put('screen-caption', 'finish this round first'); return; }
      cfg[id] = parse(b.dataset.v);
      A.store.set('trail:settings', { opp: cfg.opp, pace: cfg.pace, size: cfg.size });
      syncKeys();
      if (live) { client.r = null; liveSide(); draw(performance.now()); } else newRound();
    });
  }

  A.input({
    dir: (d) => steer(d),
    a: pressA, b: pressB,
    key: (e) => { if ((e.key === 'r' || e.key === 'R') && !live && g.phase !== 'live' && g.phase !== 'count') { newRound(); return true; } return false; },
  });
  if (!client) $('live').setAttribute('aria-disabled', 'true');
  new ResizeObserver(() => draw(performance.now())).observe($('screen'));
  if (document.fonts) document.fonts.ready.then(() => draw(performance.now()));
  reduced.addEventListener?.('change', () => draw(performance.now()));
  buildOpps();
  syncKeys();
  newRound();
  if (client) client.status();   // the sizes the server offers, for the opponent keys (scripted mode too)
})();
