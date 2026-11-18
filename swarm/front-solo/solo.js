// The AX-1: picks a stored Ante run, a task and one of its agents, and plays that agent's trace on a board.
// Drawing only; what a frame holds comes from boards.js.
(() => {
  const B = window.Boards;
  const $ = (id) => document.getElementById(id);
  const reduced = matchMedia('(prefers-reduced-motion: reduce)');
  const STEP_MS = 650;           // one event per beat: scripted turns take milliseconds, too fast to see
  const SWARM_MS = 5000;         // the whole run's clock squeezed into five seconds when a run is loaded
  const INK = '#141413', DIM = 'rgba(20,20,19,0.55)', GHOST = 'rgba(20,20,19,0.05)', KEY = '#e8501e';

  const st = { runs: [], run: null, runIdx: 0, task: null, agent: 0, board: 'trail', at: 0, playing: false,
    swarmT: Infinity, last: 0, raf: 0 };

  // ---------- data ----------
  const getJSON = async (url) => { const r = await fetch(url, { cache: 'no-cache' }); if (!r.ok) throw new Error(r.status); return r.json(); };
  const agents = () => (st.run ? st.run.attempts.filter((a) => !st.task || a.task === st.task) : []);
  const agent = () => agents()[st.agent] || null;
  const events = () => (agent() ? agent().events : []);

  const params = () => new URLSearchParams(location.search);
  const writeUrl = () => {
    if (!st.run) return;
    const q = new URLSearchParams();
    if (st.task) q.set('task', st.task);
    if (agent()) q.set('a', agent().id);
    if (st.board !== 'trail') q.set('b', st.board);
    history.replaceState(null, '', `/r/${st.run.id}${q.toString() ? '?' + q : ''}`);
  };

  async function loadRun(idx, keep) {
    st.runIdx = (idx + st.runs.length) % st.runs.length;
    const rec = st.runs[st.runIdx];
    $('run-id').textContent = rec.id.slice(0, 6);
    $('run-meta').textContent = `${rec.preset} · n${rec.size}`;
    $('screen-caption').textContent = 'loading run…';
    st.run = await getJSON(`/api/runs/${rec.id}`);
    const p = keep ? params() : new URLSearchParams();
    st.task = st.run.tasks.includes(p.get('task')) ? p.get('task') : null;
    const want = p.get('a');
    st.agent = Math.max(0, agents().findIndex((a) => a.id === want));
    renderTasks();
    renderReceipt();
    st.swarmT = reduced.matches ? Infinity : 0;
    restart(!reduced.matches);
  }

  // ---------- controls ----------
  function renderTasks() {
    const box = $('tasks');
    box.replaceChildren();
    for (const t of [null, ...st.run.tasks]) {
      const b = document.createElement('button');
      b.className = 'key';
      b.setAttribute('role', 'radio');
      b.setAttribute('aria-checked', String(st.task === t));
      b.textContent = t || 'all';
      b.onclick = () => { st.task = t; st.agent = 0; renderTasks(); restart(false); };
      box.append(b);
    }
  }

  function setBoard(name) {
    st.board = name;
    document.querySelector('.dial').dataset.board = name;
    for (const s of document.querySelectorAll('.dial-stop[data-board]')) s.setAttribute('aria-checked', String(s.dataset.board === name));
    $('r-third-tag').textContent = name === 'hill' ? 'rows' : 'trail';
    writeUrl();
    draw();
  }

  function restart(play) {
    st.at = 0;
    st.playing = !!play && !reduced.matches;
    writeUrl();
    renderTape();
    tick(performance.now());
  }

  function step(d) {
    st.playing = false;
    const n = events().length;
    st.at = Math.max(0, Math.min(n, Math.round(st.at) + d));
    draw();
  }

  function pickAgent(d) {
    const n = agents().length;
    if (!n) return;
    st.agent = (st.agent + d + n) % n;
    restart(st.playing);
  }

  function press(dir) {
    const pad = document.querySelector('.dpad');
    pad.dataset.press = dir;
    setTimeout(() => { if (pad.dataset.press === dir) delete pad.dataset.press; }, 120);
    if (dir === 'left') step(-1);
    if (dir === 'right') step(1);
    if (dir === 'up') pickAgent(-1);
    if (dir === 'down') pickAgent(1);
  }

  function toggle() {
    const n = events().length;
    if (reduced.matches) { step(1); return; }   // reduced motion: A steps instead of playing
    if (st.at >= n) st.at = 0;
    st.playing = !st.playing;
    tick(performance.now());
  }

  // ---------- clock ----------
  function tick(now) {
    cancelAnimationFrame(st.raf);
    const dt = st.last ? Math.min(500, now - st.last) : 0;  // a slow or throttled frame still moves on; a slept tab does not leap
    st.last = now;
    const n = events().length;
    let more = false;
    if (st.playing) {
      st.at = Math.min(n, st.at + dt / STEP_MS);
      if (st.at >= n) st.playing = false; else more = true;
    }
    if (st.run && st.swarmT !== Infinity) {
      const span = B.grid.span(st.run.attempts) || 1;
      st.swarmT += (dt / SWARM_MS) * span;
      if (st.swarmT >= span) st.swarmT = Infinity; else more = true;
    }
    draw();
    if (more) st.raf = requestAnimationFrame(tick); else st.last = 0;
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

  // LCD print: a pixel cell in dark ink with the faint drop shadow of a segment display.
  function pixel(ctx, x, y, s, color, inset = 0.12) {
    const p = Math.max(1, s * inset);
    ctx.fillStyle = 'rgba(20,20,19,0.14)';
    ctx.fillRect(x + p + s * 0.04, y + p + s * 0.08, s - 2 * p, s - 2 * p);
    ctx.fillStyle = color;
    ctx.fillRect(x + p, y + p, s - 2 * p, s - 2 * p);
  }

  function ghosts(ctx, cols, rows, s, ox, oy) {
    for (let y = 0; y < rows; y++) for (let x = 0; x < cols; x++) {
      ctx.fillStyle = GHOST;
      const p = Math.max(1, s * 0.12);
      ctx.fillRect(ox + x * s + p, oy + y * s + p, s - 2 * p, s - 2 * p);
    }
  }

  function drawTrail(ctx, w, h, f) {
    const s = Math.floor(Math.min(w / f.w, h / f.h)), ox = Math.floor((w - s * f.w) / 2), oy = Math.floor((h - s * f.h) / 2);
    ghosts(ctx, f.w, f.h, s, ox, oy);
    const at = (c) => [ox + c.x * s, oy + c.y * s];
    // nest: a hollow square; shell: a prompt glyph, lit while a command runs
    let [x, y] = at(f.nest);
    ctx.strokeStyle = INK; ctx.lineWidth = Math.max(1, s * 0.12);
    ctx.strokeRect(x + s * 0.18, y + s * 0.18, s * 0.64, s * 0.64);
    [x, y] = at(f.shell);
    ctx.fillStyle = f.shell.lit ? INK : DIM;
    ctx.font = `500 ${Math.round(s * 0.8)}px "Spec Mono", ui-monospace, monospace`;
    ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText('>_', x + s / 2, y + s / 2 + 1);
    for (const fd of f.food) {
      [x, y] = at(fd);
      if (fd.state === 'eaten') { ctx.strokeStyle = DIM; ctx.lineWidth = 1; ctx.strokeRect(x + s * 0.3, y + s * 0.3, s * 0.4, s * 0.4); }
      else if (fd.state === 'nibbled') { pixel(ctx, x, y, s, INK, 0.3); }
      else { pixel(ctx, x, y, s, INK, 0.2); ctx.fillStyle = 'rgba(193,195,189,1)'; ctx.fillRect(x + s * 0.42, y + s * 0.42, s * 0.16, s * 0.16); }
      ctx.fillStyle = DIM;
      ctx.font = `400 ${Math.max(9, Math.round(s * 0.42))}px "Spec Mono", ui-monospace, monospace`;
      ctx.textAlign = fd.x > f.w / 2 ? 'right' : 'left';
      ctx.fillText(fd.label.split('/').pop(), fd.x > f.w / 2 ? x - 2 : x + s + 2, y + s / 2);
    }
    f.body.slice().reverse().forEach((c, i, all) => {
      [x, y] = at(c);
      const head = i === all.length - 1;
      pixel(ctx, x, y, s, head ? KEY : INK, head ? 0.06 : 0.14);
    });
    if (f.verified !== null) banner(ctx, w, h, f.verified ? 'verified' : 'check failed');
  }

  const KIND_FILL = { read: 0.35, edit: 0.6, model: 0.45, exec: 0.8, test: 0.8, done: 1 };
  function drawHill(ctx, w, h, f) {
    const s = Math.floor(Math.min(w / (f.w + 6), h / f.h)), ox = Math.floor((w - s * f.w) / 2), oy = Math.floor((h - s * f.h) / 2);
    ghosts(ctx, f.w, f.h, s, ox, oy);
    const cell = (cx, cy, kind, ok) => {
      const x = ox + cx * s, y = oy + (f.h - 1 - cy) * s;
      if (y < oy - s) return;
      pixel(ctx, x, y, s, ok === false ? KEY : `rgba(20,20,19,${KIND_FILL[kind] || 0.6})`, 0.08);
    };
    for (const c of f.cells) cell(c.x, c.y, c.kind, c.ok);
    if (f.falling) for (let i = 0; i < f.falling.w; i++) cell(f.falling.x + i, Math.min(f.h - 1, f.falling.y), f.falling.kind, f.falling.ok);
    ctx.strokeStyle = DIM; ctx.lineWidth = 1;
    ctx.strokeRect(ox - 0.5, oy - 0.5, s * f.w + 1, s * f.h + 1);
    // the legend for bar shades, beside the well
    ctx.font = `400 ${Math.max(9, Math.round(s * 0.45))}px "Spec Mono", ui-monospace, monospace`;
    ctx.textAlign = 'left'; ctx.textBaseline = 'middle';
    ['read', 'edit', 'exec', 'done'].forEach((k, i) => {
      const x = ox + s * f.w + s * 0.6, y = oy + s * (i * 1.2 + 0.2);
      pixel(ctx, x, y, s * 0.8, `rgba(20,20,19,${KIND_FILL[k]})`, 0.1);
      ctx.fillStyle = DIM; ctx.fillText(k, x + s, y + s * 0.4);
    });
    if (f.verified === true) {
      const x = ox + s * f.w / 2, y = oy + (f.h - f.top - 1) * s;
      ctx.strokeStyle = INK; ctx.lineWidth = Math.max(1, s * 0.12);
      ctx.beginPath(); ctx.moveTo(x, y + s); ctx.lineTo(x, y - s * 0.6); ctx.stroke();
      ctx.fillStyle = KEY; ctx.beginPath(); ctx.moveTo(x, y - s * 0.6); ctx.lineTo(x + s * 0.8, y - s * 0.3); ctx.lineTo(x, y); ctx.fill();
    }
    if (f.verified !== null) banner(ctx, w, h, f.verified ? 'verified' : 'check failed');
  }

  function banner(ctx, w, h, text) {
    const fs = Math.max(11, Math.round(h * 0.05));
    ctx.font = `500 ${fs}px "Spec Mono", ui-monospace, monospace`;
    const tw = ctx.measureText(text).width + fs * 1.4;
    ctx.fillStyle = INK;
    ctx.fillRect((w - tw) / 2, fs * 0.5, tw, fs * 1.8);   // centred at the top, clear of the hill's legend
    ctx.fillStyle = '#c4c5bf'; ctx.textAlign = 'center'; ctx.textBaseline = 'middle';
    ctx.fillText(text, w / 2, fs * 1.45);
  }

  function drawGrid() {
    if (!st.run) return;
    const { ctx, w, h } = fit($('grid'));
    const g = B.grid.frame(st.run.attempts, st.swarmT);
    const span = Math.max(8, g.cols, g.rows);   // a small swarm keeps small cells, centred, rather than four giant ones
    const s = Math.floor(Math.min(w, h) / span);
    const ox = Math.floor((w - s * g.cols) / 2), oy = Math.floor((h - s * g.rows) / 2);
    const mine = new Set(agents().map((a) => a.id));
    const cur = agent();
    for (const c of g.cells) {
      const x = ox + c.x * s, y = oy + c.y * s;
      const dim = !mine.has(c.id);
      const color = c.state === 'wait' ? GHOST : c.state === 'run' ? DIM : c.state === 'fail' ? KEY : (dim ? 'rgba(20,20,19,0.22)' : INK);
      if (c.state === 'wait') { ctx.fillStyle = GHOST; ctx.fillRect(x + s * 0.12, y + s * 0.12, s * 0.76, s * 0.76); }
      else pixel(ctx, x, y, s, color);
      if (cur && c.id === cur.id) { ctx.strokeStyle = KEY; ctx.lineWidth = Math.max(1.5, s * 0.14); ctx.strokeRect(x + 0.5, y + 0.5, s - 1, s - 1); }
    }
    const t = g.tally;
    $('grid-tally').textContent = st.swarmT === Infinity ? `${t.pass}/${st.run.attempts.length} pass` : `${t.run} running · ${t.pass + t.fail} done`;
    $('grid').setAttribute('aria-label', `swarm of ${st.run.attempts.length} agents: ${t.pass} passed, ${t.fail} failed`);
    st.gridGeom = { s, ox, oy, cols: g.cols, n: g.cells.length };
  }

  const KIND_LABEL = { read: 'read', edit: 'edit', exec: 'exec', test: 'test', done: 'done', model: 'turn', verify: 'check' };
  function renderTape() {
    const ol = $('tape');
    ol.replaceChildren();
    events().forEach((e, i) => {
      const li = document.createElement('li');
      li.dataset.ok = String(e.ok !== false);
      const arg = e.kind === 'verify' ? (e.ok ? 'pass' : 'fail') : (e.arg || '');
      for (const [cls, text] of [['n', String(i + 1).padStart(2, '0')], ['k', KIND_LABEL[e.kind] || e.kind], ['t', `${e.t.toFixed(2)}s`], ['a', arg]]) {
        const sp = document.createElement('span'); sp.className = cls; sp.textContent = text; li.append(sp);
      }
      ol.append(li);
    });
  }

  function syncTape(done) {
    const items = $('tape').children;
    for (let i = 0; i < items.length; i++) items[i].dataset.state = i < done - 1 ? 'done' : i === done - 1 ? 'now' : 'next';
    const now = items[done - 1];
    if (now) {
      const box = $('tape');
      const top = now.offsetTop - box.offsetTop;
      if (top < box.scrollTop || top > box.scrollTop + box.clientHeight - now.offsetHeight) box.scrollTop = top - box.clientHeight / 2;
    }
  }

  function draw() {
    const evs = events();
    const f = B.frame(st.board, evs, st.at);
    const { ctx, w, h } = fit($('board'));
    if (st.board === 'hill') drawHill(ctx, w, h, f); else drawTrail(ctx, w, h, f);
    drawGrid();
    const a = agent();
    const done = f.count;
    const cur = evs[done - 1];
    $('screen-tag').textContent = `${st.board} · ${a ? a.id.replace(/^c\d+-/, '') : '-'}`;
    $('screen-task').textContent = a && a.task ? a.task : '';
    $('screen-caption').textContent = !a ? 'no agents in this run' : !cur ? `${a.task || 'task'} · ${f.steps} steps · press A`
      : cur.kind === 'verify' ? (cur.ok ? 'checker: pass' : 'checker: fail') : `${KIND_LABEL[cur.kind] || cur.kind} ${cur.arg || ''}`;
    $('r-step').textContent = `${String(Math.min(done, f.steps)).padStart(2, '0')}/${String(f.steps).padStart(2, '0')}`;
    $('r-time').textContent = cur ? `${cur.t.toFixed(2)}s` : '0.00s';
    $('r-third').textContent = st.board === 'hill' ? String(f.cleared) : String(f.len);
    $('lamp-play').dataset.on = String(st.playing);
    $('lamp-pass').dataset.on = String(f.verified === true);
    $('lamp-fail').dataset.on = String(f.verified === false);
    $('btn-a').setAttribute('aria-pressed', String(st.playing));
    $('board').setAttribute('aria-label', `${st.board} board, step ${Math.min(done, f.steps)} of ${f.steps}${f.verified === null ? '' : f.verified ? ', verified' : ', check failed'}`);
    syncTape(done);
  }

  function renderReceipt() {
    const r = st.run;
    const rows = [
      ['run', r.id], ['preset', r.preset], ['title', r.title], ['agents', r.size], ['passed', `${r.passes}/${r.attempts_n}`],
      ['peak at once', r.max_running], ['median wall', r.wall_s_median == null ? null : `${r.wall_s_median}s`],
      ['harness', r.harness_version], ['plan', r.plan], ['chain head', r.chain], ['verify', r.verify], ['recorded', r.created],
    ];
    const dl = $('receipt');
    dl.replaceChildren();
    for (const [k, v] of rows) {
      const d = document.createElement('div');
      const dt = document.createElement('dt'); dt.textContent = k;
      const dd = document.createElement('dd'); dd.textContent = v == null ? '—' : String(v);
      d.append(dt, dd); dl.append(d);
    }
    $('legend').textContent = 'Trail: the ant walks to each file it reads (a nibble) or edits (a bite), to the shell for each command, '
      + 'and home when done; each step grows the trail, a failed step cuts it. Hill: each model turn drops a bar sized by '
      + 'what it did; full rows clear; a passing check plants the flag. Swarm: every agent of the run, filling in on the run\'s clock.';
  }

  // ---------- wiring ----------
  document.querySelectorAll('.dpad-zone').forEach((b) => b.addEventListener('click', () => press(b.dataset.dir)));
  document.querySelectorAll('.dial-stop[data-board]').forEach((b) => b.addEventListener('click', () => setBoard(b.dataset.board)));
  $('btn-a').onclick = toggle;
  $('btn-b').onclick = () => restart(false);
  $('run-prev').onclick = () => loadRun(st.runIdx - 1);
  $('run-next').onclick = () => loadRun(st.runIdx + 1);
  $('grid').addEventListener('click', (ev) => {
    const g = st.gridGeom; if (!g) return;
    const r = $('grid').getBoundingClientRect(), k = $('grid').width / r.width;
    const cx = Math.floor(((ev.clientX - r.left) * k - g.ox) / g.s), cy = Math.floor(((ev.clientY - r.top) * k - g.oy) / g.s);
    const i = cy * g.cols + cx;
    if (cx < 0 || cx >= g.cols || i < 0 || i >= g.n) return;
    const id = st.run.attempts[i].id, at = st.run.attempts[i];
    if (st.task && at.task !== st.task) { st.task = null; renderTasks(); }
    st.agent = Math.max(0, agents().findIndex((a) => a.id === id));
    restart(true);
  });
  document.addEventListener('keydown', (e) => {
    if (e.target.closest('input, textarea') || e.metaKey || e.ctrlKey || e.altKey) return;
    const map = { ArrowLeft: 'left', ArrowRight: 'right', ArrowUp: 'up', ArrowDown: 'down' };
    if (map[e.key]) { e.preventDefault(); press(map[e.key]); }
    else if (e.key === ' ' && !e.target.closest('button')) { e.preventDefault(); toggle(); }
    else if (e.key === 'r') restart(false);
    else if (e.key === 't') setBoard(st.board === 'trail' ? 'hill' : 'trail');
    else if (e.key === 'm') location.href = '/maze/';
  });
  addEventListener('resize', () => draw());
  reduced.addEventListener?.('change', () => { st.playing = false; st.swarmT = Infinity; draw(); });

  (async () => {
    try {
      const { runs } = await getJSON('/api/runs');
      st.runs = runs;
      if (!runs.length) { $('screen-caption').textContent = 'no stored Ante runs yet'; return; }
      const m = location.pathname.match(/^\/r\/([0-9a-f]{12})$/);
      const p = params();
      if (p.get('b') === 'hill') setBoard('hill');
      // Default: the biggest swarm, newest first among equals.
      let idx = m ? runs.findIndex((r) => r.id === m[1]) : -1;
      if (idx < 0) idx = runs.reduce((best, r, i) => ((r.size || 0) > (runs[best].size || 0) ? i : best), 0);
      await loadRun(idx, !!m);
      if (document.fonts) document.fonts.ready.then(draw);
    } catch (err) {
      $('screen-caption').textContent = 'could not load runs';
    }
  })();
})();
