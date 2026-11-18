"""Live maze, the front half: front-solo/mazelive.js (the /api/play client) run in node against a fake of the API.

The fake is a stubbed fetch, EventSource, sessionStorage and timer, scripted per scenario; nothing here needs the
server. What is checked: the passcode flow (sessionStorage only, sent only in POST /api/play/rounds, cleared on 403),
the status line for every status, the round start and its event stream URL, move posting with the round token, the
team fog (rival moves and reveals are never kept), the call feed (rival calls lose their arguments), the end reveal,
giving up, and that the cartridge page loads the client under the console CSP without inline code.
"""
import json
import re
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from swarm import solo

FRONT = Path(__file__).resolve().parent.parent / "front-solo"
NODE = shutil.which("node")

HARNESS = r"""
const L = require(process.argv[process.argv.length - 1] + '/mazelive.js');
const PASS = 'correct horse';

function env(route) {
  const calls = [], store = new Map(), timers = [];
  let clock = 1000, pending = 0, overlap = false;
  const fetch = (url, init = {}) => {
    const c = { url, method: init.method || 'GET', headers: init.headers || {}, body: init.body ? JSON.parse(init.body) : null, keepalive: !!init.keepalive };
    calls.push(c);
    if (pending) overlap = true;
    pending++;
    return new Promise((resolve, reject) => setTimeout(() => {
      pending--;
      let out;
      try { out = route(c); } catch (e) { reject(e); return; }
      if (out === 'network') { reject(new TypeError('network')); return; }
      const [status, body] = out;
      resolve({ status, ok: status >= 200 && status < 300, json: async () => { if (body === undefined) throw new Error('no body'); return body; } });
    }, 0));
  };
  class ES {
    constructor(url) { this.url = url; this.l = {}; this.readyState = 0; this.closed = false; ES.all.push(this); }
    addEventListener(t, f) { (this.l[t] = this.l[t] || []).push(f); }
    send(t, d) { for (const f of this.l[t] || []) f({ data: typeof d === 'string' ? d : JSON.stringify(d) }); }
    close() { this.readyState = 2; this.closed = true; }
  }
  ES.all = [];
  const storage = { getItem: (k) => (store.has(k) ? store.get(k) : null), setItem: (k, v) => store.set(k, String(v)), removeItem: (k) => store.delete(k) };
  let changes = 0;
  const client = new L.Client({ fetch, EventSource: ES, storage, now: () => clock, onChange: () => changes++,
    setTimeout: (f, ms) => { timers.push({ f, ms }); return timers.length; }, clearTimeout: (id) => { if (timers[id - 1]) timers[id - 1].f = null; } });
  const flush = () => new Promise((r) => setTimeout(r, 5)).then(() => new Promise((r) => setTimeout(r, 5)));
  return { client, calls, store, timers, ES, flush, tick: (ms) => { clock += ms; }, get overlap() { return overlap; }, get changes() { return changes; } };
}

const DIMS = { cols: 16, rows: 10, start: 5 * 16 + 8, exit: 159 };
const opts = (o = {}) => ({ size: 's', opp: 'swarm4', assist: true, dims: DIMS, ...o });
const out = {};

function api(extra = {}) {
  return (c) => {
    if (c.url === '/api/play/status') return extra.status || [200, { live: true, queue: 0, max_antes: 16, model: 'gpt-6.1-terra' }];
    if (c.url === '/api/play/rounds' && c.method === 'POST') {
      if (extra.rounds) return extra.rounds(c);
      return c.body.passcode === PASS ? [201, { round: 'r1', token: 'tok-1', seed_hash: 'abc123' }] : [403, { error: 'wrong passcode' }];
    }
    if (c.url === '/api/play/rounds/r1/move') return extra.move ? extra.move(c) : [200, { pos: DIMS.start + 1, ok: true, open: ['W', 'E'] }];
    if (c.url === '/api/play/rounds/r1' && c.method === 'DELETE') return [204, undefined];
    return [404, { error: 'not found' }];
  };
}

(async () => {
  // ---------- status lines ----------
  out.status = {};
  for (const [name, route] of [
    ['live', [200, { live: true, queue: 0, max_antes: 16, model: 'gpt-6.1-terra' }]],
    ['queue', [200, { live: true, queue: 2, max_antes: 16, model: 'claude-haiku-5-5' }]],
    ['nomodel', [200, { live: true, queue: 0, max_antes: 16, model: '<b>x</b>' }]],
    ['limit', [200, { live: false, reason: 'limit', queue: 0, max_antes: 16, model: 'gpt-6.1-terra' }]],
    ['signin', [200, { live: false, reason: 'signin', queue: 0, max_antes: 16, model: 'gpt-6.1-terra' }]],
    ['nopass', [200, { live: false, reason: 'no_passcode_configured', queue: 0, max_antes: 16 }]],
    ['credits', [200, { live: false, reason: 'credits', queue: 0, max_antes: 16 }]],
    ['busy', [200, { live: false, reason: 'busy', queue: 2, max_antes: 16 }]],
    ['absent', [404, { error: 'not found' }]],
    ['down', [502, undefined]],
    ['network', 'network'],
  ]) {
    const e = env(() => route);
    const st = await e.client.status();
    out.status[name] = { line: L.statusLine(st, L.antes({ opp: 'swarm4', assist: true })), one: L.statusLine(st, L.antes({ opp: 'solo', assist: false })), url: e.calls[0].url };
  }
  out.status.unknown = L.statusLine(null, 1);

  // ---------- passcode: needed, kept in session storage, wrong one cleared ----------
  {
    const e = env(api());
    out.need = await e.client.start(opts());
    out.needCalls = e.calls.length;
    out.emptyRefused = e.client.setPasscode('   ');
    e.client.setPasscode('nope');
    out.stored = e.store.get(L.PASS_KEY);
    out.denied = await e.client.start(opts());
    out.afterDenied = { stored: e.store.has(L.PASS_KEY), notice: e.client.notice, denied: e.client.denied, round: e.client.r, streams: e.ES.all.length,
      body: e.calls[e.calls.length - 1].body };
    out.scriptedOnly = await e.client.start(opts({ opp: 'council3' }));
  }

  // ---------- busy and rate-limited ----------
  {
    const e = env(api({ rounds: () => [409, { queue: 2 }] }));
    e.client.setPasscode(PASS);
    out.busy = await e.client.start(opts());
    out.busyNotice = e.client.notice;
    out.busyKeeps = e.store.get(L.PASS_KEY) === PASS;
    const f = env(api({ rounds: () => [429, { error: 'slow down' }] }));
    f.client.setPasscode(PASS);
    out.limited = await f.client.start(opts());
    out.limitedKeeps = f.store.get(L.PASS_KEY) === PASS;
  }

  // ---------- a round: stream, moves, fog, rivals, feed, end ----------
  {
    const e = env(api());
    e.client.setPasscode(PASS);
    out.started = await e.client.start(opts());
    const r = e.client.r, es = e.ES.all[0];
    out.post = e.calls.find((c) => c.url === '/api/play/rounds').body;
    out.esUrl = es.url;
    out.phaseBeforeState = r.phase;
    out.moveBeforeState = e.client.move('E');
    es.send('state', { phase: 'running', cols: 16, rows: 10, start: DIMS.start, exit: 159, t: 0.5, you: { pos: DIMS.start, moves: 0 },
      ally: { pos: DIMS.start }, seen: [[DIMS.start, 0b0101]], rivals: [{ who: 'a01', progress: { calls: 0, mapped: 1, bucket: 'far' } }] });
    out.afterState = { phase: r.phase, t0: r.t0, startWalls: r.walls[DIMS.start], rivals: [...r.rivals.keys()] };
    es.send('state', 'not json');   // ignored
    // A move into a known wall (N is a wall: 0b0101) bumps locally and asks nothing.
    const before = e.calls.length;
    e.client.move('N');
    out.bump = { asked: e.calls.length - before, bumps: r.you.bumps };
    // Two moves at once go one after the other; a third and fourth over the queue are dropped.
    out.queued = [e.client.move('E'), e.client.move('E'), e.client.move('E'), e.client.move('E')];
    await e.flush(); await e.flush();
    const moves = e.calls.filter((c) => c.url.endsWith('/move'));
    out.moves = { n: moves.length, token: moves.map((c) => c.headers['X-Round-Token']), bodies: moves.map((c) => c.body), overlap: e.overlap,
      pos: r.you.pos, count: r.you.moves, walls: r.walls[DIMS.start + 1], trail: r.you.trail.slice() };
    // Rival moves and reveals are dropped; the player's team's are kept.
    es.send('move', { who: 'a01', team: 'rival', pos: 3 });
    es.send('reveal', { team: 'rival', cells: [[3, 15]] });
    es.send('reveal', { team: 'player', cells: [[DIMS.start - 16, 0b1010], { i: [9, 4], walls: 0 }, 77] });
    es.send('move', { who: 'ally', team: 'player', pos: [8, 4] });
    out.fog = { rival3: r.walls[3], above: r.walls[DIMS.start - 16], plain: r.walls[77], ally: r.ally.pos, rivalPos: JSON.stringify([...r.rivals.values()]).includes('pos') };
    e.tick(700);
    es.send('rival', { who: 'a01', progress: { calls: 7, mapped: 12, bucket: 'mid', pos: 99 } });
    es.send('rival', { who: 'a02', progress: { calls: 3, mapped: 5, bucket: 'nowhere' } });
    out.rivals = [...r.rivals.values()].map((v) => ({ ...v }));
    out.lastCall = r.lastCall;
    e.tick(300);
    es.send('call', { who: 'ally', team: 'player', op: 'next-edge', result: 'E' });
    es.send('call', { who: 'a01', team: 'rival', op: 'move', arg: 'S', result: 'pos 9,9' });
    out.feed = r.feed.map((c) => ({ who: c.who, op: c.op, arg: c.arg, result: c.result, team: c.team }));
    // The end: the whole maze, the rivals' paths, the stream closed, no more moves.
    const walls = Array.from({ length: 160 }, (_, i) => (i * 7) % 16);
    es.send('end', { winner: 'rival', reason: 'exit', stats: { calls: 40, tokens: 12345 }, maze: { cols: 16, rows: 10, start: DIMS.start, exit: 159, walls,
      paths: { a01: [DIMS.start, DIMS.start + 1, [10, 5]], a02: [DIMS.start], you: [DIMS.start] } } });
    out.end = { over: r.over, phase: r.phase, winner: r.winner, reason: r.reason, stats: r.stats, mazeWalls: r.maze && r.maze.walls.length,
      wall7: r.maze && r.maze.walls[7], paths: r.paths, closed: es.closed, moveAfter: e.client.move('E') };
    es.send('rival', { who: 'a03', progress: { calls: 1 } });   // after the end: ignored
    out.endIgnores = r.rivals.has('a03');
    // Only the start request ever carried the passcode.
    out.passcodeSeen = e.calls.filter((c) => JSON.stringify(c).includes(PASS)).map((c) => c.url);
    out.passcodeInStream = es.url.includes(PASS);
  }

  // ---------- your win, then the end; an end that never comes ----------
  {
    const e = env(api({ move: () => [200, { pos: 159, ok: true, open: ['N'], won: true }] }));
    e.client.setPasscode(PASS);
    await e.client.start(opts({ assist: false }));
    const r = e.client.r, es = e.ES.all[0];
    out.noAlly = r.ally;
    es.send('state', { you: { pos: [8, 5] } });
    e.client.move('S');
    await e.flush();
    out.won = { won: r.won, phase: r.phase, timers: e.timers.length, moreMoves: e.client.move('N') };
    es.send('end', { winner: 'player', reason: 'exit' });
    out.wonEnd = { winner: r.winner, over: r.over, maze: r.maze, timerCleared: e.timers[0].f === null };

    const f = env(api({ move: () => [200, { pos: 159, ok: true, open: ['N'], won: true }] }));
    f.client.setPasscode(PASS);
    await f.client.start(opts({ assist: false }));
    f.ES.all[0].send('state', {});
    f.client.move('E');
    await f.flush();
    f.timers[0].f();
    out.wonLocal = { over: f.client.r.over, winner: f.client.r.winner, local: f.client.r.local };
  }

  // ---------- queued behind another round; the server unavailable ----------
  {
    const e = env(api({ rounds: () => [201, { round: 'r1', token: 'tok-1', seed_hash: 'h', queue: 1 }] }));
    e.client.setPasscode(PASS);
    await e.client.start(opts());
    const r = e.client.r, es = e.ES.all[0];
    out.queued0 = { phase: r.phase, pos: r.queuePos };
    es.send('state', { phase: 'queued', queue: 1, t: 0 });
    out.queuedMove = e.client.move('E');
    out.queued1 = { phase: r.phase, t0: r.t0 };
    e.tick(5000);
    es.send('state', { phase: 'running', t: 0.25, you: { pos: DIMS.start, moves: 0 } });
    out.running = { phase: r.phase, t0: r.t0 };

    const f = env(api({ rounds: () => [503, { error: 'unavailable', reason: 'credits' }] }));
    f.client.setPasscode(PASS);
    out.unavailable = [await f.client.start(opts()), f.client.notice, f.store.get(L.PASS_KEY) === PASS];
  }

  // ---------- give up, a refused move, a lost stream, leaving the page ----------
  {
    const e = env(api({ move: () => [410, { error: 'over' }] }));
    e.client.setPasscode(PASS);
    await e.client.start(opts());
    const r = e.client.r, es = e.ES.all[0];
    es.send('state', {});
    e.client.move('E');
    await e.flush();
    out.refused = r.moveError;
    es.readyState = 0; es.onerror();
    out.reconnecting = r.stream;
    es.readyState = 2; es.onerror();
    out.lost = r.stream;
    await e.client.giveUp();
    const del = e.calls.find((c) => c.method === 'DELETE');
    out.giveUp = { url: del.url, token: del.headers['X-Round-Token'], phase: r.phase, body: del.body };
    es.send('end', { winner: 'rival', reason: 'gave_up' });
    out.gaveUpEnd = { over: r.over, reason: r.reason, winner: r.winner };

    const f = env(api());
    f.client.setPasscode(PASS);
    await f.client.start(opts());
    f.client.leave();
    const d = f.calls.find((c) => c.method === 'DELETE');
    out.leave = { keepalive: d.keepalive, token: d.headers['X-Round-Token'], closed: f.ES.all[0].closed };
  }

  // ---------- giving up while the round is still being made ----------
  {
    const e = env(api());
    e.client.setPasscode(PASS);
    const p = e.client.start(opts());
    await e.client.giveUp();
    out.cancelled = await p;
    out.cancelledCalls = e.calls.map((c) => `${c.method} ${c.url}`);
    out.cancelledStreams = e.ES.all.length;
  }

  // ---------- one team: roles, notes as counts, the boards at the end ----------
  {
    const e = env(api({ rounds: (c) => [201, { round: 'r1', token: 'tok-1', seed_hash: 'h', queue: 0, shape: 'lead' }] }));
    e.client.setPasscode(PASS);
    await e.client.start(opts({ shape: 'shared' }));
    out.shapePost = e.calls.find((c) => c.url === '/api/play/rounds').body.rival;
    const r = e.client.r, es = e.ES.all[0];
    out.shapeAfterPost = r.shape;
    es.send('state', { phase: 'running', t: 0, shape: 'lead', you: { pos: DIMS.start, moves: 0 }, rivals: [
      { who: 'a01', role: 'lead', progress: { calls: 0, mapped: 0, bucket: 'far', notes: 0 } },
      { who: 'a02', role: 'worker', progress: { calls: 0, mapped: 0, bucket: 'far', notes: 0 } },
      { who: 'a03', role: 'boss', progress: { calls: 0, mapped: 0, bucket: 'far' } }] });
    out.roles = Object.fromEntries([...r.rivals.values()].map((v) => [v.who, v.role]));
    es.send('call', { who: 'a02', team: 'rival', op: 'note', notes: 3, text: 'SECRET corridor at 9,9' });
    es.send('call', { who: 'a01', team: 'rival', op: 'assign', arg: 'a02' });
    es.send('call', { who: 'ally', team: 'player', op: 'note', text: 'dead end‮ east\nof 5,3' });
    es.send('rival', { who: 'a02', progress: { calls: 4, mapped: 3, bucket: 'mid', notes: 3 } });
    out.noteFeed = r.feed.map((c) => ({ who: c.who, op: c.op, text: c.text, notes: c.notes, arg: c.arg }));
    out.counts = { rival: r.rivalNotes, ally: r.allyNotes, a02: r.rivals.get('a02').notes };
    out.secretBeforeEnd = JSON.stringify({ feed: r.feed, rivals: [...r.rivals.values()], notes: r.notes }).includes('SECRET');
    es.send('end', { winner: 'rival', reason: 'exit', shape: 'lead', roles: { a01: 'lead', a02: 'worker', a03: 'worker', ally: 'ally' },
      notes: { rival: [{ n: 1, who: 'a02', text: 'SECRET corridor at 9,9' }, { n: 2, who: 'a03', text: 'x'.repeat(300) }, { n: 3, who: 'a01', text: '\u0000' }, 'junk'],
               player: [{ n: 1, who: 'ally', text: 'dead end east of 5,3' }] } });
    out.endNotes = r.notes;
    out.endRoles = Object.fromEntries([...r.rivals.values()].map((v) => [v.who, v.role]));
    out.endShape = r.shape;
  }
  out.sizes = [L.sizesOf(null), L.sizesOf({ sizes: [1, 4] }), L.sizesOf({ sizes: 'x' }), L.sizesOf({ sizes: [0, -1, 'a'] })];
  out.offered = [L.offered({ sizes: [1, 4] }, 'swarm16'), L.offered({ sizes: [1, 4] }, 'swarm4'), L.offered(null, 'swarm16'), L.offered(null, 'council3')];

  // ---------- helpers ----------
  out.cell = [L.cellOf(5, 16), L.cellOf([2, 3], 16), L.cellOf({ x: 2, y: 3 }, 16), L.cellOf('x', 16)];
  out.open = [L.wallsFromOpen(['N', 'S']), L.wallsFromOpen([]), L.wallsFromOpen(['N', 'E', 'S', 'W', 'Q'])];
  out.antes = [L.antes({ opp: 'solo' }), L.antes({ opp: 'swarm16', assist: true }), L.antes({ opp: 'council3' })];
  console.log(JSON.stringify(out));
})().catch((e) => { console.error(e.stack || e); process.exit(1); });
"""


@unittest.skipUnless(NODE, "node not installed")
class TestLiveClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        r = subprocess.run([NODE, "-e", HARNESS, str(FRONT)], capture_output=True, text=True, timeout=60)
        if r.returncode:
            raise AssertionError(r.stderr)
        cls.o = json.loads(r.stdout)

    def test_status_lines(self):
        s = self.o["status"]
        self.assertEqual(s["live"]["url"], "/api/play/status")
        self.assertEqual(s["live"]["line"], "live · gpt-6.1-terra · 5 antes")
        self.assertEqual(s["live"]["one"], "live · gpt-6.1-terra · 1 ante")
        self.assertEqual(s["queue"]["line"], "live · claude-haiku-5-5 · 5 antes · 2 waiting")
        self.assertEqual(s["nomodel"]["line"], "live · model · 5 antes")   # only a plain model id is shown
        self.assertEqual(s["limit"]["line"], "live off · model usage limit reached · back after it resets")
        self.assertEqual(s["signin"]["line"], "live off · the server's model sign-in needs renewing")
        self.assertEqual(s["nopass"]["line"], "live off · no passcode configured on this server")
        self.assertEqual(s["credits"]["line"], "live off · out of model credits")
        self.assertEqual(s["busy"]["line"], "live busy · a round is running · queue 2")
        self.assertEqual(s["absent"]["line"], "live off · this server has no live rounds")
        self.assertEqual(s["down"]["line"], "live off · server unreachable")
        self.assertEqual(s["network"]["line"], "live off · server unreachable")
        self.assertIn("checking", self.o["status"]["unknown"])

    def test_passcode_flow(self):
        o = self.o
        self.assertEqual(o["need"], {"need": "passcode"})
        self.assertEqual(o["needCalls"], 0)  # nothing is asked without a passcode
        self.assertFalse(o["emptyRefused"])
        self.assertEqual(o["stored"], "nope")
        self.assertEqual(o["denied"], {"denied": True})
        a = o["afterDenied"]
        self.assertFalse(a["stored"])  # a 403 clears it
        self.assertEqual(a["notice"], "wrong passcode")
        self.assertTrue(a["denied"])
        self.assertIsNone(a["round"])
        self.assertEqual(a["streams"], 0)
        self.assertEqual(a["body"], {"passcode": "nope", "size": "s", "rival": {"mode": "swarm", "count": 4}, "assist": True})
        self.assertEqual(o["scriptedOnly"], {"error": "scripted only"})

    def test_busy_and_limited_keep_the_passcode(self):
        self.assertEqual(self.o["busy"], {"busy": True, "queue": 2})
        self.assertEqual(self.o["busyNotice"], "busy · queue 2")
        self.assertTrue(self.o["busyKeeps"])
        self.assertEqual(self.o["limited"], {"limited": True})
        self.assertTrue(self.o["limitedKeeps"])

    def test_round_start_and_stream(self):
        o = self.o
        self.assertEqual(o["started"], {"ok": True})
        self.assertEqual(o["post"], {"passcode": "correct horse", "size": "s", "rival": {"mode": "swarm", "count": 4}, "assist": True})
        self.assertEqual(o["esUrl"], "/api/play/rounds/r1/events?t=tok-1")
        self.assertEqual(o["phaseBeforeState"], "starting")
        self.assertFalse(o["moveBeforeState"])  # no moves before the server's snapshot
        self.assertEqual(o["afterState"], {"phase": "live", "t0": 500, "startWalls": 5, "rivals": ["a01"]})

    def test_moves(self):
        o = self.o
        self.assertEqual(o["bump"], {"asked": 0, "bumps": 1})
        self.assertEqual(o["queued"], [True, True, True, False])  # one in flight, two waiting, the rest dropped
        m = o["moves"]
        self.assertEqual(m["n"], 3)
        self.assertEqual(m["token"], ["tok-1"] * 3)
        self.assertEqual(m["bodies"], [{"dir": "E"}] * 3)
        self.assertFalse(m["overlap"])  # one request in flight at a time
        start = 5 * 16 + 8
        self.assertEqual(m["pos"], start + 1)
        self.assertEqual(m["count"], 1)  # the fake answers the same cell each time: one move counted
        self.assertEqual(m["walls"], 0b0101)  # open W and E
        self.assertEqual(m["trail"], [start, start + 1])

    def test_team_fog_never_keeps_rival_positions(self):
        f = self.o["fog"]
        self.assertEqual(f["rival3"], -1)
        self.assertEqual(f["above"], 0b1010)
        self.assertEqual(f["plain"], -1)  # a cell without walls is not a reveal
        self.assertEqual(f["ally"], 4 * 16 + 8)
        self.assertFalse(f["rivalPos"])

    def test_rivals_and_feed(self):
        rv = {v["who"]: v for v in self.o["rivals"]}
        self.assertEqual((rv["a01"]["calls"], rv["a01"]["mapped"], rv["a01"]["bucket"], rv["a01"]["at"]), (7, 12, "mid", 1700))
        self.assertNotIn("pos", rv["a01"])
        self.assertIsNone(rv["a02"]["bucket"])  # an unknown bucket is not shown
        self.assertEqual(self.o["lastCall"], 1700)
        self.assertEqual(self.o["feed"], [
            {"who": "ally", "op": "next-edge", "arg": None, "result": "E", "team": "player"},
            {"who": "a01", "op": "move", "arg": None, "result": None, "team": "rival"},
        ])

    def test_end_reveals_the_maze_and_paths(self):
        e = self.o["end"]
        start = 5 * 16 + 8
        self.assertTrue(e["over"])
        self.assertEqual((e["phase"], e["winner"], e["reason"]), ("over", "opp", "exit"))
        self.assertEqual(e["stats"], {"calls": 40, "tokens": 12345})
        self.assertEqual((e["mazeWalls"], e["wall7"]), (160, (7 * 7) % 16))
        self.assertEqual(e["paths"], {"a01": [start, start + 1, 5 * 16 + 10], "a02": [start], "you": [start]})
        self.assertTrue(e["closed"])
        self.assertFalse(e["moveAfter"])
        self.assertFalse(self.o["endIgnores"])
        self.assertEqual(self.o["passcodeSeen"], ["/api/play/rounds"])
        self.assertFalse(self.o["passcodeInStream"])

    def test_win_waits_for_the_server(self):
        o = self.o
        self.assertIsNone(o["noAlly"])
        self.assertEqual(o["won"], {"won": True, "phase": "finishing", "timers": 1, "moreMoves": False})
        self.assertEqual(o["wonEnd"], {"winner": "you", "over": True, "maze": None, "timerCleared": True})
        self.assertEqual(o["wonLocal"], {"over": True, "winner": "you", "local": True})

    def test_queued_then_running(self):
        o = self.o
        self.assertEqual(o["queued0"], {"phase": "queued", "pos": 1})
        self.assertFalse(o["queuedMove"])
        self.assertEqual(o["queued1"], {"phase": "queued", "t0": None})  # the clock starts when the round runs
        self.assertEqual(o["running"], {"phase": "live", "t0": 5750})
        self.assertEqual(o["unavailable"], [{"error": "credits"}, "live off · out of model credits", True])

    def test_give_up_refusals_and_leaving(self):
        o = self.o
        self.assertEqual(o["refused"], "move refused (http 410)")
        self.assertEqual((o["reconnecting"], o["lost"]), ("reconnecting", "lost"))
        self.assertEqual(o["giveUp"], {"url": "/api/play/rounds/r1", "token": "tok-1", "phase": "finishing", "body": None})
        self.assertEqual(o["gaveUpEnd"], {"over": True, "reason": "gave_up", "winner": "opp"})
        self.assertEqual(o["leave"], {"keepalive": True, "token": "tok-1", "closed": True})
        self.assertEqual(o["cancelled"], {"error": "cancelled"})
        self.assertEqual(o["cancelledCalls"], ["POST /api/play/rounds", "DELETE /api/play/rounds/r1"])
        self.assertEqual(o["cancelledStreams"], 0)

    def test_one_team_roles_and_notes(self):
        o = self.o
        self.assertEqual(o["shapePost"], {"mode": "swarm", "count": 4, "shape": "shared"})
        self.assertEqual(o["shapeAfterPost"], "lead")   # the server says which shape it runs
        self.assertEqual(o["roles"], {"a01": "lead", "a02": "worker", "a03": None})   # an unknown role is not shown
        self.assertEqual(o["noteFeed"], [
            {"who": "a02", "op": "note", "text": None, "notes": 3, "arg": None},     # a rival's note: a count only
            {"who": "a01", "op": "assign", "text": None, "notes": None, "arg": None},
            {"who": "ally", "op": "note", "text": "dead end east of 5,3", "notes": None, "arg": None},
        ])
        self.assertEqual(o["counts"], {"rival": 3, "ally": 1, "a02": 3})
        self.assertFalse(o["secretBeforeEnd"])
        self.assertEqual(o["endNotes"]["player"], [{"who": "ally", "text": "dead end east of 5,3"}])
        rival = o["endNotes"]["rival"]
        self.assertEqual(rival[0], {"who": "a02", "text": "SECRET corridor at 9,9"})
        self.assertEqual(len(rival[1]["text"]), 120)
        self.assertEqual(len(rival), 2)   # an empty note and junk are dropped
        self.assertEqual(o["endRoles"], {"a01": "lead", "a02": "worker", "a03": "worker"})
        self.assertEqual(o["endShape"], "lead")

    def test_sizes_from_status(self):
        self.assertEqual(self.o["sizes"], [[1, 4, 16], [1, 4], [1, 4, 16], [1, 4, 16]])
        self.assertEqual(self.o["offered"], [False, True, True, False])

    def test_helpers(self):
        self.assertEqual(self.o["cell"], [5, 50, 50, None])
        self.assertEqual(self.o["open"], [10, 15, 0])
        self.assertEqual(self.o["antes"], [1, 17, 0])


class TestCartridgeLoadsTheClient(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="play-front-"))
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), solo.handler_for(solo.Library(cls.tmp), FRONT, FRONT.parent / "front-maze"))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, True)

    def test_page_scripts_in_order_and_served(self):
        with urllib.request.urlopen(self.base + "/maze/") as r:
            html = r.read().decode()
        srcs = re.findall(r'<script src="([^"]+)"></script>', html)
        self.assertEqual(srcs, ["/maze/maze-core.js", "/mazeplay.js", "/mazelive.js", "/maze.js"])
        with urllib.request.urlopen(self.base + "/mazelive.js") as r:
            self.assertTrue(r.headers["Content-Type"].startswith("text/javascript"))
        for el in ('id="live"', 'role="switch"', 'id="assist"', 'id="pass"', 'type="password"', 'id="live-status"', 'id="rivals"'):
            self.assertIn(el, html)
        self.assertIsNone(re.search(r"\sstyle\s*=|\son[a-z]+\s*=|<style\b", html))

    def test_no_dialogs_no_secrets_no_inline_style(self):
        live, page = (FRONT / "mazelive.js").read_text(), (FRONT / "maze.js").read_text()
        for src in (live, page):
            self.assertIsNone(re.search(r"\b(prompt|alert|confirm)\s*\(", src))
            self.assertNotIn("setAttribute('style'", src)
            self.assertNotIn(".cssText", src)
        self.assertNotIn("localStorage", live)  # the passcode is session-only
        self.assertIn("sessionStorage", page)
        self.assertNotRegex(live + page, r"passcode\s*[:=]\s*['\"][^'\"]+['\"]")


if __name__ == "__main__":
    unittest.main()
