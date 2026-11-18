// swarms front: pick harnesses (one, a pair, or all), a task and a size, then race them. Each lane is a grid of
// agents that light up as they are admitted, glow with their own activity and turn green when they pass hidden
// tests, with memory and CPU charts on one shared scale. Lane data is untrusted text: it only ever reaches the
// page through textContent or SVG attributes set from numbers.
(() => {
  const $ = (s, r = document) => r.querySelector(s);
  const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
  const NS = 'http://www.w3.org/2000/svg';
  const svgEl = (tag, attrs) => { const e = document.createElementNS(NS, tag); for (const k in attrs) e.setAttribute(k, attrs[k]); return e; };
  const fmt = (n, d = 0) => (n == null || !isFinite(n)) ? '–' : n.toLocaleString(undefined, { maximumFractionDigits: d, minimumFractionDigits: d });
  const median = (xs) => { const v = xs.filter((x) => x != null && isFinite(x)).sort((a, b) => a - b); return v.length ? v[(v.length - 1) >> 1] : null; };
  const max = (xs) => { const v = xs.filter((x) => x != null && isFinite(x)); return v.length ? Math.max(...v) : null; };
  const mb = (v) => v == null ? '–' : v >= 1024 ? `${fmt(v / 1024, 2)} GB` : v < 1 ? `${fmt(v * 1024, 0)} KB` : `${fmt(v, v < 10 ? 1 : 0)} MB`;

  const ROSTER = [
    { h: 'ante', name: 'Ante', build: 'Rust · own loop' },
    { h: 'codex', name: 'Codex', build: 'Rust · OpenAI loop' },
    { h: 'claude-code', name: 'Claude Code', build: 'TypeScript · Anthropic loop' },
    { h: 'nanocode', name: 'nanocode', build: 'Rust SDK · vendor loops' },
    { h: 'hermes', name: 'Hermes', build: 'Python · own loop' },
    { h: 'pi', name: 'Pi', build: 'TypeScript · own loop' },
  ];
  // Real models run through the Swarm gateway and are operator-only; each lane lists the ones its relay offers.
  // Provider -> model -> effort. The catalog comes from /api/lanes; an old backend sends none, so one "Live" provider is
  // built from whatever model ids the lanes list, with no efforts.
  let model = 'scripted', effort = null, provider = 'scripted', menuOpen = false, catalog = null, paneSig = '';
  const liveModels = () => [...new Set(connected().flatMap((r) => r.lane.models || []))];
  const cat = () => {
    if (catalog && Array.isArray(catalog.providers) && Array.isArray(catalog.models)) return catalog;
    const ids = liveModels();
    return { providers: [{ id: 'live', label: 'Live' }], models: ids.map((id) => ({ id, label: id, provider: 'live', efforts: [], default_effort: null })) };
  };
  const provModels = (pid) => cat().models.filter((m) => m.provider === pid);
  const modelOn = (m) => liveModels().includes(m.id);
  const effortsOf = (m) => (Array.isArray(m.efforts) ? m.efforts : []);
  const offers = (r) => model === 'scripted' || (r.lane.models || []).includes(model);

  let roster = ROSTER.map((r) => ({ ...r })), presets = [], preset = null, size = null, protocol = 'naive', picked = null;
  let race = null, source = null, data = {}, t0 = null, tick = null, lines = [], poll = null;
  // Repetitions: data[h].reps[k] is one lane's view of repetition k; `view` is the one on the panels. `follow` keeps the
  // panels on the repetition being run until someone picks another. `stats` is /stats of a series (paired ties, strips).
  let repeats = 1, view = 1, follow = true, stats = null;
  const slot = (h, k = view) => { const D = data[h] || (data[h] = { reps: {} }); return D.reps[k] || (D.reps[k] = blank()); };

  async function api(path, opts) {
    try {
      const r = await fetch(path, { cache: 'no-store', ...opts });
      let body = {}; try { body = await r.json(); } catch { /* empty */ }
      return { ok: r.ok, status: r.status, body };
    } catch { return { ok: false, status: 0, body: {} }; }
  }
  const note = (text, bad) => { const n = $('#note'); n.textContent = text || ''; n.classList.toggle('bad', !!bad); };
  const connected = () => roster.filter((r) => r.lane);
  const usable = (r) => r.lane && r.lane.state !== 'offline' && r.lane.state !== 'unclean';
  const blank = () => ({ agents: [], sample: {}, attempts: [], series: [], batch: null, elapsed: null, peak: 0, result: null, reset: null, warnings: [], memScope: null, state: null, position: null, error: null, done: false, act: {}, control: null, gate: null, canary: null, cohort: null, refused: null, runError: null });
  const idle = () => !race || (race.state !== 'running' && race.state !== 'calibrating');
  // A lane that has not started yet (queued, no error) is in the race: POST answers before the lanes have accepted it.
  const inRace = (r) => { const s = r.lane && race && race.lanes && race.lanes[r.lane.id]; return !!(s && (s.started || (!s.error && s.state === 'queued'))); };
  const raceHas = (r) => !!(r.lane && race && race.lanes && race.lanes[r.lane.id]);

  // ---- controls -------------------------------------------------------------------------------------
  function seg(host, items, current, label, pick, disabled) {
    host.replaceChildren(...items.map((it) => {
      const b = el('button', null, label(it)); b.type = 'button';
      b.setAttribute('aria-pressed', String(it === current));
      const off = disabled && disabled(it); if (off) { b.disabled = true; b.title = off; }
      b.onclick = () => pick(it); return b;
    }));
    host.hidden = !items.length;
  }
  function resetView() { if (idle()) { race = null; data = {}; stats = null; view = 1; follow = true; reps(); lines = []; $('#console').textContent = ''; build(); } }
  function renderControls() {
    if (picked == null) picked = new Set(connected().filter(usable).map((r) => r.lane.id));
    $('#chips').replaceChildren(...roster.map((r) => {
      const b = el('button', 'chip'); b.type = 'button'; b.append(r.name);
      if (!r.lane) { b.disabled = true; b.append(el('span', 'why', 'not connected')); }
      else if (r.lane.state === 'offline') { b.disabled = true; b.append(el('span', 'why', 'offline')); }
      else if (r.lane.state === 'unclean') { b.disabled = true; b.title = 'This machine was not back to a clean state after its last run, so it takes no race until it is.'; b.append(el('span', 'why', 'needs reset')); }
      else if (r.lane.state !== 'idle' && idle()) b.append(el('span', 'why', 'busy'));
      else if (!offers(r)) b.append(el('span', 'why', `no ${model}`));
      b.setAttribute('aria-pressed', String(!!(r.lane && picked.has(r.lane.id))));
      b.onclick = () => { if (!r.lane) return; picked.has(r.lane.id) ? picked.delete(r.lane.id) : picked.add(r.lane.id); renderControls(); resetView(); };
      return b;
    }));
    const p = presets.find((x) => x.id === preset);
    const pickPreset = (id) => {
      preset = id; const sizes = presets.find((x) => x.id === id).sizes || [];
      if (!sizes.includes(size)) size = fitSize(sizes);
      renderControls(); resetView();
    };
    const title = (id) => presets.find((x) => x.id === id).title || id;
    seg($('#seg-work'), presets.filter((x) => (x.kind || 'work') === 'work').map((x) => x.id), preset, title, pickPreset);
    seg($('#seg-harness'), presets.filter((x) => x.kind === 'harness').map((x) => x.id), preset, title, pickPreset,
      () => (model !== 'scripted' ? 'Harness tests replay a script; real models run the work tasks.' : null));
    $('#blurb').textContent = p ? (p.blurb || '') : '';
    const protos = p && Array.isArray(p.protocols) && p.protocols.length > 1 ? p.protocols : null;
    const prow = $('#protocol-row');
    if (prow) prow.hidden = !protos;
    if (protos) {
      if (!protos.includes(protocol)) protocol = protos[0];
      seg($('#seg-protocol'), protos, protocol, (id) => (id === 'collapsed' ? 'One program' : 'Naive path'), (id) => { protocol = id; renderControls(); resetView(); });
    } else protocol = 'naive';
    seg($('#seg-size'), p ? p.sizes : [], size, String, (s) => { size = s; renderControls(); resetView(); });
    // Repetitions: 1 or 6 on harness presets at one agent; 10 only with the operator passkey. The service enforces the same.
    const harnessOne = !!p && p.kind === 'harness' && size === 1 && model === 'scripted';
    const opts = harnessOne ? [1, 6, ...($('#passkey').value ? [10] : [])] : [1];
    if (!opts.includes(repeats)) repeats = 1;
    seg($('#seg-reps'), opts.length > 1 ? opts : [], repeats, String, (n) => { repeats = n; renderControls(); });
    $('#lbl-reps').hidden = opts.length < 2;
    const provs = cat().providers, pids = ['scripted', ...provs.map((x) => x.id)];
    const gone = 'Real models need the Swarm gateway on the lanes; operator-run.';
    seg($('#seg-provider'), pids, provider, (id) => (id === 'scripted' ? 'Scripted · $0' : (provs.find((x) => x.id === id) || {}).label || id), pickProvider,
      (id) => (id !== 'scripted' && !provModels(id).some(modelOn) ? gone : null));
    renderPane();
    $('#passkey').hidden = model === 'scripted' && !harnessOne;
    const chosen = connected().filter((r) => picked.has(r.lane.id) && offers(r));
    const blocked = chosen.filter((r) => r.lane.state !== 'idle');
    const btn = $('#run');
    const needKey = model !== 'scripted' && !$('#passkey').value;
    btn.disabled = !p || !chosen.length || blocked.length > 0 || needKey || !idle();
    const who = chosen.length === 1 ? `${chosen[0].name} alone` : chosen.length === 2 ? `${chosen[0].name} vs ${chosen[1].name}` : `${chosen.length} harnesses`;
    btn.textContent = !idle() ? 'Swarm running…' : chosen.length ? `Launch ${size || ''} agents${repeats > 1 ? ` × ${repeats} repetitions` : ''} · ${who}${model !== 'scripted' ? ` · ${model}${effort ? ` ${effort}` : ''}` : ''} →` : 'Pick a harness';
    if (idle()) note(needKey ? 'Real models are operator-only: enter the passkey.' : blocked.length ? `${blocked.map((r) => r.name).join(', ')} ${blocked.length === 1 ? 'is' : 'are'} busy with another race.` : '');
    status();
  }
  function pickModel(id, eff) {
    model = id; effort = eff || null;
    const p = presets.find((x) => x.id === preset);
    if (p && p.kind === 'harness') { const w = presets.find((x) => (x.kind || 'work') === 'work'); if (w) preset = w.id; }
    const sizes = (presets.find((x) => x.id === preset) || {}).sizes || [];
    if (!sizes.includes(size)) size = fitSize(sizes);  // the switch to a work task can change the sizes on offer
  }
  function pickProvider(id) {
    if (id === 'scripted') { provider = id; model = 'scripted'; effort = null; menuOpen = false; renderControls(); resetView(); return; }
    if (provider === id && model !== 'scripted') { menuOpen = !menuOpen; renderControls(); if (menuOpen) focusPane(); else $('#mtoggle').focus(); relayout(); return; }
    // Switching provider picks its first runnable model at its default effort and opens the menu to change it.
    const m = provModels(id).find(modelOn); if (!m) return;
    provider = id; pickModel(m.id, m.default_effort || effortsOf(m)[0]); menuOpen = true;
    renderControls(); resetView(); relayout(); focusPane();
  }
  function closeMenu(focus) { menuOpen = false; renderControls(); if (focus) $('#mtoggle').focus(); relayout(); }
  const focusPane = () => { const b = $('#mpane [aria-pressed="true"]') || $('#mpane button:not(:disabled)'); if (b) b.focus(); };
  const relayout = () => requestAnimationFrame(() => { layout(); fitAll(); charts(); });
  // The collapsed toggle shows the pick; the pane lists the provider's models, each with its efforts on the same row.
  function renderPane() {
    const tg = $('#mtoggle'), pane = $('#mpane'), show = provider !== 'scripted' && model !== 'scripted';
    const cur = cat().models.find((m) => m.id === model);
    $('#mchoice').hidden = !show;
    tg.textContent = `${cur ? cur.label : model}${effort ? ` · ${effort}` : ''} ${menuOpen ? '▴' : '▾'}`;
    tg.setAttribute('aria-expanded', String(show && menuOpen));
    pane.hidden = !(show && menuOpen);
    const sig = `${provider}|${model}|${effort}|${cat().models.map((m) => m.id + modelOn(m)).join()}`;
    if (pane.hidden || sig === paneSig) { if (pane.hidden) paneSig = ''; return; }
    paneSig = sig;
    pane.replaceChildren(...provModels(provider).map((m) => {
      const row = el('div', 'mrow'), on = modelOn(m);
      row.append(el('span', 'mname', m.label || m.id));
      const g = el('div', 'seg'), efs = effortsOf(m);
      (efs.length ? efs : [null]).forEach((e) => {
        const b = el('button', null, e || 'default'); b.type = 'button';
        b.setAttribute('aria-pressed', String(model === m.id && (effort || null) === (e || null)));
        if (!on) { b.disabled = true; b.title = 'No connected lane offers this model.'; }
        b.onclick = () => { pickModel(m.id, e); closeMenu(true); resetView(); };
        g.append(b);
      });
      row.append(g); return row;
    }));
  }
  $('#mtoggle').onclick = () => { menuOpen = !menuOpen; renderControls(); relayout(); if (menuOpen) focusPane(); };
  $('#mchoice').addEventListener('keydown', (e) => { if (e.key === 'Escape' && menuOpen) { e.preventDefault(); closeMenu(true); } });
  $('#mpane').addEventListener('keydown', (e) => { if (e.key === 'Escape' && menuOpen) { e.preventDefault(); closeMenu(true); } });
  // 32 when offered. No lane is sized down in advance: a harness that cannot hold the swarm shows it in the results.
  function fitSize(sizes) {
    return sizes.includes(32) ? 32 : sizes[sizes.length - 1];
  }
  $('#passkey').addEventListener('input', () => renderControls());
  $('#pick-all').onclick = () => { picked = new Set(connected().filter(usable).map((r) => r.lane.id)); renderControls(); resetView(); };

  // ---- lanes ------------------------------------------------------------------------------------------
  const target = () => Math.min((race ? race.size : size) || 0, 1024);
  // The biggest square cell that fits n cells in a w x h box: try every column count, keep the best.
  function geometry(n, w, h) {
    const gap = n > 128 ? 2 : n > 32 ? 3 : 4, cap = n <= 8 ? 34 : n <= 32 ? 26 : 18;
    let best = { cols: Math.max(n, 1), sz: 4 };
    for (let cols = 1; cols <= Math.max(n, 1); cols++) {
      const rows = Math.ceil(n / cols);
      const sz = Math.floor(Math.min((w - gap * (cols - 1)) / cols, h > 0 ? (h - gap * (rows - 1)) / rows : Infinity));
      if (sz > best.sz) best = { cols, sz };
    }
    const sz = Math.max(4, Math.min(best.sz, cap));
    return { cols: best.cols, sz, gap, r: Math.max(2, Math.round(sz / 4.5)) };
  }
  // Tiles for every harness in one pane: choose the column count whose tiles come closest to a readable shape.
  function layout() {
    const board = $('#lanes'), tiles = board.children.length || 1, wide = innerWidth > 760;
    board.style.height = '';
    if (!wide) { board.style.removeProperty('--tcols'); return; }
    const top = board.getBoundingClientRect().top + scrollY;
    const h = Math.max(380, innerHeight - top - 14), w = board.clientWidth;
    let pick = 3, score = -1;
    for (let c = 1; c <= tiles; c++) {
      const r = Math.ceil(tiles / c), tw = w / c, th = h / r;
      const s = Math.min(tw / 360, th / 300) - (c * r - tiles) * 0.02; // tiles want ~360 x 300 at least; empty slots cost a little
      if (s > score) { score = s; pick = c; }
    }
    board.style.setProperty('--tcols', pick);
    board.style.height = `${h}px`;
  }
  function fitGrid(grid, n) {
    const box = grid.parentElement, g = geometry(n, box.clientWidth || 300, box.clientHeight || 0);
    grid.style.setProperty('--cols', g.cols); grid.style.setProperty('--sz', `${g.sz}px`);
    grid.style.setProperty('--gap', `${g.gap}px`); grid.style.setProperty('--r', `${g.r}px`);
  }
  const fitAll = () => document.querySelectorAll('.lane:not(.off) .grid').forEach((g) => fitGrid(g, target()));
  // [key, label, what the number is]. "MB / agent" is the swarm's memory divided by the agents alive, so a harness of
  // several processes and one of a single process are measured the same way; "largest proc MB" is one process.
  const STATS = [['pass', 'passed'], ['alive', 'alive'], ['fail', 'failed'],
    ['per', 'MB / agent', 'Memory all agents held together, divided by the agents alive, at the moment most were alive. Shared pages count once.'],
    ['rss', 'largest proc MB', 'Median over agents of the largest single process (kernel rusage). A harness that runs several processes holds more than this.'],
    ['cpu', 'CPU s / agent'], ['disk', 'disk / agent', 'What the agent itself wrote to its sandbox between launch and exit. The runner\u2019s own setup is not counted.'],
    ['start', 'startup s', 'Launch to first model request, median over the agents that made one.'], ['gap', 'step gap s'], ['slope', 'bytes / step'],
    ['peakmem', 'peak memory', 'Most memory all agents held together. It cannot exceed the machine\u2019s memory.'], ['wall', 'wall s']];
  function order() {
    // Lanes in the race (or picked, before one) first, then the rest, then lanes with no machine.
    const rank = (r) => (!r.lane ? 2 : (race ? raceHas(r) : picked && picked.has(r.lane.id)) ? 0 : 1);
    return [...roster].sort((a, b) => rank(a) - rank(b));
  }
  function build() {
    const n = target();
    $('#lanes').replaceChildren(...order().map((r) => {
      slot(r.h);
      const row = el('div', r.lane ? 'lane' : 'lane off'); row.id = `lane-${r.h}`;
      const who = el('div', 'who'), name = el('div', 'name'); name.append(el('b', null, r.name), el('span', null, r.build), el('span', 'lead', ''), el('span', 'tag rep'), el('span', 'tag gate'), el('span', 'tag canary')); who.append(name);
      if (!r.lane) { row.append(who, el('div', 'why', 'not connected yet')); return row; }
      who.append(el('em', 'msg'));
      const box = el('div', 'cells'), grid = el('div', 'grid');
      grid.append(...Array.from({ length: n }, () => el('div', 'cell')));
      const charts = el('div', 'charts');
      for (const [k, t] of [['mem', 'memory in use'], ['cpu', 'CPU, whole machine']]) {
        const c = el('div', `chart c-${k}`), ct = el('div', 'ct'); ct.append(el('span', null, t), el('b', null, ''));
        c.append(ct, svgEl('svg', { role: 'img', 'aria-label': `${r.name} ${t} over time` })); charts.append(c);
      }
      box.append(grid);
      const stats = el('div', 'stats');
      for (const [k, t, tip] of STATS) { const s = el('div', `stat ${k}`); const lab = el('span', null, t); if (tip) lab.title = tip; s.append(el('b', `v-${k}`, '–'), lab); stats.append(s); }
      row.append(who, box, charts, stats);
      return row;
    }));
    layout(); fitAll();
    roster.forEach(paint); receipt();
  }
  // Counting and ranking rules live in rank.js (tested in node); this file only draws them.
  const { FAIL_WORDS, tally, excluded } = Rank, heatVals = Rank.values;
  const lanePeaks = (d) => Rank.peaks(d.series, d.attempts);
  // What a lane's run is doing before and around its agents (snapshot run.state; queued is told by its position).
  const RUN_WORDS = { checking: 'checking the machine is quiet', control: 'running this VM’s control' };
  // The refusal text is the service's public sentence; the page shortens it to the figure: "other CPU 0.6 cores".
  const quietWhy = (e) => String(e || '').replace(/^.*?measure:\s*/, '').replace(/\.$/, '').replace(/ busy \(([^)]*)\)/g, ' $1') || 'unknown';
  const pubOf = (r) => { const rp = race && race.reps && race.reps[view - 1], L = rp ? rp.lanes : race && race.lanes; return (L && r.lane && L[r.lane.id]) || null; };
  const CTL_WHAT = { start: 'first request', gap: 'step gap', cpu: 'CPU' }, CTL_FIELD = { start: 'first_request_s', gap: 'step_gap_s', cpu: 'cpu_s' };
  // `×4.2 ctl` and the tooltip saying what the control is. Failed or noisy controls say so; no control shows nothing.
  function ratioTip(k, ctl, rel) {
    if (!ctl || ['off', 'skipped', 'pending'].includes(ctl.state)) return null;
    if (rel.noControl) return ['no ctl', `${rel.noControl}: this lane’s ${CTL_WHAT[k]} is shown raw and is not ranked.`];
    const c = ctl.median[CTL_FIELD[k]];
    return rel.ratio[k] == null ? null : [`×${fmt(rel.ratio[k], rel.ratio[k] < 1 ? 2 : 1)} ctl`,
      `Control: mini on this VM, one agent, median of ${(ctl.values[CTL_FIELD[k]] || []).length || 3} runs, ${CTL_WHAT[k]} ${fmt(c, c < 1 ? 3 : 2)} s. This number is ×${fmt(rel.ratio[k], 2)} that.`];
  }
  const GATE_BITS = [['other CPU', 'cpu_cores_other', ' cores'], ['own CPU', 'cpu_cores_self', ' cores'], ['steal', 'steal_cores', ' cores'], ['swap in use', 'swap_used_mb', ' MB'],
    ['memory available', 'available_mb', ' MB'], ['at idle', 'baseline_available_mb', ' MB'], ['disk free', 'disk_free_mb', ' MB'], ['CPU pressure', 'psi_cpu_some_avg10', ''],
    ['pressure', 'pressure', ''], ['agent processes', 'agent_processes', ''], ['sandbox roots', 'sandbox_roots', '']];
  // Unknown readings are left out, never printed as 0.
  function gateText(g) {
    const v = g.values || {}, bits = GATE_BITS.filter(([, k]) => v[k] != null).map(([l, k, u]) => `${l} ${typeof v[k] === 'number' ? fmt(v[k], v[k] < 10 ? 2 : 0) : v[k]}${u}`);
    const held = (g.held_for || []).map((x) => String(x).replace(/_/g, ' '));
    return `Checked before ${g.stage} (${g.mode}): ${g.ok ? 'quiet' : `held for ${held.join(', ') || 'unknown'}`}${held.length && g.ok ? ` (waited, then quiet)` : ''}\n${bits.join(' · ')}`;
  }
  // Header chips: which repetition, whether the machine was quiet, whether the harness changed since its last run.
  function tags(row, r, d, pub) {
    const gates = d.gate || (pub && pub.gate) || [], g = gates[gates.length - 1], can = d.canary || (pub && pub.canary);
    const set = (cls, text, tip, warn) => { const t = $(`.tag.${cls}`, row); t.textContent = text; t.title = tip || ''; t.classList.toggle('warn', !!warn); };
    set('rep', race && race.repeats > 1 ? `rep ${view} of ${race.repeats}` : '', 'Repetition shown. The others are in the strips and the picker.');
    set('gate', g ? (g.ok ? 'quiet' : g.mode === 'observe' ? 'not quiet · observed' : 'not quiet') : '', g ? gates.map(gateText).join('\n') : '', g && !g.ok);
    set('canary', can && can.changed ? 'harness changed' : '', can && can.changed ? `This harness’s first request or binary differs from this lane’s previous run${(can.parts_changed || []).length ? ` (${can.parts_changed.join(', ').replace(/_/g, ' ')})` : ''}. It starts a new cohort and is not pooled with earlier rows.` : '', true);
  }
  const WARN_WORDS = { sandbox_leftover: 'sandboxes left behind', shared_seed_changed: 'shared prepared home was modified', reset_failed: 'machine not clean after the run' };
  // A cell's class for each state the lane reports. Every one of these is named in the legend.
  const CELL = { waiting: 'cell', preparing: 'cell prep', starting: 'cell start', working: 'cell alive', active: 'cell alive',
    checking: 'cell check', passed: 'cell passed', failed: 'cell failed', notrun: 'cell norun' };
  function paint(r) {
    const row = $(`#lane-${r.h}`), d = slot(r.h);
    if (!row || !r.lane) return;
    const pub = race && race.lanes ? race.lanes[r.lane.id] : null, live = inRace(r) && !!(pub && pub.started);
    if (pub && !pub.started && pub.error && !d.error) d.error = `did not join: ${pub.error}`;
    row.classList.toggle('out', race ? !raceHas(r) : !(picked && picked.has(r.lane.id)));
    const n = target(), grid = $('.grid', row);
    if (grid.children.length !== n) { grid.replaceChildren(...Array.from({ length: n }, () => el('div', 'cell'))); fitGrid(grid, n); }
    if (live) d.agents.forEach((a) => {
      const c = grid.children[a.slot]; if (!c) return;
      const k = CELL[a.state] || 'cell alive';
      if (c.className !== k) c.className = k;
    });
    else for (const c of grid.children) c.className = 'cell';
    const A = d.attempts, T = tally(A), passed = T.passed, failed = T.failed, pk = lanePeaks(d);
    const ctl = d.control || (pubOf(r) || {}).control;
    const rel = Rank.relative({ start: median(A.map((a) => a.first_request_s)), gap: median(A.map((a) => a.step_gap_s)), cpu: median(A.map((a) => a.cpu_s)) }, ctl, race ? race.size : size);
    const alive = d.done ? 0 : (d.sample.running ?? 0);
    const prep = d.done ? 0 : d.agents.filter((a) => a.state === 'preparing').length;
    // tip: [ratio text, what it is] -> `×4.2 ctl` beside the number, the control's own value in its tooltip.
    const set = (k, main, small, tip) => {
      const b = $(`.v-${k}`, row); b.textContent = main; if (small) b.append(el('small', null, small));
      if (tip && main !== '–') { const c = el('small', 'ctl', ` ${tip[0]}`); c.title = tip[1]; b.append(c); }
    };
    set('pass', live ? String(passed) : '–', live ? `/${n}` : null);
    set('alive', live ? String(alive) : '–');
    const rss = median(A.map((a) => a.rss_mb));
    set('rss', rss != null ? fmt(rss, rss < 100 ? 1 : 0) : '–');
    set('per', pk.per != null ? fmt(pk.per, pk.per < 100 ? 1 : 0) : '–');
    const cpu = median(A.map((a) => a.cpu_s));
    set('cpu', cpu != null ? fmt(cpu, cpu < 10 ? 2 : 1) : '–', null, ratioTip('cpu', ctl, rel));
    const disk = median(A.map((a) => a.disk_kb));
    set('disk', disk != null ? mb(disk / 1024) : '–');
    const st = median(A.map((a) => a.first_request_s)), reached = A.filter((a) => a.first_request_s != null).length;
    const base = pub && pub.baseline_s;
    // Only agents that reached a first request have a startup time: say so when that is not all of them.
    const some = st != null && reached < A.length ? `${reached}/${A.length}` : null;
    set('start', st != null ? fmt(st, st < 1 ? 2 : 1) : '–', [some, base != null && !ctl ? `vm ${fmt(base, 2)}` : null].filter(Boolean).join(' · ') || null, ratioTip('start', ctl, rel));
    const gap = median(A.map((a) => a.step_gap_s));
    set('gap', gap != null ? fmt(gap, gap < 1 ? 3 : 2) : '–', null, ratioTip('gap', ctl, rel));
    const slope = median(A.map((a) => a.context_slope));
    set('slope', slope != null ? fmt(slope, 0) : '–');
    set('fail', live ? String(failed) : '–', live && T.failClass ? T.failClass.replace(/_/g, ' ') : null);
    set('peakmem', pk.mem != null ? mb(pk.mem) : '–');
    const wall = d.batch ?? d.elapsed;
    // Agents stopped at the time limit make the wall time mostly the limit itself: marked, and not compared.
    set('wall', live && d.state !== 'queued' && wall != null ? fmt(wall, 1) : '–', live && T.timeouts ? `${T.timeouts} at limit` : null);
    $('.stat.pass', row).classList.toggle('on', passed > 0); $('.stat.fail', row).classList.toggle('on', failed > 0);
    $('.c-mem .ct span', row).title = d.memScope === 'rss-sum' ? 'This machine cannot report shared pages: the sum of each process\u2019s memory, which counts shared pages more than once.' : 'Memory all running agents hold together; shared pages count once.';
    $('.c-mem .ct span', row).textContent = d.memScope === 'rss-sum' ? 'memory in use (overcounted)' : 'memory in use';
    tags(row, r, d, pub);
    const msg = $('.msg', row); msg.className = 'msg';
    if (r.lane.state === 'offline') { msg.className = 'msg bad'; msg.textContent = 'not answering'; }
    else if (d.refused === 'not_quiet') { msg.className = 'msg bad'; msg.textContent = `not run: lane not quiet (${quietWhy(d.runError)})`; }
    else if (d.refused === 'lane_not_clean') { msg.className = 'msg bad'; msg.textContent = 'not run: lane not clean after its last run'; }
    else if (d.error && pub) { msg.className = 'msg bad'; msg.textContent = d.error; }
    else if (live && RUN_WORDS[d.state]) msg.textContent = RUN_WORDS[d.state];
    else if (pub && !live && !pub.error && inRace(r)) msg.textContent = 'queued';
    else if (live && d.state === 'queued') msg.textContent = `queued behind ${d.position ?? 'another'} run${d.position === 1 ? '' : 's'}`;
    else if (live && d.done) {
      const ran = A.length > 0, parts = [];
      // Ours first: an attempt the runner could not set up is not a failure of the harness, and must not read as one.
      if (T.notRun) parts.push(`${T.notRun} not run · ${T.notRunWhy}`);
      if (failed) parts.push(`${failed} failed · ${FAIL_WORDS[T.failClass] || (T.failClass || '').replace(/_/g, ' ')}`);
      (d.warnings || []).forEach((w) => parts.push(WARN_WORDS[w] || String(w).replace(/_/g, ' ')));
      msg.className = !ran ? 'msg bad' : T.notRun || (d.warnings || []).length ? 'msg warn' : 'msg';
      msg.textContent = !ran ? 'did not run' : parts.length ? parts.join(' · ') : 'all passed';
    }
    else msg.textContent = live ? (prep ? `setting up ${prep} sandbox${prep === 1 ? '' : 'es'}${alive ? ` · ${alive} running` : ''}` : 'running') : '';
    charts();
  }

  // ---- charts: one series each, a shared x (seconds) and y (MB, %) across lanes so position compares --------
  function scales() {
    let xmax = 5, ymem = 512;
    roster.forEach((r) => {
      const s = slot(r.h).series; if (!s.length) return;
      xmax = Math.max(xmax, s[s.length - 1][0] - s[0][0]);
      ymem = Math.max(ymem, max(s.map((x) => x[2])) || 0);
    });
    const step = ymem > 4096 ? 2048 : ymem > 1024 ? 512 : 256;
    return { xmax, ymem: Math.ceil(ymem / step) * step };
  }
  function charts() {
    const sc = scales();
    roster.forEach((r) => {
      const row = r.lane && $(`#lane-${r.h}`); if (!row) return;
      const d = slot(r.h), s = d.series, t0s = s.length ? s[0][0] : 0;
      const none = Rank.emptyWhy(s, d.attempts, d.done);
      draw($('.c-mem', row), s.map((x) => [x[0] - t0s, x[2]]), sc.xmax, sc.ymem, (v) => mb(v), none);
      draw($('.c-cpu', row), s.map((x) => [x[0] - t0s, x[3]]), sc.xmax, 100, (v) => `${fmt(v, 0)}%`, s.length ? 'too brief to sample' : none);
    });
  }
  function draw(box, pts, xmax, ymax, label, none) {
    const svg = $('svg', box), W = Math.max(120, svg.clientWidth || 300), H = Math.max(28, svg.clientHeight || 56), pad = 3;
    svg.setAttribute('viewBox', `0 0 ${W} ${H}`); svg.replaceChildren();
    const good = pts.filter((p) => p[1] != null && isFinite(p[1]));
    const X = (x) => (x / xmax) * W, Y = (y) => H - pad - (Math.min(y, ymax) / ymax) * (H - 2 * pad);
    svg.append(svgEl('line', { class: 'base', x1: 0, x2: W, y1: H - 0.5, y2: H - 0.5 }));
    const peak = good.reduce((m, p) => (m && m[1] >= p[1] ? m : p), null);
    $('.ct b', box).textContent = peak ? `peak ${label(peak[1])}` : '';
    if (good.length < 2 || !(peak && peak[1] > 0)) {
      const t = svgEl('text', { class: 'empty', x: 0, y: H / 2 + 4 }); t.textContent = none;
      if (pts.length && !good.length) t.textContent = 'not measured on this machine';
      svg.append(t); $('.ct b', box).textContent = ''; return;
    }
    const line = good.map((p, i) => `${i ? 'L' : 'M'}${X(p[0]).toFixed(1)},${Y(p[1]).toFixed(1)}`).join('');
    svg.append(svgEl('path', { class: 'area', d: `${line}L${X(good[good.length - 1][0]).toFixed(1)},${H}L${X(good[0][0]).toFixed(1)},${H}Z` }));
    svg.append(svgEl('path', { class: 'line', d: line }));
    svg.append(svgEl('circle', { class: 'peak', cx: X(peak[0]), cy: Y(peak[1]), r: 4 }));
    const hair = svgEl('line', { class: 'hair', y1: 0, y2: H, x1: -10, x2: -10 }), dot = svgEl('circle', { class: 'dot', r: 4, cx: -10, cy: -10 });
    const hit = svgEl('rect', { x: 0, y: -8, width: W, height: H + 16, fill: 'transparent' });
    svg.append(hair, dot, hit);
    const tip = $('#tip');
    hit.onpointermove = (e) => {
      const rect = svg.getBoundingClientRect(), x = ((e.clientX - rect.left) / rect.width) * W, t = (x / W) * xmax;
      const p = good.reduce((m, q) => (Math.abs(q[0] - t) < Math.abs(m[0] - t) ? q : m), good[0]);
      hair.setAttribute('x1', X(p[0])); hair.setAttribute('x2', X(p[0])); dot.setAttribute('cx', X(p[0])); dot.setAttribute('cy', Y(p[1]));
      tip.hidden = false; tip.textContent = `${fmt(p[0], 1)} s · ${label(p[1])}`;
      tip.style.left = `${rect.left + (X(p[0]) / W) * rect.width}px`; tip.style.top = `${rect.top + (Y(p[1]) / H) * rect.height}px`;
    };
    hit.onpointerleave = () => { tip.hidden = true; hair.setAttribute('x1', -10); hair.setAttribute('x2', -10); dot.setAttribute('cx', -10); };
  }

  // Alive cells glow with their own agent's activity: bright on each tool call or message, fading back to rust.
  setInterval(() => {
    const now = performance.now();
    roster.forEach((r) => {
      const row = r.lane && $(`#lane-${r.h}`), d = data[r.h] && slot(r.h); if (!row || !d) return;
      const cells = $('.grid', row).children;
      for (let i = 0; i < cells.length; i++) {
        const c = cells[i]; if (c.className !== 'cell alive') continue;
        const last = d.act[i], a = last ? 0.22 + 0.78 * Math.exp(-(now - last) / 900) : 0.3;
        c.style.setProperty('--a', a.toFixed(2));
      }
    });
  }, 200);

  function log(h, e) {
    if (e.slot == null) return;
    const name = (roster.find((r) => r.h === h) || { name: h }).name;
    let what = e.k;
    if (e.k === 'tool') what = `${e.tool || 'tool'} ${e.arg || ''}`.trim();
    else if (e.k === 'toolerr') what = `${e.tool || 'tool'} error`;
    else if (e.k === 'msg') what = `“${e.text || ''}”`;
    else if (e.k === 'result') what = e.ok ? 'passed hidden tests' : 'failed';
    else if (e.k === 'admit') what = 'admitted';
    lines.push(`${name.padEnd(12)} #${String(e.slot + 1).padStart(3, '0')}  ${what}`);
    if (lines.length > 8) lines.splice(0, lines.length - 8);
  }

  // ---- race stream --------------------------------------------------------------------------------
  const laneHarness = (lid) => (roster.find((r) => r.lane && r.lane.id === lid) || {}).h;
  function absorb(h, snap, k) {
    const d = slot(h, k);
    const pr = snap.progress || {}, now = performance.now();
    if (Array.isArray(pr.agents)) d.agents = pr.agents;
    if (snap.run) {
      for (const f of ['control', 'gate', 'canary', 'cohort']) if (snap.run[f] != null) d[f] = snap.run[f];
      d.refused = snap.run.refused || null; d.runError = snap.run.error || null;
    }
    if (snap.run) { d.state = snap.run.state; d.result = snap.run.result || null; d.reset = snap.run.reset || null; d.warnings = Array.isArray(snap.run.warnings) ? snap.run.warnings : []; }
    if (snap.mem_scope) d.memScope = snap.mem_scope;
    d.position = snap.position;
    if (Array.isArray(snap.series) && snap.series.length) d.series = snap.series.filter((x) => Array.isArray(x) && x.length >= 5);
    const s = snap.sample || {};
    if (Object.keys(s).length) {
      d.sample = s;
      const last = d.series[d.series.length - 1];
      if (s.t != null && (!last || s.t > last[0])) d.series.push([s.t, s.running, s.agents_mem_mb, s.cpu_pct, s.available_mb]);
      if (d.series.length > 1200) d.series.splice(0, d.series.length - 1200);
    }
    d.peak = Math.max(d.peak, d.sample.running || 0);
    if (pr.elapsed_s != null) d.elapsed = pr.elapsed_s;
    if (pr.batch_wall_s != null) d.batch = pr.batch_wall_s;
    if (Array.isArray(snap.attempts)) d.attempts.push(...snap.attempts);
    if (Array.isArray(snap.console)) snap.console.forEach((e) => { if (e.slot != null) d.act[e.slot] = now; log(h, e); });
    if (d.state === 'done' || d.state === 'failed') d.done = true;
  }
  function status() {
    const s = $('#status'), of = race && race.repeats > 1 ? ` · rep ${race.rep || 1} of ${race.repeats}` : '';
    if (race && race.state === 'running' && t0 != null) s.textContent = `running${of} · ${fmt((performance.now() - t0) / 1000, 1)} s`;
    else if (race) s.textContent = `${race.state}${of} · ${race.size} agents per VM`;
    else s.textContent = connected().length ? `${connected().length} lanes connected` : 'no lanes';
  }
  // The repetition picker, one button per repetition; the one being run is marked.
  function reps() {
    const host = $('#reps'), n = race && race.repeats > 1 ? race.repeats : 0;
    host.hidden = !n;
    host.replaceChildren(...Array.from({ length: n }, (x, i) => {
      const k = i + 1, b = el('button', race.state === 'running' && race.rep === k ? 'live' : null, `rep ${k}`); b.type = 'button';
      b.setAttribute('aria-pressed', String(k === view));
      b.onclick = () => { view = k; follow = race.state === 'running' && k >= (race.rep || 1); reps(); roster.forEach(paint); receipt(); charts(); };
      return b;
    }));
  }
  // What a finished repetition left in the race record: results and control for the picker after a reload.
  function seed(r) {
    (r.reps || []).forEach((rp) => Object.entries(rp.lanes || {}).forEach(([lid, v]) => {
      const h = laneHarness(lid); if (!h) return;
      const d = slot(h, rp.k);
      for (const f of ['result', 'control', 'gate', 'canary', 'cohort', 'refused']) if (v[f] != null) d[f] = v[f];
      if (v.state) d.state = v.state;
      if (v.state === 'done' || v.state === 'failed') d.done = true;
    }));
  }
  async function loadStats() {
    if (!race || !(race.repeats > 1)) return;
    const id = race.id, r = await api(`api/races/${encodeURIComponent(id)}/stats`);
    if (r.ok && race && race.id === id && r.body && r.body.metrics) { stats = r.body; receipt(); }
  }
  function attach(r, live) {
    if (source) source.close();
    clearInterval(poll);
    race = r; data = {}; lines = []; stats = null; $('#console').textContent = '';
    view = r.rep || 1; follow = true;
    if (r.preset && presets.some((p) => p.id === r.preset)) preset = r.preset;
    roster.forEach((x) => { data[x.h] = { reps: {} }; }); seed(r); build(); renderControls(); reps();
    clearInterval(tick); t0 = performance.now();
    if (live) tick = setInterval(status, 100); status();
    listen(r);
    // The launch answers before the lanes have accepted it: poll the race until it settles, so a lane that refuses
    // ("not clean after its last run", "offline") or a race in which none accepted shows up.
    poll = setInterval(async () => {
      if (!race || race.id !== r.id) return clearInterval(poll);
      const g = await api(`api/races/${encodeURIComponent(r.id)}`);
      if (!g.ok || !race || race.id !== r.id) return;
      race = g.body; seed(race); reps(); roster.forEach(paint); renderControls(); receipt();
      if (race.state !== 'running') { clearInterval(poll); clearInterval(tick); status(); history(); refreshLanes(); loadStats(); }
    }, 2500);
    if (r.repeats > 1) loadStats();
  }
  // One event stream; at the service's 900 s limit it says `more` and this reattaches to the repetition under way.
  function listen(r) {
    source = new EventSource(`api/races/${encodeURIComponent(r.id)}/events`);
    let pending = false;
    source.onmessage = (ev) => {
      let m; try { m = JSON.parse(ev.data); } catch { return; }
      if (m.race) { race = m.race; seed(race); reps(); renderControls(); }
      if (m.more) { source.close(); source = null; listen(r); return; }
      if (m.start && !m.lane && m.rep) {  // a repetition opens (and on a reattach, the one under way restarts its view)
        roster.forEach((x) => { if (data[x.h]) data[x.h].reps[m.rep] = blank(); });
        if (follow) view = m.rep;
        reps(); roster.forEach(paint); receipt(); charts();
        if (m.rep > 1) loadStats();
        return;
      }
      const k = m.rep || 1, h = m.lane && laneHarness(m.lane);
      if (h && m.snap) absorb(h, m.snap, k);
      if (h && m.error) slot(h, k).error = m.error;
      if (h && m.end) slot(h, k).done = true;
      if (h && k === view) { paint(roster.find((y) => y.h === h)); $('#console').textContent = lines.join('\n'); }
      if (m.end && !m.lane) { // a lane's own end marker also carries `end`; only the race's closes the stream
        source.close(); source = null; clearInterval(tick); clearInterval(poll);
        roster.forEach(paint); receipt(); status(); history(); refreshLanes(); loadStats();
      } else if (m.snap && !pending) { pending = true; requestAnimationFrame(() => { pending = false; receipt(); }); }
    };
    source.onerror = () => { if (source && source.readyState === EventSource.CLOSED) { source = null; clearInterval(tick); note('Lost the live stream. Reload to reattach.', true); } };
  }

  // ---- receipt: per-agent comparison bars and the full table ----------------------------------------
  // The first request as it left the harness, medians over the agents (or the lane's summary after a reload).
  const FIRST_KEYS = ['canonical_bytes', 'system_bytes', 'tools_bytes', 'user_bytes', 'instruction_bytes', 'injected_bytes', 'tool_count'];
  function firstOf(r) {
    const d = slot(r.h), F = d.attempts.map((a) => a.first_request).filter(Boolean), res = d.result || {};
    if (F.length) { const f = Object.fromEntries(FIRST_KEYS.map((k) => [k, median(F.map((x) => x[k]))])); if (f.canonical_bytes != null) return f; }
    if (res.first_request_bytes_median == null) return null;
    const g = (k) => res[`first_request_${k}_median`] ?? null;
    return { canonical_bytes: res.first_request_bytes_median, system_bytes: g('system_bytes'), tools_bytes: g('tools_bytes'), user_bytes: g('user_bytes'),
      instruction_bytes: g('instruction_bytes'), injected_bytes: g('injected_bytes'), tool_count: g('tool_count') };
  }
  function metrics(r) {
    const d = slot(r.h), A = d.attempts, T = tally(A), pk = lanePeaks(d), res = d.result || {}, ran = A.length > 0;
    const disk = median(A.map((a) => a.disk_kb));
    // A repetition opened from the picker after a reload has no attempts here, only the lane's summary: its medians stand in.
    const own = (v, key, scale = 1) => (v != null || ran || res[key] == null ? v : res[key] * scale);
    // Passed per VM-hour comes from the lane's own summary, which leaves it blank when the wave never filled or
    // agents waited to be admitted: then the rate describes the machine's refusal, not the harness.
    const perMin = d.done && d.result ? d.result.successes_per_minute : null;
    const f = firstOf(r);
    return {
      passed: ran ? T.passed : res.passes ?? T.passed, failed: T.failed, notRun: T.notRun, timeouts: T.timeouts, n: race ? race.size : 0, failClass: T.failClass,
      vh: perMin != null ? perMin * 60 : null,
      rss: own(median(A.map((a) => a.rss_mb)), 'peak_rss_mb_median'), per: own(pk.per, 'mem_per_agent_mb'),
      cpu: own(median(A.map((a) => a.cpu_s)), 'cpu_s_median'), disk: own(disk != null ? disk / 1024 : null, 'disk_kb_median', 1 / 1024),
      tok: median(A.map((a) => (a.input_tokens != null ? a.input_tokens + (a.output_tokens || 0) : null))),
      start: own(median(A.map((a) => a.first_request_s)), 'first_request_s_median'), gap: own(median(A.map((a) => a.step_gap_s)), 'step_gap_s_median'),
      slope: own(median(A.map((a) => a.context_slope)), 'context_slope_median'), first: f ? f.canonical_bytes : null,
      mem: own(pk.mem, 'agents_mem_mb_peak'), cpuPk: pk.cpu, alive: pk.alive ?? (d.peak || null), wall: d.batch ?? d.elapsed, memScope: d.memScope,
      ctl: d.control || (pubOf(r) || {}).control || null,
    };
  }
  // ---- series: the medians and ties over repetitions come from /stats --------------------------------------
  const STAT_ROW = { pass: 'passed', per: 'per', rss: 'rss', cpu: 'cpu', disk: 'disk', start: 'start', gap: 'gap', slope: 'slope', peakmem: 'mem', wall: 'wall', vh: 'vh', first: 'first' };
  const RESULT_FIELD = { start: 'first_request_s_median', gap: 'step_gap_s_median', cpu: 'cpu_s_median', wall: 'wall_s_median' };
  const WHY_WORDS = { control_missing: 'no control', control_failed: 'control failed', control_skipped: 'no control', control_off: 'no control', control_pending: 'no control', control_noisy: 'control noisy',
    control_key: 'control differs from other lanes', cohort_changed: 'harness changed during the series', under_half_passed: 'under half passed', error: 'lane error', not_done: 'did not finish',
    no_result: 'no result', no_value: 'not measured', missing: 'did not run' };
  const whyWord = (x) => WHY_WORDS[x] || String(x || '').replace(/_/g, ' ');
  const repResult = (r, k) => (data[r.h] && data[r.h].reps[k] && data[r.h].reps[k].result) || ((race.reps[k - 1] || { lanes: {} }).lanes[r.lane.id] || {}).result || {};
  // m with the medians over repetitions: ratios straight from /stats, raw beside them from each repetition's own summary.
  function overlay(r, m) {
    const lid = r.lane.id, out = { ...m, ratio: {} };
    let lost = null;
    for (const [sk, row] of Object.entries(STAT_ROW)) {
      const M = stats.metrics[sk], sl = M && M.lanes[lid]; if (!sl) continue;
      if (!sl.n || sl.median == null) { out[row] = null; if (sk in Rank.NORMALISED) lost = lost || whyWord((sl.reasons || []).find(Boolean) || sl.excluded); continue; }
      if (M.relative_to_control && sk in Rank.NORMALISED) {
        out.ratio[sk] = sl.median;
        out[row] = median(sl.values.map((v, i) => (v == null ? null : repResult(r, i + 1)[RESULT_FIELD[sk]])));
      } else out[row] = sk === 'disk' ? sl.median / 1024 : sl.median;
    }
    out.noControl = lost && !Object.keys(out.ratio).length ? lost : null;
    return out;
  }
  const tiesOf = () => Object.fromEntries(Object.entries(stats.metrics).map(([k, M]) => [k, (M.pairs || []).map((p) => ({ a: laneHarness(p.a), b: laneHarness(p.b), tie: p.tie }))]));
  const statKey = (rowKey) => Object.keys(Rank.HEAT).find((k) => Rank.HEAT[k][0] === rowKey) || rowKey;
  // ---- heat: rank each number across the harnesses in the race (rules in rank.js, shading in heat.js) --------
  const HEAT_COLS = [null, null, ['passed', 'high'], null, null, ['vh', 'high'], ['per', 'low', 1], ['rss', 'low', 1], ['cpu', 'low', 1],
    ['disk', 'low', 1], ['start', 'low'], ['gap', 'low', 1], ['slope', 'low', 1], ['mem', 'low', 1], null, null, ['wall', 'low', 1]];
  // Shade one number, and say in its tooltip exactly which harnesses it was compared with.
  function shadeOne(node, rows, i, pos, key, gated) {
    const { r, m } = rows[i];
    Heat.shade(node, pos[i], Rank.compared(rows, i, pos, key, gated));
    node.classList.toggle('unranked', !!excluded(r, m, key, gated) && m[key] != null);  // shown, but took no part in the ranking
  }
  const field = (rows) => rows.filter(({ r }) => r.h !== 'mini').length;
  // rows: this repetition's numbers for the tiles. lead: Rank.score of the rows the table ranks (the series when there is one).
  function heatTiles(rows, lead) {
    const { positions } = Rank.score(rows, Heat.rank), { score } = lead, n = field(lead.rows);
    for (const [k, [key, , gated]] of Object.entries(Rank.HEAT)) rows.forEach(({ r }, i) => {
      const node = $(`#lane-${r.h} .stat.${k} b`); // the number only; its label stays on the panel
      if (node) shadeOne(node, rows, i, positions[k], key, gated);
    });
    roster.forEach((r) => {
      const lead = $(`#lane-${r.h} .lead`); if (!lead) return;
      const sc = score.get(r.h);
      lead.textContent = Rank.leadText(sc, n);
      lead.title = sc && sc.wins ? `Sole best on ${sc.wins} of the ${sc.of} numbers this harness was ranked on. Resource numbers were ranked against ${sc.narrow && sc.narrow.length ? sc.narrow.join(', ') : 'no other harness'}: harnesses where under half the agents passed are ranked on passes and startup only. Hover a number to see who it was compared with.` : '';
      if (!rows.some((x) => x.r.h === r.h)) document.querySelectorAll(`#lane-${r.h} .stat b`).forEach((s) => { Heat.shade(s, null); s.classList.remove('unranked'); });
    });
  }
  // The control tick of a bar: this lane's own control value for the metric (the reference line).
  const TICK = { start: 'first_request_s', gap: 'step_gap_s', cpu: 'cpu_s', rss: 'rss_mb' };
  const ctlOk = (c) => !!(c && c.state === 'done' && c.median);
  const ratioText = (q) => `×${fmt(q, q < 1 ? 2 : 1)}`;
  // One strip: a dot per repetition, median tick, 25–75% box, min–max line. Repetitions with no value are hollow dots in
  // the gutter left of the axis, never at 0. Geometry is Rank.strip.
  function stripSvg(vals, top, sl, cv, tick, best, why) {
    const W = 240, GU = 26, AX = W - GU - 2, svg = svgEl('svg', { viewBox: `0 0 ${W} 16`, class: 's', role: 'img', 'aria-label': 'one dot per repetition' });
    const g = Rank.strip(vals, top, AX, sl && sl.median != null ? { median: cv(sl.median), p25: cv(sl.p25), p75: cv(sl.p75), min: cv(sl.min), max: cv(sl.max) } : null);
    if (tick != null && top > 0) svg.append(svgEl('line', { class: 'ctl', x1: GU + (tick / top) * AX, x2: GU + (tick / top) * AX, y1: 0, y2: 16 }));
    if (g.range) svg.append(svgEl('line', { class: 'rng', x1: GU + g.range[0], x2: GU + g.range[1], y1: 8, y2: 8 }));
    if (g.box) svg.append(svgEl('rect', { class: 'box', x: GU + g.box[0], width: Math.max(1, g.box[1] - g.box[0]), y: 3, height: 10 }));
    if (g.median != null) svg.append(svgEl('line', { class: best ? 'med best' : 'med', x1: GU + g.median, x2: GU + g.median, y1: 1, y2: 15 }));
    let j = 0;
    g.dots.forEach((d) => {
      const c = d.x == null ? svgEl('circle', { class: 'hol', cx: 4 + 7 * Math.min(j++, 3), cy: 8, r: 2.6 }) : svgEl('circle', { class: 'dot', cx: GU + d.x, cy: 8, r: 2.6 });
      const t = svgEl('title', {}); t.textContent = d.v != null ? `rep ${d.i + 1}: ${fmt(d.v, d.v < 10 ? 2 : 1)}` : `rep ${d.i + 1}: ${whyWord(why[d.i] || 'not_done')}`; c.append(t); svg.append(c);
    });
    return svg;
  }
  function receipt() {
    const size1 = race ? race.size : size, multi = !!(stats && stats.n >= 2 && race && race.repeats > 1);
    const base = roster.filter(inRace).map((r) => ({ r, m: metrics(r) }));
    const odd = Rank.oddControls(Object.fromEntries(base.map(({ r, m }) => [r.h, m.ctl && m.ctl.key])));
    const one = Rank.markScopes(base.map(({ r, m }) => ({ r, m: Rank.relative({ ...m, mismatch: odd[r.h] }, m.ctl, size1) })));
    const rows = multi ? Rank.markScopes(one.map(({ r, m }) => ({ r, m: overlay(r, m) }))) : one;
    const ties = multi ? tiesOf() : null, lead = Rank.score(rows, Heat.rank, ties), score = lead.score;
    lead.rows = rows;
    heatTiles(one, lead);
    // [metric, title, sub, format, higher is better, gated like the ranking]
    const CARDS = [
      ['vh', 'Passed per VM-hour', 'higher is better', (v) => fmt(v), true, 1],
      ['per', 'Memory per agent', 'swarm memory ÷ agents alive, at peak', (v) => mb(v), false, 1],
      ['rss', 'Largest process', 'median, one process per agent', (v) => mb(v), false, 1],
      ['cpu', 'CPU per agent', 'median CPU seconds', (v) => `${fmt(v, v < 10 ? 2 : 1)} s`, false, 1],
      ['disk', 'Disk per agent', 'written by the agent, launch to exit', (v) => mb(v), false, 1],
      ['start', 'Startup', 'launch to first request, median', (v) => `${fmt(v, v < 1 ? 2 : 1)} s`, false, 0],
      ['gap', 'Step gap', 'reply sent to next request, median', (v) => `${fmt(v, v < 1 ? 3 : 2)} s`, false, 1],
      ['slope', 'Context growth', 'bytes per step after the first request', (v) => fmt(v, 0), false, 1],
      ['mem', 'Peak memory in use', 'all running agents', (v) => mb(v), false, 1],
      ['tok', 'Tokens per agent', 'input + output, median (real models)', (v) => fmt(v), false, 1],
    ];
    const SK = { vh: 'vh', per: 'per', rss: 'rss', cpu: 'cpu', disk: 'disk', start: 'start', gap: 'gap', slope: 'slope', mem: 'peakmem' };
    $('#bars').replaceChildren(...(rows.length < 1 ? [] : CARDS.filter(([k]) => rows.some((x) => x.m[k] != null)).map(([k, t, sub, f, higher, gated]) => {
      const sk = SK[k], M = multi && sk ? stats.metrics[sk] : null, rel = !!(M && M.relative_to_control);
      const cv = (v) => (sk === 'disk' ? v / 1024 : v), fm = rel ? ratioText : f;
      const card = el('div', 'bar-card'); card.append(el('h4', null, t), el('p', null, M ? `${sub} · ${rel ? '×control, ' : ''}median of ${stats.n}` : sub));
      const cells = rows.map(({ r, m }) => {
        const sl = M && M.lanes[r.lane.id], vals = sl ? sl.values.map((v) => (v == null ? null : cv(v))) : [];
        return { r, m, sl, vals, ctl: ctlOk(m.ctl) && TICK[k] ? m.ctl.median[TICK[k]] : null };
      });
      // Time on the bars of one repetition is raw; its tick is the control's raw value, so the axis reaches it too.
      const all = cells.flatMap((c) => (M ? c.vals : [c.m[k]])).filter((v) => v != null), top = Math.max(...all, ...(rel ? [1] : M ? [] : cells.map((c) => c.ctl).filter((v) => v != null)), 1e-9);
      // The marked best is the best among harnesses that take part in this metric's ranking, never a harness that
      // failed fast or never ran.
      const pool = heatVals(rows, k, gated).filter((v) => v != null);
      const best = pool.length > 1 ? (higher ? Math.max(...pool) : Math.min(...pool)) : null;
      cells.forEach(({ r, m, sl, vals, ctl }) => {
        const v = M ? (sl && sl.median != null ? (rel ? sl.median : cv(sl.median)) : null) : m[k], out = excluded(r, m, k, gated);
        const br = el('div', (out && v != null ? 'brow unranked' : 'brow') + (M ? ' strip' : '')), track = el('div', 't');
        if (out && v != null) br.title = out;
        if (M) track.append(stripSvg(vals, top, sl, cv, rel ? 1 : null, !out && v != null && v === best, sl ? sl.reasons || [] : []));
        else {
          const fill = el('i', !out && v != null && v === best ? 'best' : '');
          fill.style.width = v != null ? `${Math.max(0.5, (100 * v) / top)}%` : '0';
          track.append(fill);
          if (ctl != null) { const tk = el('u', 'ctl'); tk.style.left = `${Math.min(100, (100 * ctl) / top)}%`; tk.title = `control: ${f(ctl)} (mini on this VM)`; track.append(tk); }
        }
        const label = el('span', v != null ? 'v' : 'v dash', v != null ? fm(v) : '–');
        br.append(el('span', 'n', r.name), track, label); card.append(br);
      });
      return card;
    })));
    firstRequest(rows, lead);
    const t = $('#summary'), th = el('thead'), tr = el('tr');
    ['harness', 'n', 'passed', 'failed', 'not run', 'passed / VM-hour', 'MB / agent', 'largest proc MB', 'CPU s / agent', 'disk / agent', 'startup s', 'step gap s', 'bytes / step', 'peak memory', 'peak CPU', 'peak alive', 'wall', 'best on']
      .forEach((x) => tr.append(el('th', null, x))); th.append(tr);
    const names = rows.map((x) => x.r.h);
    const colPos = HEAT_COLS.map((c) => (c ? Rank.positions(heatVals(rows, c[0], c[2]), c[1], ties && ties[statKey(c[0])], Heat.rank, names) : null));
    const tb = el('tbody');
    order().forEach((r) => {
      const row = el('tr'); row.append(el('td', null, r.name));
      if (!inRace(r)) { const c = el('td', 'dim', !r.lane ? 'not connected yet' : race ? 'not in this race' : 'no race yet'); c.colSpan = 17; row.append(c); tb.append(row); return; }
      const m = (rows.find((x) => x.r.h === r.h) || { m: metrics(r) }).m, vh = el('td'); vh.append(el('b', null, m.vh != null ? fmt(m.vh) : '–'));
      const failed = el('td', null, String(m.failed)); if (m.failClass) failed.title = FAIL_WORDS[m.failClass] || m.failClass;
      const skipped = el('td', null, m.notRun ? String(m.notRun) : '–'); if (m.notRun) skipped.title = 'Attempts that never ran: the runner could not set them up, stopped them, or the machine had no room to admit them. Not counted as failures of the harness.';
      const sc = score.get(r.h), sl = multi && stats.metrics.pass.lanes[r.lane.id];
      const nTd = el('td', null, multi ? (sl ? String(sl.n) : '–') : '1'); nTd.title = multi ? `Repetitions with a value for this harness, of ${stats.n}.` : 'One repetition.';
      // Raw value first, the ratio to the lane's own control beside it.
      const timed = (v, k, d) => { const td = el('td', null, v != null ? fmt(v, d) : '–'); const q = m.ratio && m.ratio[k]; if (q != null && v != null) td.append(el('small', 'ratio', ratioText(q))); return td; };
      row.append(nTd, el('td', null, `${fmt(m.passed, multi ? 1 : 0)}/${m.n}`), failed, skipped, vh, el('td', null, m.per != null ? fmt(m.per, 1) : '–'), el('td', null, m.rss != null ? fmt(m.rss, 1) : '–'),
        timed(m.cpu, 'cpu', 2), el('td', null, m.disk != null ? mb(m.disk) : '–'), timed(m.start, 'start', 2),
        timed(m.gap, 'gap', 3), el('td', null, m.slope != null ? fmt(m.slope, 0) : '–'),
        el('td', null, m.mem != null ? mb(m.mem) : '–'), el('td', null, m.cpuPk != null ? `${fmt(m.cpuPk, 0)}%` : '–'),
        el('td', null, m.alive != null ? String(m.alive) : '–'), timed(m.wall, 'wall', 1), el('td', null, sc && sc.wins ? Rank.leadText(sc, field(rows)).replace('best on ', '') : '–'));
      if (m.wall != null && m.timeouts) row.children[16].append(el('small', 'ratio', `${m.timeouts} at limit`));
      const i = rows.findIndex((x) => x.r.h === r.h);
      [...row.children].forEach((td, c) => { if (colPos[c]) shadeOne(td, rows, i, colPos[c], HEAT_COLS[c][0], HEAT_COLS[c][2]); });
      tb.append(row);
      if (ctlOk(m.ctl)) {  // the lane's own control, dim: what the ratios divide by
        const c = m.ctl.median, cr = el('tr', 'control'), cell = (v, d) => el('td', null, v != null ? fmt(v, d) : '');
        cr.append(el('td', null, 'control (mini on this VM)'), el('td', null, ''), ...Array.from({ length: 5 }, () => el('td')), cell(c.rss_mb, 1), cell(c.cpu_s, 2), el('td'), cell(c.first_request_s, 3), cell(c.step_gap_s, 3),
          ...Array.from({ length: 4 }, () => el('td')), cell(c.wall_s, 1), el('td'));
        cr.title = `Mini run alone on this lane's VM at one agent, median of ${((m.ctl.values || {}).first_request_s || []).length || 3}, before this lane's run.`;
        tb.append(cr);
      }
    });
    t.replaceChildren(th, tb);
    $('#rule').textContent = !race || !rows.length ? '' : multi ? Rank.ruleText(stats) : race.repeats > 1 ? 'Series under way: strips and ties appear once two repetitions have finished. Until then shading uses the 10% gap rule.' : Rank.ruleText(null);
  }
  // First request weight: one stacked bar per harness, in bytes on a shared scale. Hidden when no lane reports it.
  function firstRequest(rows, lead) {
    const items = rows.map(({ r, m }) => ({ r, m, f: firstOf(r) })).sort((a, b) => (a.r.h === 'mini') - (b.r.h === 'mini'));
    const sec = $('#first-sec'), have = items.filter((x) => x.f && x.f.canonical_bytes != null);
    sec.hidden = !have.length;
    if (!have.length) return;
    const top = Math.max(...have.map((x) => x.f.canonical_bytes), 1), pos = lead.positions.first, nodes = [];
    $('#first').replaceChildren(...items.map(({ r, f }) => {
      const floor = r.h === 'mini', row = el('div', floor ? 'frow floor' : 'frow'), stack = el('div', 'stack'), segs = Rank.firstRequestSegments(f);
      row.append(el('span', 'n', floor ? `${r.name} · floor` : r.name));
      const v = el('span', 'v');
      if (!segs) { stack.append(el('div', 'dashed')); stack.style.display = 'block'; v.textContent = f && f.canonical_bytes != null ? `${fmt(f.canonical_bytes)} B · not split` : 'not measured'; }
      else segs.forEach((s) => {
        if (!s.bytes) return;
        const i = el('i', `s-${s.key}`); i.style.width = `${(100 * s.bytes) / top}%`; i.title = `${s.label}: ${fmt(s.bytes)} B`; stack.append(i);
      });
      if (segs) { v.append(`${fmt(f.canonical_bytes)} B · ≈ ${fmt(f.canonical_bytes / 4)} tokens`); v.append(el('small', null, f.tool_count != null ? ` · ${f.tool_count} tool${f.tool_count === 1 ? '' : 's'}` : '')); }
      v.title = 'Tokens are bytes divided by 4, an estimate.';
      row.append(stack, v);
      if (!floor && segs) { const k = rows.findIndex((x) => x.r.h === r.h); nodes.push([v, k]); }
      return row;
    }));
    nodes.forEach(([v, k]) => { if (pos) { const t = pos[k]; Heat.shade(v, t, Rank.compared(rows, k, pos, 'first', 0)); } });
  }
  async function history() {
    const r = await api('api/races'); if (!r.ok) return [];
    const races = r.body.races || [];
    // The superseded column shows only when the service says it for some row; nothing is invented here.
    const sup = (x) => x.superseded_by || x.superseded, anySup = races.some(sup);
    const t = $('#races'), th = el('thead'), tr = el('tr');
    ['started', 'task', 'agents per VM', 'reps', 'state', 'lanes', ...(anySup ? ['cohort'] : [])].forEach((x) => tr.append(el('th', null, x))); th.append(tr);
    const tb = el('tbody');
    if (!races.length) { const row = el('tr'), c = el('td', 'dim', 'No races yet.'); c.colSpan = 6; row.append(c); tb.append(row); }
    races.forEach((x) => {
      const row = el('tr');
      const lanes = Object.entries(x.lanes).filter(([, v]) => v.started).map(([k]) => (roster.find((y) => y.lane && y.lane.id === k) || { name: k }).name).join(', ');
      [x.created.replace('T', ' ').replace('Z', ' UTC'), (presets.find((p) => p.id === x.preset) || {}).title || x.preset, String(x.size), String(x.repeats || 1), x.state, lanes, ...(anySup ? [sup(x) ? 'superseded' : 'current'] : [])]
        .forEach((v) => row.append(el('td', null, v)));
      tb.append(row);
    });
    t.replaceChildren(th, tb);
    return races;
  }
  async function refreshLanes() {
    const r = await api('api/lanes'); if (!r.ok) { note('The race service is not reachable right now.', true); return; }
    const lanes = r.body.lanes || [];
    catalog = r.body.catalog || null;
    const next = ROSTER.map((x) => ({ ...x, lane: lanes.find((l) => l.harness === x.h) || null }));
    lanes.filter((l) => !ROSTER.some((x) => x.h === l.harness)).forEach((l) => next.push({ h: l.harness, name: l.label, build: 'test lane', lane: l }));
    const shape = (xs) => xs.map((x) => `${x.h}:${!!x.lane}`).join();
    const changed = shape(next) !== shape(roster);
    roster = next;
    if (picked) for (const id of [...picked]) if (!lanes.some((l) => l.id === id)) picked.delete(id);
    $('#notice').textContent = `scripted model · no tokens spent${r.body.notice ? ` · ${r.body.notice}` : ''}`;
    if (changed || !$('#lanes').children.length) build(); else roster.forEach(paint);
    receipt(); renderControls();
  }

  $('#run').onclick = async () => {
    $('#run').disabled = true; note('');
    const lanes = connected().filter((r) => picked.has(r.lane.id) && offers(r)).map((r) => r.lane.id);
    const p = presets.find((x) => x.id === preset);
    if (p && p.kind === 'harness' && lanes.length >= 2) note('Measuring each VM…');
    const launch = { preset, size, lanes, ...(repeats > 1 ? { repeats } : {}), ...(model === 'scripted' && repeats === 10 ? { passkey: $('#passkey').value } : {}), ...(protocol && protocol !== 'naive' ? { protocol } : {}), ...(model !== 'scripted' ? { model, ...(effort ? { effort } : {}), passkey: $('#passkey').value } : {}) };
    const r = await api('api/races', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(launch) });
    if (r.status === 202) { attach(r.body, true); return; }
    if (r.status === 409 && r.body.race) { note('A race is already running on one of those lanes. Watching it.'); const g = await api(`api/races/${r.body.race}`); if (g.ok) attach(g.body, true); return; }
    renderControls();  // first: it resets the note
    note(r.status === 403 ? (r.body.error || 'Real models are operator-only.') : r.status === 429 ? 'You have used this hour’s launches. Watch the next one, or come back later.' : (r.body.error || 'Could not launch.'), true);
  };
  let resizeTimer = null;
  const refit = () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(() => { layout(); fitAll(); charts(); }, 60); };
  // The launch bar changes height as chips and presets load or wrap; the board takes whatever is left.
  if ('ResizeObserver' in window) new ResizeObserver(refit).observe($('.panel'));
  addEventListener('resize', refit);

  (async () => {
    const p = await api('api/presets');
    presets = p.ok ? p.body.presets || [] : [];
    preset = presets[0] ? presets[0].id : null;
    await refreshLanes();
    size = preset ? fitSize(presets[0].sizes || []) : null; renderControls();
    const races = await history();
    if (races[0]) attach(races[0], races[0].state === 'running');
    setInterval(() => { if (!source) refreshLanes(); }, 5000);
  })();
})();
