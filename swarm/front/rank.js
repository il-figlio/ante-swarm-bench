// What the page counts and compares, kept apart from the DOM so it can be tested (tests/test_page.py runs it in
// node). Rules: a number is compared only with numbers that mean the same thing, the page says who was compared,
// and nothing the runner did is counted against a harness.
(() => {
  const finite = (xs) => xs.filter((x) => x != null && Number.isFinite(x));
  const max = (xs) => { const v = finite(xs); return v.length ? Math.max(...v) : null; };

  // Failures in words. NOT_RUN: the runner never got an agent going, or stopped it itself. Not the harness's.
  const FAIL_WORDS = { oom_killed: 'out of memory', descendant_leak: 'left processes running', timeout: 'timed out',
    check_failed: 'wrong answer', off_target: 'touched other files', agent_error: 'crashed', admission_timeout: 'never admitted',
    request_budget_exhausted: 'ran out of requests', request_incomplete: 'stopped mid-request', script_unfinished: 'stopped early',
    infra: 'runner error', cancelled: 'cancelled', request_error: 'request rejected', late_change: 'files changed after exit',
    output_limit: 'too much output' };
  const NOT_RUN = ['infra', 'cancelled', 'admission_timeout'];
  const INFRA_WORDS = { admission_timeout: 'never admitted: the machine had no room', cancelled: 'cancelled', disk_full: 'the machine’s disk was full', setup_failed: 'the runner’s setup failed', runner_error: 'the runner failed' };
  const notRun = (a) => !a.passed && NOT_RUN.includes(a.failure);

  // Counts for one lane's attempts. `failed` is the harness's own failures only.
  function tally(A) {
    const passed = A.filter((a) => a.passed).length, skipped = A.filter(notRun);
    const n = {}; A.forEach((a) => { if (!a.passed && a.failure && !notRun(a)) n[a.failure] = (n[a.failure] || 0) + 1; });
    const top = Object.entries(n).sort((x, y) => y[1] - x[1])[0];
    const why = {}; skipped.forEach((a) => { const k = a.failure !== 'infra' ? a.failure : (a.infra && a.infra.reason) || 'runner_error'; why[k] = (why[k] || 0) + 1; });
    const topWhy = Object.entries(why).sort((x, y) => y[1] - x[1])[0];
    return { passed, notRun: skipped.length, failed: A.length - passed - skipped.length, failClass: top ? top[0] : null,
      timeouts: n.timeout || 0, notRunWhy: topWhy ? INFRA_WORDS[topWhy[0]] || INFRA_WORDS.runner_error : null };
  }

  // Lane peaks from its samples [t, running, memory MB, cpu %, available MB] and attempts.
  function peaks(series, attempts) {
    const one = max(attempts.map((a) => a.rss_mb));
    // Agents that finish between samples never show in the sampled figure, so the peak is at least one agent's largest process.
    const sampled = max(series.map((x) => x[2]));
    // Memory per agent: the swarm's memory over the agents alive, at the sample where most were alive.
    let per = null, top = 0, mem = 0;
    series.forEach((x) => { if (x[1] > 0 && x[2] != null && (x[1] > top || (x[1] === top && x[2] > mem))) { top = x[1]; mem = x[2]; per = x[2] / x[1]; } });
    return { mem: sampled == null && one == null ? null : Math.max(sampled || 0, one || 0), per, cpu: max(series.map((x) => x[3])), alive: max(series.map((x) => x[1])) };
  }

  // Why a chart has nothing to draw. "Too brief" is only true when agents ran; when none ever started, say that.
  function emptyWhy(series, attempts, done) {
    if (!series.length) return 'no samples yet';
    const started = series.some((x) => x[1] > 0) || attempts.some((a) => a.ran);
    return started ? 'too brief to sample' : done ? 'no agent started' : 'no agent running yet';
  }

  // ---- ranking (the shading itself is heat.js) --------------------------------------------------------
  // [metric, better end, gated]. Gated metrics rank only harnesses where at least half the agents passed: a harness
  // that fails fast is not "cheap" or "quick". Startup is not gated: it is measured before any work, so every harness
  // with a first request is compared. Failures are shown, not scored. mini is the floor and is left out of every rank.
  const HEAT = { pass: ['passed', 'high'], per: ['per', 'low', 1], rss: ['rss', 'low', 1], cpu: ['cpu', 'low', 1],
    disk: ['disk', 'low', 1], start: ['start', 'low'], gap: ['gap', 'low', 1], slope: ['slope', 'low', 1],
    peakmem: ['mem', 'low', 1], wall: ['wall', 'low', 1], first: ['first', 'low'] };
  // Resource numbers mean something only when most agents finished the work: under half passing, not ranked.
  // Attempts that never ran are not the harness's: half is counted of those that did.
  const thin = (m) => !m.passed || (m.n > 0 && 2 * m.passed < m.n - (m.notRun || 0));
  const MEMORY = ['per', 'mem'];
  // Lanes can measure memory differently (a machine without the cgroup figure). Such numbers are not comparable:
  // flag every lane whose scope is not the commonest one.
  function markScopes(rows) {
    const count = {}; rows.forEach(({ m }) => { if (m.memScope) count[m.memScope] = (count[m.memScope] || 0) + 1; });
    const usual = (Object.entries(count).sort((a, b) => b[1] - a[1])[0] || [])[0];
    rows.forEach(({ m }) => { m.memOdd = !!(usual && m.memScope && m.memScope !== usual); });
    return rows;
  }
  // Why this harness's number is left out of a metric's ranking, or null when it takes part. rows: [{ r: {h, name}, m }].
  function excluded(r, m, key, gated) {
    if (r.h === 'mini') return 'floor: not ranked';
    // Time on a slower VM scales: without a valid control of its own the raw number is shown and takes no part.
    if (key in NORMALISED && m.ratio !== undefined && m[key] != null && basis(m, key) == null) return `${m.noControl || 'no control for this metric'}: shown raw, not ranked`;
    // A wave where agents were stopped at the time limit lasted about as long as the limit: not a speed.
    if (key === 'wall' && m.timeouts) return `${m.timeouts} agent${m.timeouts === 1 ? '' : 's'} stopped at the time limit: not compared`;
    // Passes out of fewer attempts than the others had is not a lower score.
    if (key === 'passed' && m.notRun) return `${m.notRun} attempt${m.notRun === 1 ? '' : 's'} not run: not compared`;
    if (MEMORY.includes(key) && m.memOdd) return 'memory measured differently on this machine: not compared';
    if (gated && thin(m)) return 'under half passed: not ranked';
    return null;
  }
  // The number that is ranked: the ratio to the lane's own control for time metrics once relative() has run (null without
  // a valid control), the raw value otherwise. Wall above size 1 has no ratio and ranks raw, as /stats does.
  function basis(m, key) {
    if (!(key in NORMALISED) || m.ratio === undefined) return m[key];
    if (m.noControl) return null;
    // Wall ranks raw only above size 1, where it is reference by design; at size 1 a missing ratio is no control.
    if (key === 'wall' && m.ratio.wall === undefined) return m.wallRaw ? m.wall : null;
    return m.ratio[key] === undefined ? null : m.ratio[key];
  }
  const values = (rows, key, gated) => rows.map(({ r, m }) => (excluded(r, m, key, gated) ? null : basis(m, key)));
  // The tooltip for one number: who it was compared with, or why it was not.
  function compared(rows, i, pos, key, gated) {
    const out = excluded(rows[i].r, rows[i].m, key, gated);
    if (out) return out;
    const others = rows.filter((x, j) => j !== i && pos[j] != null).map((x) => x.r.name);
    return pos[i] != null && others.length ? `against ${others.join(', ')}` : 'not compared: too close, or nothing to compare with';
  }
  // Per harness: metrics it was ranked on, metrics where it was the sole best, and who it was ranked against.
  // A metric counts as a win only for a sole best: a tie (every passer at 32/32) is nobody's win.
  // ties: { metric: [{a, b, tie}] } with harness ids (from /stats), or null for the 10% gap rule of `rank`.
  function score(rows, rank, ties) {
    const out = new Map(rows.map(({ r }) => [r.h, { wins: 0, of: 0, rivals: [], narrow: null }])), positions = {};
    for (const [k, [key, better, gated]] of Object.entries(HEAT)) {
      const vals = values(rows, key, gated);
      const pos = positions[k] = ties && ties[k] ? positions_(vals, better, ties[k], rank, rows.map(({ r }) => r.h)) : rank(vals, better);
      const top = pos.filter((t) => t === 1).length;
      rows.forEach(({ r }, i) => {
        if (pos[i] == null) return;
        const sc = out.get(r.h); sc.of += 1;
        const these = rows.filter((x, j) => j !== i && pos[j] != null).map((x) => x.r.name);
        these.forEach((name) => { if (!sc.rivals.includes(name)) sc.rivals.push(name); });
        if (pos[i] === 1 && top === 1) sc.wins += 1;
      });
    }
    // narrow: who a harness met on the resource numbers, where most of its wins come from: the others with at least
    // half passed. Null for a harness that is itself under half (it is ranked on passes and startup only).
    const pool = rows.filter(({ r, m }) => r.h !== 'mini' && !thin(m));
    pool.forEach(({ r }) => { out.get(r.h).narrow = pool.filter((x) => x.r.h !== r.h).map((x) => x.r.name); });
    return { score: out, positions };
  }
  // "best on 6 of 7" alone reads as beating everyone in the race. When the resource numbers were ranked against
  // fewer harnesses than raced (the rest had under half pass), say who. field: harnesses in the race, mini aside.
  function leadText(sc, field) {
    if (!sc || !sc.wins) return '';
    const n = sc.narrow;
    const who = !n || n.length >= field - 1 ? '' : n.length === 1 ? ` · resources vs ${n[0]} only` : ` · resources vs ${n.length} of ${field - 1} others`;
    return `best on ${sc.wins} of ${sc.of}${who}`;
  }

  // ---- control (the lane's own mini run) and ties ------------------------------------------------------
  // Page key -> the control median it is divided by. Time on a slower VM scales, so a ratio compares like with like.
  // wall is normalised at size 1 only: a wave of many agents measures contention, which the control does not have.
  const NORMALISED = { start: 'first_request_s', gap: 'step_gap_s', cpu: 'cpu_s', wall: 'wall_s' };
  const WHY = { none: 'no control', failed: 'control failed', noisy: 'control noisy', odd: 'control differs from other lanes' };
  // Why a lane's control cannot be used, or null. odd: set by the caller from oddControls().
  function controlWhy(control, odd) {
    if (!control || !control.median || ['off', 'skipped'].includes(control.state)) return WHY.none;
    if (control.state === 'failed') return WHY.failed;
    if (control.noisy) return WHY.noisy;
    return odd ? WHY.odd : null;
  }
  // keys: { lane: control key or null }. Lanes whose key is not the commonest one (a different control cannot be pooled).
  function oddControls(keys) {
    const count = {}; Object.values(keys).forEach((k) => { if (k) count[k] = (count[k] || 0) + 1; });
    const usual = (Object.entries(count).sort((a, b) => b[1] - a[1])[0] || [])[0];
    const out = {}; Object.entries(keys).forEach(([lane, k]) => { out[lane] = !!(usual && k && k !== usual); });
    return out;
  }
  // m (raw values) plus m.ratio = { start, gap, cpu, wall } each ÷ the lane's control, and m.noControl = reason or null.
  // A control median that is missing or zero gives no ratio for that metric (never 0 or Infinity). m.mismatch from the caller.
  function relative(m, control, size) {
    const why = controlWhy(control, m.mismatch), ratio = {};
    if (!why) for (const [k, field] of Object.entries(NORMALISED)) {
      if (k === 'wall' && size !== 1) continue;
      const c = control.median[field];
      if (m[k] != null && Number.isFinite(m[k]) && c != null && Number.isFinite(c) && c > 0) ratio[k] = m[k] / c;
    }
    return { ...m, ratio, noControl: why, wallRaw: size !== 1 };
  }
  // What to rank for a metric: the ratio for normalised keys (null without a valid control), raw otherwise.
  // Wall above size 1 has no ratio and is reference only. rows: [{ m }] after relative().
  function rankable(rows, key) {
    return rows.map(({ m }) => (key in NORMALISED ? (m.ratio && !m.noControl && m.ratio[key] != null ? m.ratio[key] : null) : m[key]));
  }
  // Rank with the paired-difference ties from /stats. values: {harness: number|null} (returns {harness: t}) or an array
  // with `names` (returns an array, the shape heat.js rank() returns). t: 0 worst .. 1 best, null unranked. ties:
  // [{a, b, tie}] or null; null calls `rank` (heat.js, the 10% gap rule) and returns it unchanged.
  // Best and worst tied: the column is blank. Harnesses tied with the best share position 1, so nobody is the sole
  // best. Other tied harnesses share one shade, the mean of theirs.
  function positions(values, better, ties, rank, names) {
    return positions_(values, better, ties, rank, names);
  }
  function positions_(values, better, ties, rank, names) {
    const obj = !Array.isArray(values), keys = obj ? Object.keys(values) : names, vals = obj ? keys.map((k) => values[k]) : values;
    if (!ties) { const t = rank(vals, better); return obj ? Object.fromEntries(keys.map((k, i) => [k, t[i]])) : t; }
    const tied = (a, b) => ties.some((p) => p.tie && ((p.a === a && p.b === b) || (p.a === b && p.b === a)));
    const ok = vals.map((x) => x != null && Number.isFinite(x)), t = vals.map(() => null), idx = vals.map((x, i) => i).filter((i) => ok[i]);
    const lo = Math.min(...idx.map((i) => vals[i])), hi = Math.max(...idx.map((i) => vals[i]));
    const pick = (f) => idx.find((i) => vals[i] === f);
    if (idx.length >= 2 && hi !== lo) {
      const bi = pick(better === 'low' ? lo : hi), wi = pick(better === 'low' ? hi : lo);
      if (!tied(keys[bi], keys[wi])) {
        const raw = (i) => (better === 'low' ? (hi - vals[i]) / (hi - lo) : (vals[i] - lo) / (hi - lo));
        const top = idx.filter((i) => i === bi || tied(keys[i], keys[bi]));
        idx.forEach((i) => {
          if (top.includes(i)) { t[i] = 1; return; }
          const group = idx.filter((j) => !top.includes(j) && (j === i || tied(keys[i], keys[j])));
          t[i] = group.reduce((s, j) => s + raw(j), 0) / group.length;
        });
      }
    }
    return obj ? Object.fromEntries(keys.map((k, i) => [k, t[i]])) : t;
  }
  // The sentence under the table: which rule decided shading, and how many repetitions it had.
  function ruleText(stats) {
    const n = stats && stats.n;
    if (!stats || !n || n < 2) return 'n = 1 · shading uses the 10% gap rule, a fallback, not a test';
    if (stats.rule === 'bootstrap') return `n = ${n} repetitions · tie = 95% bootstrap interval of the paired difference includes 0 · time metrics relative to each lane’s control`;
    return `n = ${n} repetitions · fewer than ${stats.min_pairs || 6} valid pairs, so shading uses the 10% gap rule, a fallback, not a test · time metrics relative to each lane’s control`;
  }

  // ---- strips: one dot per repetition -------------------------------------------------------------------
  // values: per repetition, null for one that failed or did not run (hollow, drawn apart from the axis: never at 0).
  // max: the axis end shared by every row, width: px. summary {median,p25,p75,min,max} from /stats, else computed here.
  function quantile(v, q) {
    const s = [...v].sort((a, b) => a - b), p = (s.length - 1) * q, lo = Math.floor(p), hi = Math.ceil(p);
    return s[lo] + (s[hi] - s[lo]) * (p - lo);
  }
  function strip(values, max, width, summary) {
    const ok = (x) => x != null && Number.isFinite(x), v = values.filter(ok), X = (x) => (max > 0 ? Math.min(width, Math.max(0, (x / max) * width)) : 0);
    const s = summary && summary.median != null ? summary : v.length ? { median: quantile(v, 0.5), p25: quantile(v, 0.25), p75: quantile(v, 0.75), min: Math.min(...v), max: Math.max(...v) } : null;
    return { dots: values.map((x, i) => ({ i, v: ok(x) ? x : null, x: ok(x) ? X(x) : null })), missing: values.length - v.length,
      median: s ? X(s.median) : null, box: s ? [X(s.p25), X(s.p75)] : null, range: s ? [X(s.min), X(s.max)] : null };
  }

  // ---- first request weight -----------------------------------------------------------------------------
  // f: { canonical_bytes, system_bytes, tools_bytes, user_bytes, instruction_bytes, injected_bytes }. The user part is the
  // task prompt plus what the harness injected; when the prompt is not found verbatim they cannot be split, so the user part
  // is one segment. `other` is the remainder, so the segments always sum to the total. Null when there is no total.
  function firstRequestSegments(f) {
    if (!f || f.canonical_bytes == null || f.system_bytes == null || f.tools_bytes == null || f.user_bytes == null) return null;
    const split = f.instruction_bytes != null && f.injected_bytes != null;
    const segs = [{ key: 'system', label: 'system', bytes: f.system_bytes }, { key: 'tools', label: 'tools', bytes: f.tools_bytes },
      split ? { key: 'injected', label: 'injected', bytes: f.injected_bytes } : null,
      { key: 'task', label: split ? 'task prompt' : 'task prompt + injected', bytes: split ? f.instruction_bytes : f.user_bytes }].filter(Boolean);
    const rest = f.canonical_bytes - segs.reduce((n, s) => n + s.bytes, 0);
    if (rest < 0) return null;  // medians of different requests: do not draw a bar that does not add up
    segs.push({ key: 'other', label: 'other', bytes: rest });
    return segs;
  }

  const api = { FAIL_WORDS, INFRA_WORDS, NOT_RUN, HEAT, tally, peaks, emptyWhy, thin, markScopes, excluded, values, NORMALISED, controlWhy, oddControls, relative, rankable, positions, ruleText, compared, score, leadText, basis, strip, quantile, firstRequestSegments };
  if (typeof window !== 'undefined') window.Rank = api;
  if (typeof module !== 'undefined') module.exports = api;
})();
