// /swarm: launch a live swarm of coding agents (Ante, Codex CLI or Claude Code) on a Swarm lane and watch it run.
// Talks to `swarm serve` through the same-origin proxy at /swarm/live/api (nginx adds the ingress secret).
// Everything from the API is written with textContent; nothing is parsed as HTML.
'use strict';

const API = '/swarm/live/api';
const $ = s => document.querySelector(s);
const el = (tag, cls, text) => { const e = document.createElement(tag); if (cls) e.className = cls; if (text != null) e.textContent = text; return e; };
const fmt = n => Number.isFinite(n) ? n.toLocaleString('en-US') : '—';
const fixed = (n, digits) => Number.isFinite(n) ? n.toFixed(digits) : '—';
const rounded = n => Number.isFinite(n) ? fmt(Math.round(n)) : '—';
const reduced = matchMedia('(prefers-reduced-motion: reduce)');

const HARNESSES = ['ante', 'codex', 'claude-code'];
const HNAME = { ante: 'Ante', codex: 'Codex CLI', 'claude-code': 'Claude Code' };
const hname = h => HNAME[h] || h || 'Ante';

// Shown when the run service can't be reached, so the page still explains itself.
const OFFLINE_PRESETS = [
  { id: 'fleet-fix', title: 'Fix a fleet of CLIs', blurb: 'Every agent gets its own copy of a small CLI, adds input validation and proves it with hidden tests.', tasks: ['cli-validate'], sizes: [1, 8, 32, 128, 256], harnesses: HARNESSES },
  { id: 'nightly-reports', title: 'Nightly reports', blurb: 'One agent per region turns raw sales into a totals report, checked against the expected numbers.', tasks: ['csv-totals'], sizes: [1, 8, 32, 128, 256], harnesses: HARNESSES },
  { id: 'log-triage', title: 'Triage error logs', blurb: 'Agents read service logs in parallel and count errors per service, one log each.', tasks: ['log-triage'], sizes: [1, 8, 32, 128, 256], harnesses: HARNESSES },
  { id: 'config-migrate', title: 'Migrate configs', blurb: 'A config change across many repos at once: every agent rewrites its service configs to the new format.', tasks: ['config-migrate'], sizes: [1, 8, 32, 128, 256], harnesses: HARNESSES },
  { id: 'mixed', title: 'All of it at once', blurb: 'The four jobs mixed across one swarm, the way a real backlog looks.', tasks: ['cli-validate', 'csv-totals', 'log-triage', 'config-migrate'], sizes: [4, 32, 128, 256], harnesses: HARNESSES },
];

let presets = [], preset = null, size = null, harness = 'ante', online = false, busy = false, maint = null;
let run = null; // {id, size, admitted, done: Map(slot -> attempt), sample, state, rec}
let source = null;

// Passkey: raises this visitor's hourly cap. Kept for the tab only; the server checks it.
const store = { get() { try { return sessionStorage.getItem('swarm-passkey') || ''; } catch { return ''; } },
  set(v) { try { v ? sessionStorage.setItem('swarm-passkey', v) : sessionStorage.removeItem('swarm-passkey'); } catch { /* private mode */ } } };
let passkey = store.get();
function showPasskey(open) { $('#passkey').hidden = !open; $('#passkey-link').hidden = open || !!passkey; if (open) $('#passkey-input').focus(); }

async function api(path, opts) {
  const res = await fetch(API + path, { cache: 'no-store', ...opts });
  let body = null;
  try { body = await res.json(); } catch { /* empty */ }
  return { ok: res.ok, status: res.status, body: body || {} };
}

// ---- 1 · pick a job ----
const JOBS = {
  'fleet-fix': ['Fix a CLI', 'Traceback', 'Clean error'],
  'nightly-reports': ['Build reports', 'sales.csv', 'totals.csv'],
  'log-triage': ['Triage logs', 'app.log', 'errors.json'],
  'config-migrate': ['Migrate configs', 'timeout: 2', 'timeout_ms: 2000'],
  mixed: ['Mix the jobs', 'Four jobs', 'One swarm'],
};
function jobPreview(id) {
  const data = JOBS[id] || ['Local task', 'Input', 'Checked output'];
  const art = el('span', `job-preview job-${id}`); art.setAttribute('aria-hidden', 'true');
  const before = el('span', 'preview-before'), after = el('span', 'preview-after');
  if (id === 'nightly-reports') {
    for (const line of ['region, sales', 'north, 120', 'south, 80']) before.append(el('i', null, line));
    const bars = el('span', 'preview-bars'); for (const height of [65, 40]) { const bar = el('i'); bar.style.height = height + '%'; bars.append(bar); } after.append(bars);
  } else if (id === 'log-triage') {
    for (const line of ['INFO  api', 'ERROR api', 'ERROR jobs']) before.append(el('i', line.startsWith('ERROR') ? 'error-line' : '', line));
    for (const line of ['api    1', 'jobs   1']) after.append(el('i', null, line));
  } else if (id === 'mixed') {
    for (const kind of ['code', 'report', 'logs', 'config']) before.append(el('i', 'job-token', kind));
    for (let i = 0; i < 12; i++) after.append(el('i', 'preview-cell'));
  } else {
    before.append(el('i', null, id === 'fleet-fix' ? 'ValueError' : 'timeout'), ...(id === 'config-migrate' ? [el('i', null, '2s')] : []));
    after.append(el('i', null, id === 'fleet-fix' ? 'exit 2 ✓' : 'timeout_ms'), ...(id === 'config-migrate' ? [el('i', null, '2000')] : []));
  }
  art.append(before, el('span', 'preview-arrow', '→'), after);
  return art;
}
function renderPresets() {
  const box = $('#presets'); box.replaceChildren();
  for (const p of presets) {
    const b = el('button', 'preset'); b.type = 'button';
    b.setAttribute('role', 'radio'); b.setAttribute('aria-label', `${p.title}. ${p.blurb}`);
    b.setAttribute('aria-checked', String(p === preset)); b.tabIndex = p === preset ? 0 : -1;
    b.append(jobPreview(p.id), el('b', null, JOBS[p.id]?.[0] || p.title), el('span', 'job-example', 'Example'));
    b.addEventListener('click', () => choose(p));
    b.addEventListener('keydown', e => arrows(e, presets, p, choose, '#presets .preset'));
    box.append(b);
  }
  $('#job-caption').textContent = preset?.blurb || '';
}

function renderSizes() {
  const box = $('#sizes');
  box.replaceChildren();
  const index = preset.sizes.indexOf(size), slider = $('#size-range');
  slider.max = preset.sizes.length - 1; slider.value = index;
  slider.setAttribute('aria-valuetext', `${size} agents`);
  $('#size-value').textContent = fmt(size);
  $('#size-number').value = size; $('#size-number').min = preset.sizes[0]; $('#size-number').max = preset.sizes.at(-1);
  drawSizePreview();
  for (const n of preset.sizes) {
    const b = el('button', null, fmt(n));
    b.type = 'button';
    b.setAttribute('role', 'radio');
    b.setAttribute('aria-checked', String(n === size));
    b.setAttribute('aria-label', `${n} agent${n === 1 ? '' : 's'}`);
    b.tabIndex = n === size ? 0 : -1;
    b.addEventListener('click', () => { size = n; renderSizes(); updateGo(); });
    b.addEventListener('keydown', e => arrows(e, preset.sizes, n, v => { size = v; renderSizes(); updateGo(); }, '#sizes button'));
    box.append(b);
  }
}

function drawSizePreview() {
  const c = $('#size-preview'), w = Math.max(100, c.parentElement.clientWidth), dpr = Math.min(2, devicePixelRatio || 1);
  c.width = w * dpr; c.height = 42 * dpr; c.style.height = '42px';
  const context = c.getContext('2d'); context.scale(dpr, dpr);
  const cols = Math.max(1, Math.floor(w / 8)), rows = Math.ceil(size / cols), cell = Math.max(1, Math.min(6, 40 / rows - 2));
  context.fillStyle = ink('--orange');
  for (let i = 0; i < size; i++) context.fillRect((i % cols) * (cell + 2), Math.floor(i / cols) * (cell + 2), cell, cell);
}
function setSize(n) { size = n; renderSizes(); updateGo(); }
$('#size-range').addEventListener('input', e => setSize(preset.sizes[Number(e.target.value)]));
$('#size-number').addEventListener('change', e => {
  const n = Number(e.target.value);
  e.target.setCustomValidity(preset.sizes.includes(n) ? '' : `Choose ${preset.sizes.join(', ')} agents.`);
  if (!e.target.reportValidity()) return;
  setSize(n);
});
$('#size-number').addEventListener('input', e => e.target.setCustomValidity(''));

function renderHarnesses() {
  const box = $('#harnesses'), list = preset.harnesses?.length ? preset.harnesses : ['ante'];
  if (!list.includes(harness)) harness = list[0];
  $('#harness-box').hidden = list.length < 2;
  box.replaceChildren();
  for (const hv of list) {
    const b = el('button', null, hname(hv));
    b.type = 'button';
    b.setAttribute('role', 'radio');
    b.setAttribute('aria-checked', String(hv === harness));
    b.tabIndex = hv === harness ? 0 : -1;
    const pick = v => { harness = v; renderHarnesses(); updateGo(); };
    b.addEventListener('click', () => pick(hv));
    b.addEventListener('keydown', e => arrows(e, list, hv, pick, '#harnesses button'));
    box.append(b);
  }
}

// Radio-group keyboard behaviour: arrows move the choice and the focus.
function arrows(e, list, current, pick, selector) {
  const step = { ArrowRight: 1, ArrowDown: 1, ArrowLeft: -1, ArrowUp: -1 }[e.key];
  if (!step) return;
  e.preventDefault();
  const i = (list.indexOf(current) + step + list.length) % list.length;
  pick(list[i]);
  document.querySelectorAll(selector)[i]?.focus();
}

function choose(p) {
  preset = p;
  if (!p.sizes.includes(size)) size = p.sizes.includes(32) ? 32 : p.sizes[Math.min(1, p.sizes.length - 1)];
  renderPresets(); renderSizes(); renderHarnesses(); updateGo();
}

function updateGo() {
  const go = $('#go');
  go.disabled = !online || busy || !!maint || !preset || !size;
  $('.go-label').textContent = maint ? 'Benchmark running' : busy ? 'Swarm running…' : size ? `Launch ${fmt(size)} ${hname(harness)} agent${size === 1 ? '' : 's'}` : 'Launch swarm';
  updateChoice();
}

function updateChoice() {
  const saved = !$('#setup').open && run;
  $('#setup-choice').textContent = saved ? `${run.title} · ${fmt(run.size)} agents · ${hname(run.harness)}`
    : preset ? `${preset.title} · ${fmt(size)} agents · ${hname(harness)}` : '';
}
$('#setup').addEventListener('toggle', updateChoice);

function note(text, err) { const n = $('#launch-note'); n.textContent = text || ''; n.classList.toggle('err', !!err); }

async function launch() {
  if (!preset || !size || busy || maint || !online || !$('#size-number').checkValidity()) return;
  busy = true; updateGo(); note('');
  try {
    const r = await api('/runs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ preset: preset.id, size, harness, ...(passkey ? { passkey } : {}) }) });
    if (r.status === 202) {
      history.replaceState(null, '', `?run=${r.body.id}#watch`);
      follow(r.body.id, preset.title, size, harness);
      $('#watch').scrollIntoView({ behavior: reduced.matches ? 'instant' : 'smooth', block: 'start' });
      return;
    }
    busy = false; updateGo();
    const wait = `${Math.max(1, Math.ceil((r.body.retry_after_s || 600) / 60))} min`;
    if (r.status === 403 && r.body.error === 'wrong passkey') { passkey = ''; store.set(''); note("That passkey didn't work.", true); showPasskey(true); }
    else if (r.status === 429 && r.body.error === 'too many passkey attempts') { passkey = ''; store.set(''); showPasskey(false); note(`Too many passkey tries. Try again in about ${wait}.`, true); }
    else if (r.status === 429 && r.body.passkey) { note(`You've used this hour's launches. Enter a passkey for 5 more, or try again in about ${wait}.`, true); showPasskey(true); }
    else if (r.status === 429) note(`You've used this hour's launches. Try again in about ${wait}.`, true);
    else if (r.status === 503 && r.body.error === 'benchmark in progress') { maint = { minutes: r.body.minutes ?? null }; showMaintenance(); }
    else if (r.status === 503) note('The queue is full right now. Try again in a minute.', true);
    else note(`Couldn't launch: ${r.body.error || `HTTP ${r.status}`}.`, true);
  } catch {
    busy = false; updateGo();
    note("Couldn't reach the swarm machine. Try again shortly.", true);
  }
}

async function pollStatus() {
  try {
    const r = await api('/status');
    if (!r.ok) throw Error();
    const q = r.body.queued || 0, cur = r.body.running, m = r.body.maintenance;
    const was = !!maint;
    maint = m ? { minutes: m.minutes ?? null } : null;
    if (maint) showMaintenance(); else if (was) { note(''); updateGo(); }
    if (!maint) $('#queue').textContent = cur ? `Running: ${cur.title} × ${fmt(cur.size)}${cur.harness ? ' · ' + hname(cur.harness) : ''}${q ? ` · ${q} queued` : ''}` : q ? `${q} queued` : 'Machine idle · ready';
  } catch { $('#queue').textContent = online ? '' : 'Live runs offline'; }
}

// The machine is lent to a benchmark: launching is off until it ends.
function showMaintenance() {
  const m = maint?.minutes;
  note(`The machine is running a benchmark; back in about ${m ? `${m} min` : 'a few minutes'}. Launching is off until it ends.`, true);
  $('#queue').textContent = 'Benchmark running';
  updateGo();
}

// ---- 2 · watch it run ----
function follow(id, title, n, h) {
  if (source) source.close();
  run = { id, title, size: n, harness: h || harness, admitted: 0, done: new Map(), sample: {}, state: 'queued', rec: null, popped: new Map(), agents: new Map(), requests: new Map(), connected: false, lastReceived: performance.now(), progress: null };
  document.body.dataset.phase = 'running';
  $('#setup').open = false;
  updateChoice();
  $('#watch').hidden = false;
  $('#receipt').hidden = true;
  $('#watch-label').textContent = 'Watch it run';
  $('#diagnostics').open = false;
  selectPanel('activity');
  $('#agent-choice').replaceChildren(el('option', null, 'Choose an agent'));
  $('#agent-choice').firstChild.value = '';
  for (let slot = 0; slot < n; slot++) {
    const option = el('option', null, `Agent ${tag(slot)}`); option.value = slot; $('#agent-choice').append(option);
  }
  $('#field-empty').hidden = true;
  resetConsole(id, title, n, run.harness);
  $('#receipt-body').replaceChildren(el('p', 'muted', 'Waiting for the wave to finish…'));
  resizeField();
  $('#failure-list').replaceChildren();
  source = new EventSource(`${API}/runs/${id}/events`);
  source.onmessage = e => { if (run?.id !== id) return; try { update(JSON.parse(e.data)); } catch { run.connected = false; renderHealth(); } };
  source.onerror = () => { if (run?.id !== id) return; run.connected = false; renderHealth(); if (run.state === 'done' || run.state === 'failed') source.close(); };
  tick();
}

function update(snap) {
  if (!run || snap.run?.id !== run.id) return;
  const rec = snap.run;
  run.connected = true; run.lastReceived = performance.now(); run.progress = snap.progress || null;
  if (snap.progress?.agents) run.agents = new Map(snap.progress.agents.map(a => [a.slot, a]));
  run.state = rec.state; run.rec = rec; run.size = rec.size; run.title = rec.title; run.harness = rec.harness || run.harness || 'ante';
  updateChoice();
  run.admitted = Math.max(run.admitted, snap.admitted || 0);
  if (snap.sample && Object.keys(snap.sample).length) run.sample = snap.sample;
  const now = performance.now();
  for (const a of snap.attempts || []) {
    const key = a.slot ?? run.done.size;
    if (!run.done.has(key)) { run.done.set(key, a); run.popped.set(key, now); }
  }
  if (snap.console?.length) {
    for (const e of snap.console) if (e.k === 'request_error') run.requests.set(e.slot, e);
    consoleLines(snap.console);
  }
  counters(); renderFailures(); renderHealth();
  const count = Number.isFinite(snap.finished) ? snap.finished : run.done.size;
  $('#completion').setAttribute('aria-valuenow', String(Math.round(100 * count / run.size)));
  $('#completion-fill').style.width = `${100 * count / run.size}%`;
  $('#completion-label').textContent = `${fmt(count)} / ${fmt(run.size)} finished`;
  if (run.agents.size) for (const option of $('#agent-choice').options) if (option.value !== '') option.textContent = `Agent ${tag(Number(option.value))} · ${run.agents.get(Number(option.value))?.state || 'unknown'}`;
  const s = $('#state');
  s.classList.toggle('live', rec.state === 'running');
  const who = hname(run.harness);
  s.textContent = rec.state === 'queued' ? `${run.title} × ${fmt(run.size)} · ${who} · queued${snap.position ? `, position ${snap.position}` : ''}`
    : rec.state === 'running' ? `${run.title} × ${fmt(run.size)} · ${who} · running`
    : rec.state === 'done' ? `${run.title} × ${fmt(run.size)} · ${who} · finished` : `${run.title} · ${who} · stopped`;
  if (rec.state === 'done' || rec.state === 'failed') {
    document.body.dataset.phase = 'complete';
    $('#watch-label').textContent = rec.state === 'done' ? rec.complete === false ? 'Run incomplete' : 'Run complete' : 'Run stopped';
    $('#receipt').hidden = false;
    source?.close();
    busy = false; updateGo(); receipt(rec); pollStatus();
    $('#term-dot').className = 'term-dot ' + (rec.state === 'done' ? 'done' : '');
    loadSpec(run.id);
    if (inspecting == null && run.done.size) inspect([...run.done.values()].find(a => !a.passed)?.slot ?? Math.min(...run.done.keys()), false);
  }
}

function duration(seconds) {
  if (!Number.isFinite(seconds)) return '—';
  const s = Math.max(0, Math.floor(seconds));
  return s < 60 ? `${s}s` : `${Math.floor(s / 60)}m ${s % 60}s`;
}
function failureName(a) { return a.failure_detail?.title || a.failure?.replace(/_/g, ' ') || 'Attempt failed'; }
function renderFailures() {
  const box = $('#failure-list'), failed = [...run.done.values()].filter(a => !a.passed);
  const requestErrors = [...run.requests].filter(([slot]) => !run.done.has(slot));
  const exited = [...run.agents.values()].filter(a => a.observed_exit?.exit_code != null && a.observed_exit.exit_code !== 0 && !run.done.has(a.slot));
  box.replaceChildren(); box.hidden = !failed.length && !requestErrors.length && !exited.length;
  if (box.hidden) return;
  box.append(el('h3', null, failed.length ? `${failed.length} failed · run still in progress` : exited.length ? 'Agent exited with an error · verification pending' : 'Request rejected · attempt still in progress'));
  if (['done', 'failed'].includes(run.state)) box.firstChild.textContent = `${failed.length} failed · evidence retained`;
  const list = el('div', 'failure-buttons');
  const entries = [...failed.map(a => [a.slot, failureName(a)]), ...requestErrors.map(([slot, e]) => [slot, `HTTP ${e.status || '—'} · ${e.text}`]), ...exited.map(a => [a.slot, `Exit ${a.observed_exit.exit_code} · checking`])];
  let buttons = list;
  for (const [index, [slot, reason]] of entries.entries()) {
    if (index === 8) { const more = el('details', 'failure-more'); more.append(el('summary', null, `Show all ${entries.length} affected agents`)); buttons = el('div'); more.append(buttons); list.append(more); }
    const button = el('button', null, `${tag(slot)} · ${reason}`); button.type = 'button';
    button.addEventListener('click', () => inspect(slot)); buttons.append(button);
  }
  box.append(list);
}
function renderHealth() {
  if (!run) return;
  const box = $('#run-health');
  if (!box.firstChild) {
    const title = el('strong'); title.id = 'health-title'; title.setAttribute('role', 'status');
    title.setAttribute('aria-live', 'polite');
    const meta = el('span'); meta.id = 'health-meta'; meta.setAttribute('aria-live', 'off');
    const detail = el('p'); detail.id = 'health-detail';
    box.append(title, meta, detail);
  }
  const p = run.progress, since = Math.max(0, (performance.now() - run.lastReceived) / 1000);
  const finished = ['done', 'failed'].includes(run.state);
  const elapsed = p?.elapsed_s == null ? null : p.elapsed_s + (finished ? 0 : since);
  const quiet = [...run.agents.values()].filter(a => ['active', 'checking'].includes(a.state) && a.quiet_s != null && a.quiet_s + since >= (p?.quiet_warning_s || 30));
  const waiting = [...run.agents.values()].filter(a => a.state === 'waiting').length;
  const failed = [...run.done.values()].filter(a => !a.passed).length;
  let title = 'Receiving updates', detail = '', tone = 'good';
  if (run.state === 'failed') { title = 'Run stopped'; detail = run.rec?.error || 'The service did not finish this run. Partial attempts are retained.'; tone = 'error'; }
  else if (finished && run.rec?.complete === false) { title = 'Run incomplete'; detail = 'Not all requested attempts were recorded. The receipt covers the attempts retained.'; tone = 'warning'; }
  else if (finished) { title = failed ? 'Finished with failures' : 'Run finished'; tone = failed ? 'error' : 'good'; }
  else if (!run.rec && since < 8) { title = 'Connecting to the run'; detail = 'Waiting for its first observation.'; }
  else if (!run.connected || since >= 8) { title = run.unavailable ? 'Run no longer available' : 'Updates interrupted'; detail = `Last update ${duration(since)} ago. Reconnecting; recorded results stay visible.`; tone = 'warning'; }
  else if (run.state === 'queued') { title = p?.maintenance ? 'Paused for machine maintenance' : 'In the queue'; detail = p?.maintenance ? 'The run will wait until maintenance ends.' : 'No agents have started. This run is waiting for the machine.'; }
  else if (p?.sample_age_s != null && p.sample_age_s + since > 15) { title = 'Machine observations are stale'; detail = 'The service is reachable, but its machine sample is old. Process state is unconfirmed.'; tone = 'warning'; }
  else if ([...run.agents.values()].some(a => a.observed_exit?.exit_code != null && a.observed_exit.exit_code !== 0)) { title = 'Agent exited with an error'; detail = 'Verification is still pending. Open the affected agent for its exit status.'; tone = 'error'; }
  else if (quiet.length || (p && p.last_activity_age_s == null && elapsed >= 30)) {
    title = quiet.length ? `${quiet.length} quiet agent${quiet.length === 1 ? '' : 's'}` : 'No agent activity recorded yet';
    detail = quiet.length ? `No recorded activity for at least ${duration(Math.min(...quiet.map(a => a.quiet_s)) + since)}. Quiet output does not prove a hang.` : 'The run is reporting, but no agent output has been recorded. This does not prove a hang.';
    if (p.agent_limit_s != null) detail += ` Agent execution limit: ${duration(p.agent_limit_s)}.`;
    tone = 'warning';
  }
  else if (failed || run.requests.size) { title = failed ? `${failed} attempt${failed === 1 ? '' : 's'} failed` : 'A request was rejected'; detail = 'Open a failed agent to see its recorded reason. Other attempts continue.'; tone = 'error'; }
  else if (waiting) { title = `${waiting} waiting for admission`; detail = p?.admission_limit_s != null ? `Admission wait limit: ${duration(p.admission_limit_s)}. No start is assumed until observed.` : 'Agents will enter as the machine admits them.'; }
  const set = (id, text) => { if ($(id).textContent !== text) $(id).textContent = text; };
  set('#health-title', title); set('#health-detail', detail);
  set('#health-meta', `${duration(elapsed)} elapsed · ${finished ? 'saved result' : run.connected && since < 8 ? 'connected' : 'reconnecting'}${waiting ? ` · ${waiting} waiting` : ''}`);
  box.dataset.tone = tone;
}
let pollInFlight = false, lastPoll = 0;
setInterval(async () => {
  if (!run) return;
  renderHealth();
  if (['done', 'failed'].includes(run.state) || run.unavailable || pollInFlight || performance.now() - run.lastReceived < 8000 || performance.now() - lastPoll < 5000) return;
  pollInFlight = true; lastPoll = performance.now();
  const id = run.id;
  try {
    const response = await api(`/runs/${id}`);
    if (run?.id !== id) return;
    if (response.ok) update(response.body);
    else if (response.status === 404) { run.unavailable = true; source?.close(); }
  } catch { /* retain the last observations while reconnecting */ }
  finally { pollInFlight = false; }
}, 1000);

function counters() {
  const finished = run.done.size;
  const passed = [...run.done.values()].filter(a => a.passed).length;
  const alive = run.state === 'running' ? (run.agents.size ? [...run.agents.values()].filter(a => ['active', 'checking'].includes(a.state)).length : Math.max(0, run.admitted - [...run.done.values()].filter(a => a.failure !== 'admission_timeout').length)) : 0;
  const adm = $('#c-admitted'); adm.textContent = fmt(run.admitted); adm.append(el('small', null, ` / ${fmt(run.size)}`));
  $('#c-running').textContent = fmt(alive);
  $('#c-passed').textContent = fmt(passed);
  $('#c-failed').textContent = fmt(finished - passed);
  const mb = run.sample.available_mb;
  $('#c-mem').textContent = Number.isFinite(mb) ? `${(mb / 1024).toFixed(1)} GB` : '–';
  $('#c-pressure').textContent = run.sample.pressure || '–';
  $('#c-pressure').dataset.pressure = run.sample.pressure || 'unknown';
  const total = run.progress?.memory_total_mb;
  $('#memory-meter').hidden = !Number.isFinite(mb) || !Number.isFinite(total) || total <= 0;
  if (!$('#memory-meter').hidden) { $('#memory-meter').max = total; $('#memory-meter').value = mb; $('#memory-meter').setAttribute('aria-label', `${fmt(mb)} MB free of ${fmt(total)} MB`); }
}

// ---- the console: Ante's own log, or the scripted model's request log for Codex CLI and Claude Code ----
let inspecting = null, filter = null, lastT = 0;
const MAX_LINES = 3000;
const ts = t => { const m = Math.floor(t / 60), sec = (t % 60).toFixed(2).padStart(5, '0'); return `${String(m).padStart(2, '0')}:${sec}`; };
const tag = slot => `#${String(slot).padStart(3, '0')}`;

function resetConsole(id, title, n, h) {
  inspecting = null; filter = null; lastT = 0;
  $('#term-body').replaceChildren(el('li', 'term-hint', 'Waiting for the first agent to be admitted…'));
  $('#term-title').textContent = `swarm serve · run ${id} · ${title} × ${fmt(n)} · ${hname(h)}`;
  $('#term-dot').className = 'term-dot live';
  $('#term-filter').hidden = true;
  $('#inspect').replaceChildren(el('p', 'inspect-empty', 'Click any square, or an agent number in the console, to see exactly what that agent ran: its command, prompt, every tool call with its output, and the hidden test that checked it.'));
  $('#spec').hidden = true;
}

function line(t, slot, kind, kindCls, rest) {
  const li = el('li');
  li.dataset.slot = slot;
  const who = el('button', 'who', tag(slot)); who.type = 'button';
  who.addEventListener('click', () => inspect(slot));
  li.append(el('span', 'ts', ts(t)), who, el('span', kindCls, kind), rest);
  if (filter != null && filter !== slot) li.hidden = true;
  return li;
}

function append(items) {
  const body = $('#term-body');
  body.querySelector('.term-hint')?.remove();
  const frag = document.createDocumentFragment();
  items.forEach(i => frag.append(i));
  body.append(frag);
  while (body.children.length > MAX_LINES) body.firstChild.remove();
  if ($('#term-follow').checked) body.scrollTop = body.scrollHeight;
}

function consoleLines(events) {
  append(events.map(e => {
    lastT = Math.max(lastT, e.t || 0);
    if (e.k === 'admit') return line(e.t, e.slot, 'admit', 'k admit', el('span', 'admit', `${Number.isFinite(e.mb) ? (e.mb / 1024).toFixed(1) + ' GB free' : ''} · ${e.p || '–'} · waited ${Number(e.w || 0).toFixed(2)} s`));
    if (e.k === 'tool') return line(e.t, e.slot, e.tool || 'tool', 'tool', el('span', 'arg', e.arg || ''));
    if (e.k === 'request_error') return line(e.t, e.slot, 'REQUEST', 'fail', el('span', 'fail', `${e.status ? 'HTTP ' + e.status + ' · ' : ''}${e.text}`));
    if (e.k === 'toolerr') return line(e.t, e.slot, e.tool || 'tool', 'fail', el('span', 'fail', e.arg || 'failed'));
    if (e.k === 'result') return line(e.t, e.slot, e.ok ? 'PASS' : 'FAIL', e.ok ? 'pass' : 'fail', el('span', e.ok ? 'pass' : 'fail',
      e.ok ? `✓ hidden tests passed · ${fixed(e.wall, 2)} s · ${fmt(e.mb)} MB peak` : `✕ ${(e.why || 'failed').replace(/_/g, ' ')}`));
    return line(e.t, e.slot, 'reply', 'k', el('span', 'msg', e.text || ''));
  }));
}

function setFilter(slot) {
  filter = slot;
  const f = $('#term-filter');
  f.hidden = slot == null;
  f.textContent = slot == null ? '' : `${tag(slot)} ×`; f.setAttribute('aria-label', slot == null ? '' : `Show all agents (now only ${tag(slot)})`);
  for (const li of $('#term-body').children) if (li.dataset.slot) li.hidden = slot != null && Number(li.dataset.slot) !== slot;
}
$('#term-filter').addEventListener('click', () => setFilter(null));

function selectPanel(panel) {
  $('.deck').dataset.panel = panel;
  for (const name of ['activity', 'agent']) {
    const tab = $(`#${name}-tab`), selected = name === panel;
    tab.setAttribute('aria-selected', String(selected)); tab.tabIndex = selected ? 0 : -1;
  }
}
for (const name of ['activity', 'agent']) {
  const tab = $(`#${name}-tab`);
  tab.addEventListener('click', () => selectPanel(name));
  tab.addEventListener('keydown', e => {
    if (!['ArrowLeft', 'ArrowRight', 'Home', 'End'].includes(e.key)) return;
    e.preventDefault();
    const next = e.key === 'Home' ? 'activity' : e.key === 'End' ? 'agent' : name === 'activity' ? 'agent' : 'activity';
    selectPanel(next); $(`#${next}-tab`).focus();
  });
}
$('#agent-choice').addEventListener('change', e => { if (e.target.value !== '') inspect(Number(e.target.value)); });
$('#diagnostics').addEventListener('toggle', () => { if (!$('#diagnostics').open) $('#agent-choice').value = ''; });

// ---- the inspector: one agent, everything it did ----
async function inspect(slot, narrow = true) {
  if (!run) return;
  if (narrow) { $('#diagnostics').open = true; selectPanel('agent'); }
  if (narrow) $('#agent-choice').value = slot;
  inspecting = slot; if (narrow) setFilter(slot);
  const box = $('#inspect');
  box.replaceChildren(el('p', 'inspect-empty', `Loading agent ${tag(slot)}…`));
  let t;
  try { const r = await api(`/runs/${run.id}/agents/${slot}`); if (!r.ok) throw Error(); t = r.body; } catch {
    box.replaceChildren(el('p', 'inspect-empty', `Agent ${tag(slot)} hasn't started yet.`)); return;
  }
  if (inspecting !== slot) return;
  const h = el('h3', null, `Agent ${tag(slot)}`);
  if (t.task) h.append(el('span', 'chip', t.task));
  h.append(el('span', 'chip', hname(t.harness)));
  h.append(el('span', t.state !== 'done' ? 'badge run' : t.passed ? 'badge ok' : 'badge no', t.state !== 'done' ? t.lifecycle?.phase === 'agent_exited' ? 'checking' : 'running' : t.passed ? 'passed' : 'failed'));
  const refresh = el('button', 'refresh-agent', 'Refresh'); refresh.type = 'button'; refresh.setAttribute('aria-label', 'Refresh agent details');
  refresh.addEventListener('click', () => inspect(slot, false)); h.append(refresh);
  const stats = el('p', 'stats');
  const stat = (k, v) => { const sp = el('span', null, `${k} `); sp.append(el('b', null, v)); stats.append(sp); };
  if (t.agent?.wall_s != null) stat('wall', `${Number(t.agent.wall_s).toFixed(2)} s`);
  stat('peak', `${fmt(t.agent?.rss_mb)} MB`);
  if (t.agent?.cpu_s != null) stat('cpu', `${Number(t.agent.cpu_s).toFixed(2)} s`);
  if (t.requests != null) stat('model calls', String(t.requests));
  const parts = [h, stats];
  const earlyExit = t.lifecycle?.phase === 'agent_exited' && t.state !== 'done' ? t.lifecycle.agent : null;
  if (earlyExit) {
    const observed = el('div', 'failure-evidence');
    observed.append(el('h4', null, earlyExit.exit_code === 0 ? 'Agent exited · verification pending' : 'Agent exited with an error · verification pending'),
      el('p', null, `Recorded exit: ${earlyExit.exit_code ?? 'unknown'} · ${earlyExit.termination || 'unknown'}. This is not the final attempt verdict.`));
    parts.push(observed);
  }
  if (t.failure_detail || t.request_errors?.length) {
    const f = t.failure_detail, card = el('div', 'failure-evidence');
    card.append(el('h4', null, f?.title || 'Requests rejected'), el('p', null, f?.message || 'The scripted endpoint rejected a request. The attempt may still be running.'));
    const facts = el('dl', 'failure-facts');
    const add = (k, v) => { const row = el('div'); row.append(el('dt', null, k), el('dd', null, v)); facts.append(row); };
    if (f) {
      add('Recorded class', f.code); add('Exit status', f.exit_code == null ? 'Not recorded' : f.exit_code < 0 ? `Signal ${-f.exit_code} (exit ${f.exit_code})` : String(f.exit_code));
      add('Termination', f.termination || 'Not recorded');
      add('Requests completed', `${fmt(f.completed_requests)} / ${fmt(f.requests)}`);
      if (f.admission_wait_s != null) add('Admission wait', duration(f.admission_wait_s));
      if (f.held_for?.length) add('Admission reasons', f.held_for.join(', '));
      add('Hidden test exit', f.check_exit == null ? 'No check result recorded' : String(f.check_exit));
      if (f.request_errors?.length) add('Request errors', f.request_errors.join(', '));
    }
    for (const e of t.request_errors || []) add(`HTTP ${e.status || '—'}`, e.error);
    card.append(facts); parts.push(card);
  }
  const technical = el('details', 'technical');
  technical.append(el('summary', null, 'Command and prompt'), el('pre', null, '$ ' + t.command.join(' ')), el('p', 'prompt', t.prompt || '–'));
  parts.push(el('h4', null, 'Activity timeline'));

  const steps = el('ol', 'steps');
  for (const st of t.steps) {
    const d = el('details'), sum = el('summary');
    const served = st.status === 'Served', ok = served || st.status == null ? null : st.status === 'Completed';
    const native = st.tool || '', label = /read|cat/i.test(native + ' ' + st.arg) ? 'Read' : /write|edit/i.test(native) ? 'Write' : 'Command';
    sum.append(el('span', 'tool', label), el('span', 'arg', st.arg || native), el('span', ok === false ? 'st no' : 'st', served ? 'served →' : ok == null ? '…' : ok ? '✓' : '✕'));
    const body = el('div', 'body');
    const args = Object.entries(st.args || {}).filter(([k]) => k !== 'description');
    if (args.length) { body.append(el('span', null, 'input')); body.append(el('pre', null, args.map(([k, v]) => args.length === 1 && typeof v === 'string' ? v : `${k}: ${v}`).join('\n'))); }
    if (st.result != null) { body.append(el('span', null, 'output')); body.append(el('pre', null, st.result || '(empty)')); }
    d.append(sum, body); steps.append(d);
  }
  parts.push(steps, technical);
  if (t.messages?.length) parts.push(el('h4', null, 'Reply'), el('p', 'prompt', t.messages.join('\n')));
  if (t.state === 'done') {
    parts.push(el('h4', null, 'Hidden test (runs after the agent exits, on a private copy of its workdir)'));
    if (t.check?.script) parts.push(el('pre', null, t.check.script));
    parts.push(el('h4', null, 'Test output'), el('pre', null, (t.check?.log || '(none)') + (t.check?.changed?.length ? `\n\nfiles changed: ${t.check.changed.join(', ')}` : '') + (t.check?.off_target?.length ? `\noff-target changes: ${t.check.off_target.join(', ')}` : '')));
  }
  box.replaceChildren(...parts);
  box.scrollTop = 0;
  if (narrow && matchMedia('(max-width: 1000px)').matches) box.scrollIntoView({ block: 'nearest', behavior: reduced.matches ? 'instant' : 'smooth' });
}

async function loadSpec(id) {
  try { const r = await api(`/runs/${id}/spec`); if (r.ok && r.body.spec) { $('#spec-text').textContent = r.body.spec; $('#spec').hidden = false; } } catch { /* optional */ }
}

// The field: one square per agent. Waiting, alive (pulsing orange), passed, failed.
const canvas = $('#field'), ctx = canvas.getContext('2d');
let geo = null, raf = 0;
function resizeField() {
  if (!run) return;
  const wrap = canvas.parentElement, w = wrap.clientWidth - parseFloat(getComputedStyle(wrap).paddingLeft) * 2;
  const n = run.size, gap = n <= 8 ? 12 : n > 128 ? 4 : 6;
  // Columns for a roughly 2.4:1 field on wide screens, squarer on phones.
  const aspect = w > 560 ? 2.4 : 1.2;
  let cols = Math.max(1, Math.min(n, Math.ceil(Math.sqrt(n * aspect))));
  let cell = Math.min(34, Math.floor((w - gap * (cols - 1)) / cols));
  if (n <= 8) { cols = Math.min(n, Math.max(1, Math.floor((w + gap) / 56))); cell = Math.min(44, Math.floor((w - gap * (cols - 1)) / cols)); }
  const rows = Math.ceil(n / cols), dpr = Math.min(2, devicePixelRatio || 1);
  const width = cols * cell + (cols - 1) * gap, height = rows * cell + (rows - 1) * gap;
  canvas.width = Math.round(w * dpr); canvas.height = Math.round(height * dpr);
  canvas.style.height = `${height}px`;
  canvas.parentElement.classList.toggle('small-wave', n <= 8);
  geo = { cols, cell, gap, dpr, x0: Math.floor((w - width) / 2), w };
  canvas.setAttribute('aria-label', `${n} squares, one per agent`);
}
addEventListener('resize', () => { resizeField(); draw(performance.now()); if (preset) drawSizePreview(); });
canvas.addEventListener('click', e => {
  if (!run || !geo) return;
  const r = canvas.getBoundingClientRect(), x = e.clientX - r.left - geo.x0, y = e.clientY - r.top;
  const col = Math.floor(x / (geo.cell + geo.gap)), row = Math.floor(y / (geo.cell + geo.gap)), i = row * geo.cols + col;
  if (x >= 0 && y >= 0 && x % (geo.cell + geo.gap) < geo.cell && y % (geo.cell + geo.gap) < geo.cell && col < geo.cols && i >= 0 && i < run.size) inspect(i);
});

function ink(name) { return getComputedStyle(document.documentElement).getPropertyValue(name).trim(); }
function draw(t) {
  if (!run || !geo) return;
  const { cols, cell, gap, dpr, x0 } = geo, c = { wait: ink('--wait'), run: ink('--run'), pass: ink('--pass'), fail: ink('--fail'), label: ink('--ink') };
  ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
  ctx.clearRect(0, 0, canvas.width, canvas.height);
  // Admission order is close to slot order, so the first `admitted` unfinished slots are shown alive.
  let aliveLeft = run.state === 'running' ? Math.max(0, run.admitted - run.done.size) : 0;
  const r = Math.max(1.5, cell * 0.16);
  for (let i = 0; i < run.size; i++) {
    const x = x0 + (i % cols) * (cell + gap), y = Math.floor(i / cols) * (cell + gap);
    const a = run.done.get(i);
    let fill = c.wait, scale = 1, alpha = 1;
    if (a) {
      fill = a.passed ? c.pass : c.fail;
      const age = (t - (run.popped.get(i) || 0)) / 260;
      if (age < 1 && !reduced.matches) scale = 1 + Math.min(0.18, gap * 0.7 / cell) * Math.sin(age * Math.PI);
    } else if (run.agents.get(i)?.state === 'checking') {
      fill = ink('--check');
    } else if (run.agents.size ? run.agents.get(i)?.state === 'active' : aliveLeft > 0) {
      aliveLeft--; fill = c.run;
      if (!reduced.matches) alpha = 0.55 + 0.45 * (0.5 + 0.5 * Math.sin(t / 180 + i * 0.9));
    }
    const s = cell * scale, o = (cell - s) / 2;
    ctx.globalAlpha = alpha; ctx.fillStyle = fill;
    ctx.beginPath(); ctx.roundRect(x + o, y + o, s, s, r); ctx.fill();
    if (a && !a.passed) {
      ctx.globalAlpha = 1; ctx.strokeStyle = '#161510'; ctx.lineWidth = Math.max(1.5, cell / 12);
      ctx.beginPath(); ctx.moveTo(x + cell * .3, y + cell * .3); ctx.lineTo(x + cell * .7, y + cell * .7);
      ctx.moveTo(x + cell * .7, y + cell * .3); ctx.lineTo(x + cell * .3, y + cell * .7); ctx.stroke();
    }
    if (cell >= 28 && run.size <= 32 && !(a && !a.passed)) {
      ctx.globalAlpha = 1; ctx.fillStyle = fill === c.wait ? c.label : '#161510'; ctx.font = '11px "IBM Plex Mono", monospace';
      ctx.textAlign = 'center'; ctx.textBaseline = 'middle'; ctx.fillText(String(i).padStart(3, '0'), x + cell / 2, y + cell / 2);
    }
  }
  ctx.globalAlpha = 1;
}
function tick() {
  cancelAnimationFrame(raf);
  const loop = t => { draw(t); if (run && (run.state === 'queued' || run.state === 'running' || [...run.popped.values()].some(p => t - p < 300))) raf = requestAnimationFrame(loop); };
  raf = requestAnimationFrame(loop);
}

// ---- 3 · the receipt ----
function receipt(rec) {
  const box = $('#receipt-body');
  box.replaceChildren();
  if (rec.state !== 'done' || !rec.result) {
    box.append(el('p', 'muted', rec.error || 'The run stopped before a full receipt was available. Recorded attempts remain visible above.'));
    box.append(again());
    return;
  }
  draw(performance.now()); tick();
  const r = rec.result, ok = rec.verify === 'verified';
  const head = el('div', 'receipt-head');
  const title = el('h3');
  title.append(el('span', 'receipt-count', `${fmt(r.passes)} of ${fmt(r.attempts)} `),
    el('span', 'receipt-caption', `${hname(rec.harness)} agents passed hidden tests`));
  const orbit = el('span', 'receipt-orbit');
  const percent = r.attempts > 0 && Number.isFinite(r.passes) ? Math.round(100 * r.passes / r.attempts) : null;
  orbit.append(el('b', null, percent == null ? '—' : `${percent}%`)); orbit.setAttribute('aria-hidden', 'true');
  if (percent != null) orbit.style.background = `conic-gradient(var(--pass) ${percent}%, ${r.passes < r.attempts ? 'var(--fail)' : 'var(--line)'} 0)`;
  head.append(orbit, title, el('span', ok ? 'verified' : 'verified no', ok ? '✓ Bundle verified' : '✕ Verification failed'));
  const facts = el('dl', 'facts');
  const extra = el('dl', 'facts extra-facts');
  const fact = (k, v, unit, target = facts) => { const d = el('div'); const dd = el('dd', null, v); if (unit) dd.append(el('small', null, unit)); d.append(el('dt', null, k), dd); target.append(d); };
  fact('Failed', Number.isFinite(r.attempts) && Number.isFinite(r.passes) ? fmt(r.attempts - r.passes) : '—');
  fact('Successes', rounded(r.successes_per_minute), '/ min');
  fact('Most alive at once', fmt(r.max_running));
  fact('Peak RSS, median', rounded(r.peak_rss_mb_median), 'MB, upper bound');
  fact('Harness', hname(rec.harness), null, extra);
  fact('Wall time, median', fixed(r.wall_s_median, 2), 's', extra);
  fact('CPU per success', fixed(r.cpu_s_per_success, 3), 's', extra);
  fact('Admission wait', fixed(r.admission_wait_s_total, 1), 's, all agents summed', extra);
  const hashes = el('p', 'hashes');
  const h = (k, v) => { const s = el('span', null, `${k} `); s.append(el('b', null, v)); hashes.append(s); };
  h('plan', String(rec.plan_sha256 || '').slice(0, 12)); h('chain head', rec.chain_head || ''); h('runtime', rec.harness_version || '');
  const proof = el('details', 'receipt-proof');
  proof.append(el('summary', null, 'Measurements and provenance'), extra, hashes);
  box.append(head, facts);
  if (r.max_running != null && r.wall_s_median != null && r.max_running < r.attempts && r.attempts > 1) {
    proof.append(el('p', 'muted', `Each job took about ${fixed(r.wall_s_median, 1)} s, so agents start and finish in waves: launch rate and admission set how many overlap. Longer sessions keep more alive at once.`));
  }
  box.append(again(), proof);
  $('#version').textContent = rec.harness_version || '';
}

function again() {
  const b = el('button', 'again', 'Launch another');
  b.type = 'button';
  b.addEventListener('click', () => {
    document.body.dataset.phase = 'idle'; $('#setup').open = true;
    $('#launch').scrollIntoView({ behavior: reduced.matches ? 'instant' : 'smooth' });
    $('#presets [aria-checked="true"]')?.focus({ preventScroll: true });
  });
  return b;
}

// ---- start ----
$('#go').addEventListener('click', launch);
$('#passkey-link').addEventListener('click', () => showPasskey(true));
const unlock = () => { const v = $('#passkey-input').value.trim(); if (!v) return; passkey = v; store.set(v); $('#passkey-input').value = ''; showPasskey(false); note('Passkey saved for this tab. Launch again.'); };
$('#passkey-go').addEventListener('click', unlock);
$('#passkey-input').addEventListener('keydown', e => { if (e.key === 'Enter') unlock(); });
showPasskey(false);
(async () => {
  try {
    const r = await api('/presets');
    if (!r.ok || !Array.isArray(r.body.presets) || !r.body.presets.length) throw Error();
    presets = r.body.presets; online = true;
  } catch {
    presets = OFFLINE_PRESETS; online = false;
    note('Live runs are offline right now. The jobs below are what you could launch.', true);
  }
  choose(presets.find(p => p.id === 'mixed') || presets[0]);
  pollStatus(); setInterval(pollStatus, 6000);
  const id = new URLSearchParams(location.search).get('run');
  if (online && /^[0-9a-f]{12}$/.test(id || '')) {
    const r = await api(`/runs/${id}`).catch(() => null);
    if (r?.ok) {
      const rec = r.body.run;
      busy = rec.state === 'queued' || rec.state === 'running'; updateGo();
      follow(id, rec.title, rec.size, rec.harness);
    }
  }
})();
