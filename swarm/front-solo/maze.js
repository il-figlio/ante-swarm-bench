// The AX-1's maze cartridge: you race simulated Antes through a fogged maze on the handheld's screen.
// The maze and every move the Antes make come from maze-core.js (the eval's own rules); what a round is and how the
// Antes' log replays live in mazeplay.js. This file is input, the clock and drawing.
// With the live switch on, the rivals are real Ante processes on the server: mazelive.js holds that round (the
// /api/play client) and the "live" section below draws it. With it off (the default) nothing here touches the API.
(() => {
  const Core = window.MazeCore, P = window.MazePlay, L = window.MazeLive;
  const $ = (id) => document.getElementById(id);
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const INK = '#141413', DIM = 'rgba(20,20,19,0.55)', TRAIL = 'rgba(20,20,19,0.34)', GHOST = 'rgba(20,20,19,0.07)';
  const PRINT = 'rgba(20,20,19,0.14)', KEY = '#e8501e', GLASS = 'rgba(196,197,191,0.92)', FONT = '"Spec Mono", ui-monospace, monospace';
  const KEYS = { ArrowUp: 'N', ArrowRight: 'E', ArrowDown: 'S', ArrowLeft: 'W', w: 'N', d: 'E', s: 'S', a: 'W', W: 'N', D: 'E', S: 'S', A: 'W' };
  const PAD = { up: 'N', right: 'E', down: 'S', left: 'W' };
  const STEP = { N: [0, -1, 1], E: [1, 0, 2], S: [0, 1, 4], W: [-1, 0, 8] };
  const BEAT = 700;          // the countdown: 3, 2, 1, then go
  const REPEAT = 140;        // a held D-pad arm moves again every 140 ms

  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k) || 'null'); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private window: no bests */ } },
  };
  const newSeed = () => (Math.random() * 1e9) >>> 0;
  const saved = store.get('maze-race:settings') || {};
  const cfg = { opp: P.opponent(saved.opp).id, pace: P.PACES.includes(saved.pace) ? saved.pace : 6,
    size: P.SIZES[saved.size] ? saved.size : 'm', seed: newSeed() };
  const played = new Set();   // mazes (size:seed) raced this visit: a replay is from memory, so it sets no best
  let g = null, raf = 0, last = 0;

  // ---------- a round ----------
  function newRound() {
    const prm = P.params(cfg.size, cfg.seed), maze = Core.generate(prm), plan = P.plan(Core, prm, cfg.opp);
    const seen = new Uint8Array(maze.cols * maze.rows);
    seen[maze.start] = 1;
    // Settings are fixed for the round: the clock and the best-time key read these, never cfg.
    g = { prm, maze, plan, rep: new P.Replay(maze, plan), phase: 'ready', countAt: 0, t0: 0, end: 0, acc: 0, tick: 0,
      you: { pos: maze.start, seen, trail: [maze.start], moves: 0, bumps: 0, time: null }, bump: -1e9,
      shortest: Core.shortestRoute(maze).length - 1, size: cfg.size, opp: plan.opp, pace: cfg.pace,
      key: P.bestKey(cfg.size, cfg.opp, cfg.pace), replay: played.has(`${cfg.size}:${cfg.seed}`), winner: null, newBest: false };
    g.best = store.get(g.key);
    g.tapeAt = -1;
    cancelAnimationFrame(raf); last = 0;
    renderTape(); renderReceipt(); draw(performance.now());
  }

  function start() {
    if (!g || g.phase !== 'ready') return;
    g.phase = 'count'; g.countAt = performance.now();
    played.add(`${g.size}:${g.prm.seed}`);
    raf = requestAnimationFrame(loop);
  }

  function move(dir) {
    if (live) { liveMove(dir); return; }
    if (!g) return;
    if (g.phase === 'ready') { start(); return; }   // the first press starts the countdown; no jump starts
    if (g.phase !== 'live') return;
    const { maze, you } = g, d = STEP[dir];
    if (maze.walls[you.pos] & d[2]) { you.bumps++; g.bump = performance.now(); return; }
    you.pos += d[1] * maze.cols + d[0]; you.moves++;
    you.seen[you.pos] = 1; you.trail.push(you.pos);
    if (you.pos === maze.exit) finish('you', performance.now());
  }

  function finish(who, now) {
    if (g.phase !== 'live') return;
    g.phase = 'over'; g.winner = who; g.end = now;
    if (who === 'you') {
      g.you.time = (now - g.t0) / 1000;
      if (!g.replay && (g.best === null || g.you.time < g.best)) { store.set(g.key, g.you.time); g.newBest = true; }
    }
    renderReceipt();
  }

  const pressA = () => { if (live) { liveStart(); return; } if (!g) return; if (g.phase === 'over') newRound(); if (g.phase === 'ready') start(); };   // after a round: again, straight to the countdown
  const pressB = () => { if (live) { liveB(); return; } cfg.seed = newSeed(); newRound(); };

  // ---------- the clock: each simulated Ante makes `pace` calls a second ----------
  function loop(now) {
    cancelAnimationFrame(raf);
    if (live) { liveLoop(now); return; }
    if (!g) return;
    const dt = last ? Math.min(500, now - last) : 0;
    last = now;
    if (g.phase === 'count' && now - g.countAt >= 3 * BEAT) { g.phase = 'live'; g.t0 = now; g.acc = 0; }
    else if (g.phase === 'live') {
      g.acc += dt * g.pace / 1000;
      while (g.acc >= 1 && g.phase === 'live') {
        g.acc -= 1; g.tick++;
        g.rep.advance(g.tick);
        // Their race ends at the tick after the call that found the exit. If they all stop without it (a limit),
        // the race is yours, as on the first race page, but no best is set.
        if (g.tick >= g.plan.endTick) {
          if (g.plan.found) finish('opp', now);
          else { g.phase = 'over'; g.winner = 'you'; g.stopped = true; g.end = now; renderReceipt(); }
        }
      }
    }
    draw(now);
    if (g.phase === 'count' || g.phase === 'live') raf = requestAnimationFrame(loop);
    else last = 0;
  }

  // ---------- drawing ----------
  function fit(canvas) {
    const r = canvas.getBoundingClientRect();
    const dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  function geom(w, h, maze) {
    const s = Math.max(2, Math.floor(Math.min(w / maze.cols, h / maze.rows)));
    return { s, ox: Math.floor((w - s * maze.cols) / 2), oy: Math.floor((h - s * maze.rows) / 2), cols: maze.cols };
  }
  const cx = (gm, i) => gm.ox + (i % gm.cols) * gm.s;
  const cy = (gm, i) => gm.oy + ((i / gm.cols) | 0) * gm.s;

  // LCD print: a cell in ink with the faint drop shadow of a segment display (the same as the replay boards).
  function pixel(ctx, x, y, s, color, inset) {
    const p = Math.max(1, s * inset);
    ctx.fillStyle = PRINT;
    ctx.fillRect(x + p + s * 0.04, y + p + s * 0.08, s - 2 * p, s - 2 * p);
    ctx.fillStyle = color;
    ctx.fillRect(x + p, y + p, s - 2 * p, s - 2 * p);
  }

  // Fog is unlit segments; a cell you know is clear glass with its walls printed on it.
  function drawMaze(ctx, gm, maze, known, reveal) {
    // Fog: small unlit dots, quieter than the walls, and no bigger than a few pixels however big the cells get.
    const { s } = gm, n = maze.cols * maze.rows, dot = Math.max(1, Math.min(s * 0.4, 7)), p = (s - dot) / 2;
    ctx.fillStyle = GHOST;
    for (let i = 0; i < n; i++) if (!(reveal || known[i])) ctx.fillRect(cx(gm, i) + p, cy(gm, i) + p, dot, dot);
    const lw = Math.max(1, Math.round(s * 0.13)), off = lw % 2 ? 0.5 : 0;
    const walls = (dx, dy) => {
      ctx.beginPath();
      for (let i = 0; i < n; i++) {
        if (!(reveal || known[i])) continue;
        const x = cx(gm, i) + off + dx, y = cy(gm, i) + off + dy, wl = maze.walls[i];
        if (wl & 1) { ctx.moveTo(x, y); ctx.lineTo(x + s, y); }
        if (wl & 2) { ctx.moveTo(x + s, y); ctx.lineTo(x + s, y + s); }
        if (wl & 4) { ctx.moveTo(x, y + s); ctx.lineTo(x + s, y + s); }
        if (wl & 8) { ctx.moveTo(x, y); ctx.lineTo(x, y + s); }
      }
      ctx.stroke();
    };
    ctx.lineWidth = lw; ctx.lineCap = 'square';
    ctx.strokeStyle = PRINT; walls(Math.max(0.25, s * 0.03), Math.max(1, s * 0.07));
    ctx.strokeStyle = INK; walls(0, 0);
    if (reveal || known[maze.exit]) {   // the exit: a hollow square with a lit centre, like the replay's nest
      const x = cx(gm, maze.exit), y = cy(gm, maze.exit);
      ctx.strokeStyle = INK; ctx.lineWidth = Math.max(1, s * 0.12);
      ctx.strokeRect(x + s * 0.22, y + s * 0.22, s * 0.56, s * 0.56);
      ctx.fillStyle = INK; ctx.fillRect(x + s * 0.4, y + s * 0.4, s * 0.2, s * 0.2);
    }
  }

  function line(ctx, gm, cells, color, width) {
    if (!cells || cells.length < 2) return;
    ctx.strokeStyle = color; ctx.lineWidth = width; ctx.lineJoin = 'round'; ctx.lineCap = 'round';
    ctx.beginPath();
    cells.forEach((i, k) => { const x = cx(gm, i) + gm.s / 2, y = cy(gm, i) + gm.s / 2; if (k) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
    ctx.stroke();
  }

  function banner(ctx, w, h, text, big) {
    const fs = Math.max(11, Math.round(big ? Math.min(h * 0.32, w * 0.2) : Math.min(h * 0.075, w * 0.045)));
    ctx.font = `500 ${fs}px ${FONT}`;
    const tw = ctx.measureText(text).width + fs * (big ? 1 : 1.4), th = fs * (big ? 1.3 : 1.8);
    const y = big ? (h - th) / 2 : fs * 0.5;
    ctx.fillStyle = big ? GLASS : INK;
    ctx.fillRect((w - tw) / 2, y, tw, th);
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    if (big) { ctx.fillStyle = PRINT; ctx.fillText(text, w / 2 + fs * 0.03, y + th / 2 + fs * 0.06); }
    ctx.fillStyle = big ? INK : '#c4c5bf';
    ctx.fillText(text, w / 2, y + th / 2);
  }

  // The route shown after a round: the council's own synthesized route, else the maze's shortest one.
  const shownRoute = () => (g.plan.opp.mode === 'council' && g.plan.out.route ? g.plan.out.route : Core.shortestRoute(g.maze));

  function drawYou(now) {
    const { ctx, w, h } = fit($('board')), m = g.maze, gm = geom(w, h, m), over = g.phase === 'over', s = gm.s;
    drawMaze(ctx, gm, m, g.you.seen, over);
    const trail = new Set(g.you.trail);
    for (const i of trail) if (i !== g.you.pos) pixel(ctx, cx(gm, i), cy(gm, i), s, TRAIL, 0.34);
    if (over && g.winner === 'opp') line(ctx, gm, shownRoute(), DIM, Math.max(1, s * 0.14));
    const bumping = !reduced.matches && now - g.bump < 160;
    pixel(ctx, cx(gm, g.you.pos), cy(gm, g.you.pos), s, KEY, bumping ? 0.26 : 0.1);
    if (g.phase === 'ready') banner(ctx, w, h, 'press A to start');
    else if (g.phase === 'count') {
      const left = 3 - Math.floor((now - g.countAt) / BEAT);
      banner(ctx, w, h, left > 0 ? String(left) : 'go', true);
    } else if (over) banner(ctx, w, h, g.winner === 'you' ? (g.newBest ? 'you win · best' : 'you win') : `${g.opp.team} wins`);
  }

  function drawOpp() {
    const { ctx, w, h } = fit($('opp-board')), m = g.maze, gm = geom(w, h, m), over = g.phase === 'over', rep = g.rep, s = gm.s;
    drawMaze(ctx, gm, m, rep.seen, over);
    if (rep.route) line(ctx, gm, rep.route, DIM, Math.max(1, s * 0.3));
    else if (over && g.winner === 'opp') {
      const win = rep.agents[g.plan.out.exit_agent];
      if (win) line(ctx, gm, win.trail, DIM, Math.max(1, s * 0.3));
    }
    if (g.phase !== 'ready') {
      for (const a of rep.agents) {
        if (a.state === 'wait') continue;
        pixel(ctx, cx(gm, a.pos), cy(gm, a.pos), s, a.state === 'run' || a.state === 'found' ? INK : DIM, 0.1);
      }
    }
    if (over) pixel(ctx, cx(gm, g.you.pos), cy(gm, g.you.pos), s, KEY, 0.1);
  }

  const put = (id, text) => { const el = $(id); if (el.textContent !== text) el.textContent = text; };
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;
  const pct = (n, all) => Math.round((100 * n) / all);
  const youMapped = () => { let n = 0; for (const v of g.you.seen) n += v; return n; };
  const theirTime = () => (g.plan.endTick / g.pace);
  const elapsed = (now) => (g.phase === 'live' ? (now - g.t0) / 1000 : g.phase === 'over' ? (g.end - g.t0) / 1000 : null);

  function draw(now) {
    if (live) { drawLive(now); return; }
    if (!g) return;
    drawYou(now);
    drawOpp();
    const m = g.maze, cells = m.cols * m.rows, rep = g.rep, t = elapsed(now);
    put('screen-tag', `maze · ${g.size} ${m.cols}×${m.rows}`);
    put('screen-opp', `vs ${g.opp.label} · simulated`);
    put('opp-name', g.opp.label);
    put('opp-tally', `${rep.calls} calls · ${pct(rep.mapped(), cells)}% mapped`);
    let caption;
    if (g.phase === 'ready') caption = `you vs ${g.opp.label} · press A or a direction`;
    else if (g.phase === 'count') caption = 'get ready…';
    else if (g.phase === 'live') caption = `you ${g.you.moves} moves · ${g.opp.team} ${rep.calls} calls, ${pct(rep.mapped(), cells)}% mapped`;
    else if (g.stopped) caption = `${g.opp.team} stopped without the exit · ${rep.calls} calls`;
    else if (g.winner === 'you') caption = `${g.you.time.toFixed(1)}s in ${g.you.moves} moves (shortest ${g.shortest})${g.newBest ? ' · new best' : g.replay ? ' · replay, no best' : ''}`;
    else caption = `${g.opp.team} reached the exit in ${theirTime().toFixed(1)}s, ${g.plan.out.calls} calls · you had ${g.you.moves} moves`;
    put('screen-caption', caption);
    put('a-label', g.phase === 'over' ? 'again' : g.phase === 'ready' ? 'start' : 'race');
    put('r-time', t === null ? '-.-s' : `${t.toFixed(1)}s`);
    put('r-moves', String(g.you.moves).padStart(3, '0'));
    const best = g.newBest ? g.you.time : g.best;
    put('r-best', best === null || best === undefined ? '-.-s' : `${best.toFixed(1)}s`);
    $('lamp-play').dataset.on = String(g.phase === 'live' || g.phase === 'count');
    $('lamp-pass').dataset.on = String(g.phase === 'over' && g.winner === 'you');
    $('lamp-fail').dataset.on = String(g.phase === 'over' && g.winner === 'opp');
    $('screen').dataset.phase = g.phase;
    $('board').setAttribute('aria-label', `your maze, ${m.cols} by ${m.rows}: ${plural(g.you.moves, 'move')}, ${plural(g.you.bumps, 'bump')}, ${pct(youMapped(), cells)}% seen`);
    $('opp-board').setAttribute('aria-label', `${g.opp.label}, simulated: ${rep.calls} calls, ${pct(rep.mapped(), cells)}% mapped`);
    if (rep.at !== g.tapeAt) renderTape();
  }

  function renderTape() {
    g.tapeAt = g.rep.at;
    const ol = $('tape'), rows = g.rep.recent.slice(-24).reverse();
    ol.replaceChildren();
    if (!rows.length) {
      const li = document.createElement('li');
      li.dataset.state = 'done';
      li.textContent = g.phase === 'ready' ? 'no calls yet · press A' : '…';
      ol.append(li);
      return;
    }
    rows.forEach((r, i) => {
      const li = document.createElement('li');
      li.dataset.state = i === 0 ? 'now' : 'done';
      if (r.hit) li.dataset.hit = 'true';
      for (const [cls, text] of [['n', String(r.tick).padStart(3, '0')], ['k', r.who], ['a', r.call + (r.hit ? ' · exit' : '')]]) {
        const sp = document.createElement('span'); sp.className = cls; sp.textContent = text; li.append(sp);
      }
      ol.append(li);
    });
  }

  function renderReceipt() {
    const m = g.maze, over = g.phase === 'over', out = g.plan.out;
    const youRow = over && g.winner === 'you' && !g.stopped
      ? `${g.you.time.toFixed(1)}s · ${g.you.moves} moves · ${g.you.bumps} bumps`
      : `${g.you.moves} moves · ${plural(g.you.bumps, 'bump')}${over ? ' · not out' : ''}`;
    // After the round the simulation's own end is shown, even when you won first: it is already fixed by the maze.
    const themRow = !over ? 'racing…' : !g.plan.found ? `stopped · ${out.calls} calls`
      : `${theirTime().toFixed(1)}s at this pace · tick ${g.plan.endTick} · ${out.calls} calls`;
    const rows = [
      ['maze', `${g.size} · ${m.cols}×${m.rows}`], ['seed', String(g.prm.seed)], ['shortest route', `${g.shortest} moves`],
      ['opponent', `${g.opp.label} · simulated`], ['pace', `${g.pace} calls/s each`],
      ['you', youRow], [g.opp.team, themRow],
      ['best here', g.newBest ? `${g.you.time.toFixed(1)}s · new` : g.best == null ? '—' : `${g.best.toFixed(1)}s`],
    ];
    if (g.opp.mode === 'council' && over) rows.push(['council route', out.route ? `${out.route_len - 1} moves · ${out.verified ? 'checked against the walls' : 'not legal'}` : 'none']);
    if (g.opp.mode === 'delegate' && over) rows.push(['lead', `${out.lead_calls} assignments · workers waited ${out.wait_ticks} turns`]);
    if (g.opp.mode === 'parallel' && over && out.cancelled) rows.push(['cancelled', out.cancelled.length ? out.cancelled.map((k) => `r${String(k + 1).padStart(2, '0')}`).join(' ') : 'none']);
    const dl = $('receipt');
    dl.replaceChildren();
    for (const [k, v] of rows) {
      const d = document.createElement('div');
      const dt = document.createElement('dt'); dt.textContent = k;
      const dd = document.createElement('dd'); dd.textContent = v;
      d.append(dt, dd); dl.append(d);
    }
    $('legend').textContent = `${g.opp.label}: ${g.opp.about} Everyone starts in the middle; you see only the cells you have `
      + `entered, and so do they. A tick is one call from every running Ante. Results are the simulation's, replayed, `
      + `not a recording of Ante processes.`;
    put('opp-note', g.opp.about);
  }

  // ---------- live: real Antes through /api/play (mazelive.js) ----------
  // Off by default and on only by the switch. The page knows what the server sends your team: your fog, the ally,
  // each rival's calls, cells mapped and distance bucket. Rival paths are drawn only after `end`.
  const ALLY = 'rgba(20,20,19,0.2)';
  const REPLAY = 4000;        // the rivals' paths are drawn over 4 s after the end
  const STATUS_EVERY = 15000; // status refresh while live is on and no round runs
  const RETRY = 5000;         // a busy server is asked again every 5 s, until B
  const liveModel = () => (L ? L.modelName(client && client.st) : 'model');
  const LIVE_ABOUT = {
    solo: (m) => `one real Ante (${m}) on its own map, with the ./maze client: look, move, next-edge, map.`,
    swarm4: (m) => `four real Antes (${m}) playing as one team on one map: a lead assigns each worker a different unexplored edge, and the workers report dead ends and corridors on a shared board of notes.`,
    swarm16: (m) => `sixteen real Antes (${m}) as one team: a lead and fifteen workers, one map, one board of notes. The most the server runs at once.`,
  };
  const SCRIPTED_NOTE = $('masthead-note').textContent;
  const LIVE_NOTE = () => `Find the exit before the Antes do. Live: the rivals are real Ante processes on this server, on ${liveModel()}, `
    + 'in real time. You see your team\'s map and how they are doing, never where they are, until the end.';
  let lastSt = null;
  let live = false, assist = false, redraw = 0, statusTimer = 0, retryTimer = 0, quitAt = -1e9, replayFrom = 0, sideKey = '', said = null;
  const safeSession = (() => {
    try { const s = window.sessionStorage; s.getItem('x'); return s; } catch (e) {
      return { getItem: () => null, setItem: () => { throw new Error('no session storage'); }, removeItem: () => {} };
    }
  })();
  const client = L ? new L.Client({ fetch: (...a) => window.fetch(...a), EventSource: window.EventSource, storage: safeSession,
    now: () => performance.now(), onChange: () => liveChanged() }) : null;

  const dimsOf = (size) => {
    const [cols, rows] = P.SIZES[size] || P.SIZES.m;
    return { cols, rows, start: ((rows / 2) | 0) * cols + ((cols / 2) | 0), exit: cols * rows - 1 };   // as maze-core's generate
  };
  const liveRunning = () => !!(client && client.r && !client.r.over);
  const liveSay = (text) => { said = { text, at: performance.now() }; draw(performance.now()); };
  const ago = (now, at) => (at === null || at === undefined ? null : Math.max(0, (now - at) / 1000));
  const teamName = () => P.opponent(cfg.opp).team;

  function setLive(on) {
    if (!client || on === live) return;
    if (!on && liveRunning()) { liveSay('a live round is running · B twice gives up'); return; }
    live = on;
    cancelAnimationFrame(raf); raf = 0; last = 0;
    clearTimeout(retryTimer); retryTimer = 0;
    clearInterval(statusTimer); statusTimer = 0;
    if (on) {
      if (!L.LIVE[cfg.opp]) cfg.opp = 'solo';   // not saved: the scripted pick comes back with live off
      client.r = null; client.notice = null;
      showPass(!client.passcode);
      refreshStatus();
      statusTimer = setInterval(() => { if (!liveRunning()) refreshStatus(); }, STATUS_EVERY);
    } else {
      showPass(false);
      cfg.opp = P.opponent((store.get('maze-race:settings') || {}).opp || cfg.opp).id;
    }
    syncKeys();
    $('masthead-note').textContent = on ? LIVE_NOTE() : SCRIPTED_NOTE;
    if (on) liveReset(); else { $('rivals').hidden = true; $('opp-board').hidden = false; put('tape-tag', 'calls · simulated'); put('r-best-tag', 'best'); newRound(); }
  }

  function showPass(on) {
    $('pass').hidden = !on;
    if (on) put('pass-note', client.denied ? 'wrong passcode' : 'kept in this tab only, sent only to start a round');
  }

  async function refreshStatus() {
    if (!client) return;
    await client.status();
  }

  // Back to "ready": no round, the chosen size's empty maze. A finished round is dropped.
  function liveReset() {
    if (liveRunning()) return;
    clearTimeout(retryTimer); retryTimer = 0;
    client.r = null; said = null; sideKey = '';
    put('tape-tag', 'calls · live'); put('r-best-tag', 'last call');
    liveSide(true); liveTape(); liveReceipt(); draw(performance.now());
  }

  async function liveStart() {
    if (liveRunning()) return;
    if (client.r && client.r.over) client.r = null;
    clearTimeout(retryTimer); retryTimer = 0;
    if (!client.passcode) { showPass(true); $('pass-in').focus(); liveSay('enter the passcode to start a live round'); return; }
    const st = client.st;
    if (st && !st.live && (st.error === 'absent' || (st.reason && st.reason !== 'busy'))) { liveSay(L.statusLine(st, 0)); return; }
    said = null;
    const res = await client.start({ size: cfg.size, opp: cfg.opp, assist, dims: dimsOf(cfg.size) });
    if (!live) return;
    if (res.ok) { sideKey = ''; liveTape(); liveReceipt(); if (!raf) raf = requestAnimationFrame(loop); return; }
    if (res.need || res.denied) { showPass(true); $('pass-in').value = ''; $('pass-in').focus(); }
    if (res.busy) retryTimer = setTimeout(() => { retryTimer = 0; if (live && !liveRunning()) liveStart(); }, RETRY);
    refreshStatus();
    draw(performance.now());
  }

  // B in live: during a round, twice within 3 s gives up (DELETE); otherwise it stops a busy retry and resets.
  function liveB() {
    const now = performance.now();
    if (liveRunning()) {
      if (client.r.gaveUp) return;
      if (now - quitAt < 3000) { quitAt = -1e9; client.giveUp(); return; }
      quitAt = now; liveSay('press B again to give up');
      return;
    }
    liveReset();
  }

  function liveMove(dir) {
    const r = client && client.r;
    if (!r || r.over) { liveSay(client && client.passcode ? 'press A to start a live round' : 'enter the passcode, then press A'); return; }
    if (r.phase !== 'live') return;
    client.move(dir);
  }

  function liveChanged() {
    const r = client.r;
    if (r && r.over && !replayFrom) { replayFrom = performance.now(); said = null; refreshStatus(); }
    if (!r || !r.over) replayFrom = 0;
    if (!live) return;
    if (client.denied) showPass(true);
    if (client.st !== lastSt) { lastSt = client.st; syncKeys(); }   // the sizes the server offers live
    liveTape(); liveSide(false);
    if (!r || r.over || r.phase !== 'live') liveReceipt();
    if (r && (!r.over || replayFrom)) { if (!raf) raf = requestAnimationFrame(loop); }
    else if (!redraw) redraw = requestAnimationFrame(() => { redraw = 0; if (live) draw(performance.now()); });
  }

  function liveLoop(now) {
    raf = 0;
    draw(now);
    const r = client.r;
    if (r && (!r.over || now - replayFrom < REPLAY + 100)) raf = requestAnimationFrame(loop);
  }

  // The maze as your team knows it: walls of seen cells, fog elsewhere. After the end, the whole maze.
  function liveMaze() {
    const r = client.r;
    if (r && r.maze) { const n = r.maze.walls.length; return { m: r.maze, known: new Uint8Array(n).fill(1), reveal: true }; }
    const d = r || dimsOf(cfg.size), n = d.cols * d.rows, walls = new Uint8Array(n), known = new Uint8Array(n);
    if (r) for (let i = 0; i < n; i++) if (r.walls[i] >= 0) { walls[i] = r.walls[i]; known[i] = 1; }
    return { m: { cols: d.cols, rows: d.rows, walls, start: d.start, exit: d.exit }, known, reveal: false };
  }

  function rivalPaths(r, frac) {
    const out = [];
    for (const [who, cells] of Object.entries((r && r.paths) || {})) {
      if (who === 'you' || who === 'ally') continue;
      out.push([who, cells.slice(0, Math.max(1, Math.ceil(cells.length * frac)))]);
    }
    return out;
  }

  function drawLive(now) {
    const r = client.r, { m, known, reveal } = liveMaze(), over = !!(r && r.over);
    // your screen
    {
      const { ctx, w, h } = fit($('board')), gm = geom(w, h, m), s = gm.s;
      drawMaze(ctx, gm, m, known, reveal);
      const frac = !over ? 0 : reduced.matches ? 1 : Math.min(1, (now - replayFrom) / REPLAY);
      if (over) {
        for (const [, cells] of rivalPaths(r, frac)) {
          line(ctx, gm, cells, DIM, Math.max(1, s * 0.14));
          pixel(ctx, cx(gm, cells[cells.length - 1]), cy(gm, cells[cells.length - 1]), s, INK, 0.18);
        }
      }
      if (r && r.ally) {
        for (const i of new Set(r.ally.trail)) pixel(ctx, cx(gm, i), cy(gm, i), s, ALLY, 0.38);
        if (r.ally.pos !== null) {   // the ally: a hollow square in ink
          ctx.strokeStyle = INK; ctx.lineWidth = Math.max(1, s * 0.14);
          ctx.strokeRect(cx(gm, r.ally.pos) + s * 0.2, cy(gm, r.ally.pos) + s * 0.2, s * 0.6, s * 0.6);
        }
      }
      const you = r ? r.you : { pos: m.start, trail: [] };
      for (const i of new Set(you.trail)) if (i !== you.pos) pixel(ctx, cx(gm, i), cy(gm, i), s, TRAIL, 0.34);
      const bumping = r && !reduced.matches && now - r.bumpAt < 160;
      pixel(ctx, cx(gm, you.pos), cy(gm, you.pos), s, KEY, bumping ? 0.26 : 0.1);
      let b = null;
      const st = client.st, off = st && !st.live && (st.error || (st.reason && st.reason !== 'busy'));
      if (!r) b = off ? 'live off' : client.passcode ? 'press A · live' : 'passcode needed';
      else if (r.phase === 'posting') b = r.gaveUp ? 'cancelling…' : 'starting antes…';
      else if (r.phase === 'starting') b = 'antes starting…';
      else if (r.phase === 'queued') b = r.queuePos ? `queued · #${r.queuePos}` : 'queued';
      else if (r.phase === 'finishing') b = r.gaveUp ? 'giving up…' : r.won ? 'exit · checking' : 'round ending…';
      else if (over) b = r.reason === 'gave_up' ? 'gave up' : r.winner === 'you' ? 'you win' : r.winner === 'opp' ? `${teamName()} wins` : r.reason === 'timeout' ? 'time up' : 'round over';
      if (b) banner(ctx, w, h, b);
    }
    // the rivals' board: shown only once the round is over and the server has sent the maze
    const showOpp = over && !!r.maze;
    $('opp-board').hidden = !showOpp;
    if (showOpp) {
      const { ctx, w, h } = fit($('opp-board')), gm = geom(w, h, m), s = gm.s;
      drawMaze(ctx, gm, m, known, true);
      for (const [, cells] of rivalPaths(r, 1)) {
        line(ctx, gm, cells, DIM, Math.max(1, s * 0.3));
        pixel(ctx, cx(gm, cells[cells.length - 1]), cy(gm, cells[cells.length - 1]), s, INK, 0.1);
      }
      pixel(ctx, cx(gm, r.you.pos), cy(gm, r.you.pos), s, KEY, 0.1);
    }
    liveText(now);
  }

  const nearest = (r) => { let best = null; for (const v of r.rivals.values()) if (v.bucket && (best === null || L.BUCKETS.indexOf(v.bucket) > L.BUCKETS.indexOf(best))) best = v.bucket; return best; };
  const rivalCalls = (r) => { let n = 0; for (const v of r.rivals.values()) n += v.calls; return n; };

  function liveText(now) {
    const r = client.r, d = r || dimsOf(cfg.size), o = P.opponent(cfg.opp), t = client.elapsed(now);
    const count = L.antes({ opp: cfg.opp, assist });
    put('screen-tag', `maze · ${cfg.size} ${d.cols}×${d.rows}`);
    put('screen-opp', `vs ${o.label} · live${assist ? ' + ally' : ''}`);
    put('opp-name', `${o.label} · live`);
    put('live-status', client.notice || L.statusLine(client.st, count));
    let caption;
    if (said && now - said.at < 4000) caption = said.text;
    else if (!r) caption = client.passcode ? `you vs ${count} real ${count === 1 ? 'ante' : 'antes'} · press A` : 'live · enter the passcode';
    else if (r.phase === 'posting' || r.phase === 'starting') caption = 'starting the Antes on the server…';
    else if (r.phase === 'queued') caption = `queued behind another round${r.queuePos ? ` · ${plural(r.queuePos, 'round')} ahead` : ''} · B twice leaves`;
    else if (r.over) {
      const why = { exit: 'reached the exit', timeout: 'time up', tokens: 'token budget spent', budget: 'token budget spent', left: 'you left', gave_up: 'you gave up', error: 'server error' }[r.reason] || r.reason || 'over';
      caption = `${r.winner === 'you' ? 'you won' : r.winner === 'opp' ? `${o.team} won` : 'no winner'} · ${why} · ${r.you.moves} moves in ${t === null ? '-' : t.toFixed(1)}s${r.local ? ' · no end from the server' : ''}`;
    } else {
      const parts = [`you ${r.you.moves} moves`];
      parts.push(r.rtt === null ? 'rtt -' : `rtt ${Math.round(r.rtt)}ms`);
      parts.push(`${o.team} ${rivalCalls(r)} calls`);
      const nb = nearest(r);
      if (nb) parts.push(`nearest ${nb}`);
      if (r.stream === 'reconnecting') parts.unshift('stream reconnecting…');
      else if (r.stream === 'lost') parts.unshift('lost the stream · B twice gives up');
      if (r.moveError) parts.unshift(r.moveError);
      caption = parts.join(' · ');
    }
    put('screen-caption', caption);
    const running = liveRunning();
    put('a-label', running ? 'race' : r && r.over ? 'again' : 'start');
    put('r-time', t === null ? '-.-s' : `${t.toFixed(1)}s`);
    put('r-moves', String(r ? r.you.moves : 0).padStart(3, '0'));
    const age = r && !r.over ? ago(now, r.lastCall) : null;
    put('r-best', age === null ? '-.-s' : `${age.toFixed(1)}s`);
    put('opp-tally', r ? `${rivalCalls(r)} calls${age === null ? '' : ` · last ${age.toFixed(1)}s ago`}` : '');
    $('lamp-play').dataset.on = String(running);
    $('lamp-pass').dataset.on = String(!!(r && r.over && r.winner === 'you'));
    $('lamp-fail').dataset.on = String(!!(r && r.over && r.winner === 'opp'));
    $('screen').dataset.phase = running && r.phase === 'live' ? 'live' : r && r.over ? 'over' : 'ready';
    $('board').setAttribute('aria-label', `your maze, ${d.cols} by ${d.rows}, live: ${r ? plural(r.you.moves, 'move') : 'no round'}`);
    liveAges(now);
  }

  // The rival rows: name, distance bucket, cells mapped, calls, last call age. Rebuilt when the set of rivals
  // changes, otherwise updated in place.
  function liveSide(force) {
    const r = client.r, ol = $('rivals');
    ol.hidden = false;
    const rows = r ? [...r.rivals.values()].sort((a, b) => (a.who < b.who ? -1 : 1)) : [];
    const key = rows.map((v) => v.who).join(',') + (r ? `|${r.expect}` : '|');
    if (force || key !== sideKey) {
      sideKey = key;
      ol.replaceChildren();
      if (!rows.length) {
        const li = document.createElement('li');
        li.className = 'rivals-empty';
        li.textContent = r ? `waiting for ${plural(r.expect, 'Ante')}…` : 'rival Antes appear here: calls, cells mapped, how near the exit. Never where they are, until the end.';
        ol.append(li);
        return;
      }
      for (const v of rows) {
        const li = document.createElement('li');
        li.dataset.who = v.who;
        const name = document.createElement('span'); name.className = 'rv-name'; name.textContent = v.who;
        const role = document.createElement('span'); role.className = 'rv-role';
        const meter = document.createElement('span'); meter.className = 'rv-bucket'; meter.setAttribute('role', 'img');
        for (let k = 0; k < 3; k++) meter.append(document.createElement('i'));
        const bar = document.createElement('span'); bar.className = 'rv-bar';
        const fill = document.createElement('i'); bar.append(fill);
        const calls = document.createElement('span'); calls.className = 'rv-calls';
        const age = document.createElement('span'); age.className = 'rv-age';
        li.append(name, role, meter, bar, calls, age);
        ol.append(li);
      }
    }
    if (!r) return;
    const cells = r.cols * r.rows;
    for (const li of ol.children) {
      const v = r.rivals.get(li.dataset.who);
      if (!v) continue;
      li.dataset.bucket = v.bucket || 'unknown';
      li.dataset.state = v.state;
      const [, role, meter, bar, calls] = li.children;
      const roleText = v.role === 'lead' ? 'lead' : v.role === 'worker' ? 'wkr' : '';
      if (role.textContent !== roleText) role.textContent = roleText;
      role.title = v.role === 'lead' ? 'the lead: assigns the workers' : v.role === 'worker' ? 'a worker: follows the lead, reports by notes' : '';
      li.dataset.role = v.role || 'rival';
      meter.setAttribute('aria-label', `${v.who}: ${v.bucket || 'distance unknown'}`);
      bar.firstChild.style.width = `${Math.min(100, (100 * v.mapped) / cells).toFixed(1)}%`;   // CSSOM: allowed under the page's style-src 'self'
      bar.title = `${v.mapped} cells mapped${v.notes ? ` · ${plural(v.notes, 'note')}` : ''}`;
      const text = `${v.calls}c`;
      if (calls.textContent !== text) calls.textContent = text;
    }
  }

  function liveAges(now) {
    const r = client.r;
    if (!r) return;
    for (const li of $('rivals').children) {
      const v = r.rivals.get(li.dataset.who);
      if (!v) continue;
      const a = r.over ? null : ago(now, v.at), text = v.state === 'found' ? 'exit' : a === null ? '' : `${a.toFixed(0)}s`;
      if (li.lastChild.textContent !== text) li.lastChild.textContent = text;
    }
  }

  // The call feed on the tape: the ally's calls with their answers ("next-edge → E"), the rivals' as the op alone.
  function liveTape() {
    const r = client.r, ol = $('tape'), rows = r ? r.feed.slice(-24).reverse() : [];
    ol.replaceChildren();
    if (!rows.length) {
      const li = document.createElement('li');
      li.dataset.state = 'done';
      li.textContent = !r ? 'no calls yet · live rounds show real Ante calls here' : r.over ? 'no calls' : 'waiting for the first call…';
      ol.append(li);
      return;
    }
    rows.forEach((c, i) => {
      const li = document.createElement('li');
      li.dataset.state = i === 0 ? 'now' : 'done';
      if (c.team === 'player') li.dataset.hit = 'true';
      const t = r.t0 === null ? 0 : Math.max(0, (c.at - r.t0) / 1000);
      // Your ally's note is yours to read; a rival's is only the count on their board until the end.
      const call = c.team === 'player'
        ? (c.text ? `note · ${c.text}` : `${c.op}${c.arg ? ` ${c.arg}` : ''}${c.result ? ` → ${c.result}` : ''}`)
        : c.notes !== null && c.notes !== undefined ? `note · ${plural(c.notes, 'note')} on their board` : c.op;
      for (const [cls, text] of [['n', `${Math.round(t)}s`.padStart(4, ' ')], ['k', c.who], ['a', call]]) {
        const sp = document.createElement('span'); sp.className = cls; sp.textContent = text; li.append(sp);
      }
      ol.append(li);
    });
  }

  function liveReceipt() {
    const r = client.r, o = P.opponent(cfg.opp), d = r || dimsOf(cfg.size);
    const rows = [['maze', `${cfg.size} · ${d.cols}×${d.rows}`], ['opponent', `${o.label} · live · ${liveModel()}`], ['assist', assist ? 'an Ante on your team' : 'off']];
    if (r && r.id) rows.push(['round', r.id], ['seed hash', r.seedHash ? String(r.seedHash).slice(0, 16) : '—']);
    if (r && r.over) {
      const t = client.elapsed(performance.now());
      rows.push(['winner', r.winner === 'you' ? 'you' : r.winner === 'opp' ? o.team : 'none'], ['ended', r.reason || '—'],
        ['you', `${r.you.moves} moves · ${plural(r.you.bumps, 'bump')} · ${t === null ? '-' : t.toFixed(1)}s`],
        [o.team, `${rivalCalls(r)} calls · ${plural(r.rivals.size, 'Ante')}`]);
      for (const [k, v] of Object.entries(r.stats || {})) if (['string', 'number', 'boolean'].includes(typeof v)) rows.push([k.replace(/_/g, ' '), String(v)]);
      if (r.local) rows.push(['note', 'the server sent no end; closed here']);
      if (r.shape && r.shape !== 'single') rows.push(['shape', r.shape === 'lead' ? 'a lead and workers, one board' : 'peers on one map, one board']);
      // The swarm's chatter, revealed with the maze: every note on their board, then your team's.
      const notes = r.notes || { rival: [], player: [] };
      if (r.shape && r.shape !== 'single') rows.push([`${o.team} notes`, notes.rival.length ? plural(notes.rival.length, 'note') : 'none']);
      for (const n of notes.rival) rows.push([`· ${n.who}`, n.text]);
      if (notes.player.length) {
        rows.push(['ally notes', plural(notes.player.length, 'note')]);
        for (const n of notes.player) rows.push([`· ${n.who}`, n.text]);
      }
    }
    const dl = $('receipt');
    dl.replaceChildren();
    for (const [k, v] of rows) {
      const div = document.createElement('div');
      const dt = document.createElement('dt'); dt.textContent = k;
      const dd = document.createElement('dd'); dd.textContent = v;
      div.append(dt, dd); dl.append(div);
    }
    $('legend').textContent = `${o.label}, live: real Ante processes on this server, on ${liveModel()}, in real time. Your fog is your `
      + `team's: your cells${assist ? ' and the ally\'s' : ''}. The rivals' positions stay on the server until the round ends; `
      + 'then the maze, their paths and the notes they left each other are shown. The last-call clock is how long ago any Ante\'s call landed.';
    put('opp-note', LIVE_ABOUT[cfg.opp] ? LIVE_ABOUT[cfg.opp](liveModel()) : o.about);
  }

  if (client) {
    $('live').addEventListener('click', (e) => { setLive(!live); if (e.detail) e.currentTarget.blur(); });
    $('assist').addEventListener('click', (e) => {
      if (e.detail) e.currentTarget.blur();
      if (!live) { put('opp-note', 'assist is live only: an Ante on your team, on the server.'); return; }
      if (liveRunning()) { liveSay('a live round is running · B twice gives up'); return; }
      assist = !assist; syncKeys(); liveReset();
    });
    $('pass').addEventListener('submit', (e) => {
      e.preventDefault();
      const input = $('pass-in');
      if (!client.setPasscode(input.value)) { put('pass-note', input.value.trim() ? 'this tab cannot keep it (storage blocked)' : 'type the passcode first'); return; }
      input.value = ''; input.blur();
      client.notice = null;
      showPass(false);
      liveSay('passcode kept for this tab · press A to start');
    });
    window.addEventListener('pagehide', () => client.leave());
  } else {
    $('live').setAttribute('aria-disabled', 'true');
  }

  // ---------- settings ----------
  function syncKeys() {
    for (const [id, val] of [['opp', cfg.opp], ['pace', String(cfg.pace)], ['size', cfg.size]]) {
      for (const b of $(id).querySelectorAll('button')) {
        b.setAttribute('aria-checked', String(b.dataset.v === val));
        // In live, the desktop shapes and the pace are the simulation's only: greyed, and they say why.
        const off = live && (id === 'pace' || (id === 'opp' && !L.offered(client && client.st, b.dataset.v)));
        if (off) b.setAttribute('aria-disabled', 'true'); else b.removeAttribute('aria-disabled');
      }
    }
    if (L) {
      $('live').setAttribute('aria-checked', String(live));
      $('assist').setAttribute('aria-checked', String(live && assist));
      $('assist').setAttribute('aria-disabled', String(!live));
    }
    put('opp-aside', live ? 'live · real antes' : 'simulated');
    put('opts-sub', live ? 'desktop modes · scripted only' : 'desktop modes');
    put('pace-aside', live ? 'real time in live' : 'calls / s, each');
  }
  for (const [id, parse] of [['opp', String], ['pace', Number], ['size', String]]) {
    $(id).addEventListener('click', (e) => {
      const b = e.target.closest('button');
      if (!b) return;
      if (live && liveRunning()) { liveSay('a live round is running · B twice gives up'); return; }
      if (b.getAttribute('aria-disabled') === 'true') {
        put('opp-note', id === 'pace' ? 'live rounds run in real time: each Ante moves when its call lands.' : 'scripted only: live rounds run ante ×1 and the swarm. Turn live off to race this shape.');
        return;
      }
      cfg[id] = parse(b.dataset.v);
      store.set('maze-race:settings', { opp: cfg.opp, pace: cfg.pace, size: cfg.size });
      syncKeys();
      if (live) { liveReset(); if (e.detail) b.blur(); return; }
      newRound();   // pace too: a mid-race change would file the time under the wrong pace
      if (e.detail) b.blur();   // a pointer press hands the keys back to the maze; a keyboard press keeps focus
    });
  }

  // ---------- input ----------
  function flashPad(dir) {
    const pad = document.querySelector('.dpad');
    pad.dataset.press = dir;
    setTimeout(() => { if (pad.dataset.press === dir) delete pad.dataset.press; }, 120);
  }
  for (const b of document.querySelectorAll('.dpad-zone')) {
    let timer = null;
    const stop = () => { clearInterval(timer); timer = null; };
    const go = () => { flashPad(b.dataset.dir); move(PAD[b.dataset.dir]); };
    b.addEventListener('pointerdown', (e) => {
      e.preventDefault();
      go(); stop();
      timer = setInterval(go, REPEAT);
    });
    for (const ev of ['pointerup', 'pointerleave', 'pointercancel']) b.addEventListener(ev, stop);
    b.addEventListener('click', (e) => { if (e.detail === 0) go(); });   // keyboard activation; pointers moved on pointerdown
    b.addEventListener('contextmenu', (e) => e.preventDefault());
  }
  $('btn-a').addEventListener('click', pressA);
  $('btn-b').addEventListener('click', pressB);
  $('btn-a').addEventListener('pointerdown', (e) => e.preventDefault());   // keep focus off the keys so space stays A
  $('btn-b').addEventListener('pointerdown', (e) => e.preventDefault());

  // A swipe on the screen: one move, in the swipe's main direction.
  let touch = null;
  const screen = $('board');
  screen.addEventListener('touchstart', (e) => { const t = e.touches[0]; touch = [t.clientX, t.clientY]; }, { passive: true });
  screen.addEventListener('touchend', (e) => {
    if (!touch) return;
    const t = e.changedTouches[0], dx = t.clientX - touch[0], dy = t.clientY - touch[1];
    touch = null;
    if (Math.max(Math.abs(dx), Math.abs(dy)) < 24) return;
    move(Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'E' : 'W') : (dy > 0 ? 'S' : 'N'));
  });

  document.addEventListener('keydown', (e) => {
    if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest('input, textarea')) return;
    const dir = KEYS[e.key];
    if (dir) { e.preventDefault(); if (!e.repeat || live || g.phase === 'live') { flashPad({ N: 'up', E: 'right', S: 'down', W: 'left' }[dir]); move(dir); } return; }
    const onControl = e.target.closest('button, a');
    if ((e.key === ' ' || e.key === 'Enter') && !onControl) { e.preventDefault(); pressA(); }
    else if (e.key === 'r' || e.key === 'R') { if (!live) newRound(); }
    else if (e.key === 'n' || e.key === 'N') pressB();
  });

  new ResizeObserver(() => draw(performance.now())).observe($('screen'));
  if (document.fonts) document.fonts.ready.then(() => draw(performance.now()));

  syncKeys();
  newRound();
})();
