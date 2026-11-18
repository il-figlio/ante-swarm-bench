// The AX-1 cartridge kit shared by trail.js and hill.js: LCD drawing helpers, the D-pad / A / B / keys / swipe input,
// the receipt and tape, and the live switch with its in-page passcode. The maze cartridge (maze.js) keeps its own
// copies; these follow it so the three cartridges look and handle alike.
(() => {
  const $ = (id) => document.getElementById(id);
  const INK = '#141413', DIM = 'rgba(20,20,19,0.55)', TRAIL = 'rgba(20,20,19,0.34)', GHOST = 'rgba(20,20,19,0.07)';
  const PRINT = 'rgba(20,20,19,0.14)', KEY = '#e8501e', GLASS = 'rgba(196,197,191,0.92)', FONT = '"Spec Mono", ui-monospace, monospace';
  const REPEAT = 140;
  const PAD = { up: 'N', right: 'E', down: 'S', left: 'W' };
  const KEYS = { ArrowUp: 'N', ArrowRight: 'E', ArrowDown: 'S', ArrowLeft: 'W', w: 'N', d: 'E', s: 'S', a: 'W', W: 'N', D: 'E', S: 'S', A: 'W' };
  const PRESS = { N: 'up', E: 'right', S: 'down', W: 'left' };

  const store = {
    get(k) { try { return JSON.parse(localStorage.getItem(k) || 'null'); } catch (e) { return null; } },
    set(k, v) { try { localStorage.setItem(k, JSON.stringify(v)); } catch (e) { /* private window */ } },
  };
  const session = (() => {
    try { const s = window.sessionStorage; s.getItem('x'); return s; } catch (e) {
      return { getItem: () => null, setItem: () => { throw new Error('no session storage'); }, removeItem: () => {} };
    }
  })();

  function fit(canvas) {
    const r = canvas.getBoundingClientRect(), dpr = Math.min(2, window.devicePixelRatio || 1);
    const w = Math.max(1, Math.round(r.width * dpr)), h = Math.max(1, Math.round(r.height * dpr));
    if (canvas.width !== w || canvas.height !== h) { canvas.width = w; canvas.height = h; }
    const ctx = canvas.getContext('2d');
    ctx.setTransform(1, 0, 0, 1, 0, 0);
    ctx.clearRect(0, 0, w, h);
    return { ctx, w, h };
  }

  // LCD print: a cell in ink with the faint drop shadow of a segment display.
  function pixel(ctx, x, y, s, color, inset) {
    const p = Math.max(1, s * inset);
    ctx.fillStyle = PRINT;
    ctx.fillRect(x + p + s * 0.04, y + p + s * 0.08, s - 2 * p, s - 2 * p);
    ctx.fillStyle = color;
    ctx.fillRect(x + p, y + p, s - 2 * p, s - 2 * p);
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

  const put = (id, text) => { const el = $(id); if (el && el.textContent !== text) el.textContent = text; };
  const plural = (n, word) => `${n} ${word}${n === 1 ? '' : 's'}`;

  function receipt(rows, legend) {
    const dl = $('receipt');
    dl.replaceChildren();
    for (const [k, v] of rows) {
      const d = document.createElement('div');
      const dt = document.createElement('dt'); dt.textContent = k;
      const dd = document.createElement('dd'); dd.textContent = v;
      d.append(dt, dd); dl.append(d);
    }
    if (legend !== undefined) $('legend').textContent = legend;
  }

  // The tape: rows of [n, who, what, hit?], newest first; `empty` when there are none.
  function tape(rows, empty) {
    const ol = $('tape');
    ol.replaceChildren();
    if (!rows.length) {
      const li = document.createElement('li'); li.dataset.state = 'done'; li.textContent = empty; ol.append(li);
      return;
    }
    rows.forEach((r, i) => {
      const li = document.createElement('li');
      li.dataset.state = i === 0 ? 'now' : 'done';
      if (r[3]) li.dataset.hit = 'true';
      for (const [cls, text] of [['n', r[0]], ['k', r[1]], ['a', r[2]]]) {
        const sp = document.createElement('span'); sp.className = cls; sp.textContent = text; li.append(sp);
      }
      ol.append(li);
    });
  }

  // Standings: one row per player, [who, value, detail, state] with state run|out|win|you.
  function standings(rows) {
    const ol = $('standings');
    ol.replaceChildren();
    for (const [who, val, detail, state] of rows) {
      const li = document.createElement('li');
      li.dataset.state = state;
      for (const [cls, text] of [['st-who', who], ['st-val', val], ['st-detail', detail]]) {
        const sp = document.createElement('span'); sp.className = cls; sp.textContent = text; li.append(sp);
      }
      ol.append(li);
    }
  }

  // ---------- input ----------
  // o: {dir(d, held) -> void, a(), b(), repeat(d) -> bool (hold-repeat this direction), key(e) -> bool (extra keys),
  //     swipe(dx, dy, px) -> bool (handled), tap() (a tap on the screen)}
  function input(o) {
    const pad = document.querySelector('.dpad');
    const flash = (dir) => { pad.dataset.press = dir; setTimeout(() => { if (pad.dataset.press === dir) delete pad.dataset.press; }, 120); };
    for (const b of document.querySelectorAll('.dpad-zone')) {
      let timer = null;
      const stop = () => { clearInterval(timer); timer = null; };
      const d = PAD[b.dataset.dir];
      const go = (held) => { flash(b.dataset.dir); o.dir(d, held); };
      b.addEventListener('pointerdown', (e) => {
        e.preventDefault();
        go(false); stop();
        if (!o.repeat || o.repeat(d)) timer = setInterval(() => go(true), REPEAT);
      });
      for (const ev of ['pointerup', 'pointerleave', 'pointercancel']) b.addEventListener(ev, stop);
      b.addEventListener('click', (e) => { if (e.detail === 0) go(false); });
      b.addEventListener('contextmenu', (e) => e.preventDefault());
    }
    $('btn-a').addEventListener('click', () => o.a());
    $('btn-b').addEventListener('click', () => o.b());
    for (const id of ['btn-a', 'btn-b']) $(id).addEventListener('pointerdown', (e) => e.preventDefault());

    let touch = null;
    const screen = $('board');
    screen.addEventListener('touchstart', (e) => { const t = e.touches[0]; touch = [t.clientX, t.clientY]; }, { passive: true });
    screen.addEventListener('touchend', (e) => {
      if (!touch) return;
      const t = e.changedTouches[0], dx = t.clientX - touch[0], dy = t.clientY - touch[1];
      touch = null;
      if (Math.max(Math.abs(dx), Math.abs(dy)) < 24) { if (o.tap) o.tap(); return; }
      if (o.swipe && o.swipe(dx, dy)) return;
      o.dir(Math.abs(dx) > Math.abs(dy) ? (dx > 0 ? 'E' : 'W') : (dy > 0 ? 'S' : 'N'), false);
    });

    document.addEventListener('keydown', (e) => {
      if (e.metaKey || e.ctrlKey || e.altKey || e.target.closest('input, textarea')) return;
      if (o.key && o.key(e)) { e.preventDefault(); return; }
      const d = KEYS[e.key];
      if (d) {
        e.preventDefault();
        if (!e.repeat || !o.repeat || o.repeat(d)) { flash(PRESS[d]); o.dir(d, e.repeat); }
        return;
      }
      const onControl = e.target.closest('button, a');
      if ((e.key === ' ' || e.key === 'Enter') && !onControl) { e.preventDefault(); o.a(); }
      else if (e.key === 'b' || e.key === 'B' || e.key === 'n' || e.key === 'N') o.b();
    });
  }

  // Option keys (role=radio buttons in a .opts group): check the chosen one, grey the ones `off(v)` says.
  function syncOpts(id, val, off) {
    for (const b of $(id).querySelectorAll('button')) {
      b.setAttribute('aria-checked', String(b.dataset.v === val));
      if (off && off(b.dataset.v)) b.setAttribute('aria-disabled', 'true'); else b.removeAttribute('aria-disabled');
    }
  }

  // ---------- live: the switch, the passcode, the status line ----------
  // o: {client, onSwitch(on) -> bool (false refuses), onUnlocked()}. Returns {setOn(on), showPass(on)}.
  function liveSwitch(o) {
    const c = o.client;
    let timer = 0;
    const showPass = (on) => {
      $('pass').hidden = !on;
      if (on) put('pass-note', c.denied ? 'wrong passcode' : 'kept in this tab only, sent only to start a round');
    };
    const setOn = (on) => {
      clearInterval(timer); timer = 0;
      $('live').setAttribute('aria-checked', String(on));
      if (on) {
        showPass(!c.passcode);
        c.status();
        timer = setInterval(() => { if (!(c.r && !c.r.over)) c.status(); }, 15000);
      } else showPass(false);
    };
    $('live').addEventListener('click', (e) => { if (e.detail) e.currentTarget.blur(); o.onSwitch(); });
    $('pass').addEventListener('submit', (e) => {
      e.preventDefault();
      const input = $('pass-in');
      if (!c.setPasscode(input.value)) { put('pass-note', input.value.trim() ? 'this tab cannot keep it (storage blocked)' : 'type the passcode first'); return; }
      input.value = ''; input.blur();
      c.notice = null;
      showPass(false);
      if (o.onUnlocked) o.onUnlocked();
    });
    window.addEventListener('pagehide', () => c.leave());
    return { setOn, showPass };
  }

  window.Arcade = { $, INK, DIM, TRAIL, GHOST, PRINT, KEY, GLASS, FONT, store, session, fit, pixel, banner, put, plural,
    receipt, tape, standings, input, syncOpts, liveSwitch };
})();
