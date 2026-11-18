"""Live trail and hill (docs/live.md, "Games: trail and hill"): the game service's token and peer checks, the
arcade's admission (passcode first, shapes, the one-round slot shared with the live maze), whole stand-in rounds over
HTTP with the real Ante binary, and the cartridge pages under the console's strict CSP.

No model is ever called: rounds run the scripted model, which drives each Ante with the game's scripted policy."""
import contextlib
import hashlib
import http.client
import io
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swarm import arcade, gamesvc, hill as hl, play, solo, trail as tr  # noqa: E402
from swarm.sandbox import tagged_processes  # noqa: E402

PASS = "a test passcode for the arcade 41c"
PASS_HASH = hashlib.sha256(PASS.encode()).hexdigest()
REAL_ANTE = Path.home() / ".ante" / "bin" / "ante"
FRONT = Path(__file__).resolve().parents[1] / "front-solo"


def fake_ante(dirpath: Path) -> Path:
    path = dirpath / "ante"
    path.write_text('#!/bin/sh\nif [ "$1" = "--version" ]; then echo "ante 0.3.0"; exit 0; fi\n'
                    f'exec {sys.executable} -c "import time; time.sleep(120)"\n')
    path.chmod(0o755)
    return path


class ServiceTests(unittest.TestCase):
    def call(self, svc, token, op, arg=None):
        """Run the client exactly as an agent would, from a child process of ours (a list arg is several words)."""
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            (Path(d) / "trail").write_text(gamesvc.client_source("trail", tr.OPS, tr.USAGE))
            (Path(d) / ".trail.json").write_text(json.dumps({"socket": str(svc.socket_path), "token": token}))
            argv = [sys.executable, "trail", op] + (arg if isinstance(arg, list) else [arg] if arg else [])
            out = subprocess.run(argv, cwd=d, capture_output=True, text=True, timeout=20)
            return json.loads(out.stdout)

    def test_tokens_are_honoured_only_from_their_process_tree(self):
        m = tr.TrailModel(tr.params("s", 1, 2), ["you", "ante"])
        svc = gamesvc.GameService(m, "trail")
        svc.start()
        self.addCleanup(svc.stop)
        token = svc.register("ante")
        svc.bind("ante", os.getpid())   # our children descend from us
        look = self.call(svc, token, "look")
        self.assertEqual(look["heading"], "W")
        self.assertEqual(self.call(svc, token, "turn", "L")["next_heading"], "S")
        planned = self.call(svc, token, "plan", "SSLR")
        self.assertEqual((planned["plan"], m.by["ante"].pending), ("SSLR", None))
        self.assertIsInstance(planned["safe"], int)
        self.assertEqual(self.call(svc, token, "turn", "L")["plan"], "")
        self.assertEqual(self.call(svc, "not-a-token", "look"), {"error": "forbidden", "done": False})
        self.assertIn("usage", self.call(svc, token, "fly")["error"])
        # bound to another tree (pid 1's children are not ours): a stolen token changes nothing
        svc.bind("ante", 2 if os.getpid() != 2 else 3)
        self.assertEqual(self.call(svc, token, "turn", "R")["error"], "forbidden")
        self.assertEqual(svc.stolen, 1)
        self.assertEqual(m.by["ante"].pending, "S")

    def test_a_team_notes_and_views_through_the_client(self):
        m = tr.TrailModel(tr.params("l", 1, 3), ["you", "a01", "a02"], ["you", "swarm", "swarm"])
        gate = threading.Event()
        svc = gamesvc.GameService(m, "trail", gate=gate, free_ops=arcade.TrailGame.free_ops)
        svc.start()
        self.addCleanup(svc.stop)
        token = svc.register("a01")
        svc.bind("a01", os.getpid())
        self.assertEqual(self.call(svc, token, "note", ["claim", "12,5", "for", "me"])["notes"], ["a01 t0: claim 12,5 for me"])
        self.assertEqual(self.call(svc, token, "team")["team"], "swarm")   # notes and the team view need no go
        self.assertIn("usage", self.call(svc, token, "note")["error"])     # the client wants some text
        self.assertFalse(gate.is_set())

    def test_the_gate_holds_acting_ops_until_the_start(self):
        m = hl.HillModel(1, ["you", "ante"])
        gate = threading.Event()
        svc = gamesvc.GameService(m, "hill", gate=gate)
        svc.start()
        self.addCleanup(svc.stop)
        token = svc.register("ante")
        svc.bind("ante", os.getpid())
        threading.Timer(0.6, gate.set).start()
        t = time.monotonic()
        self.assertEqual(svc._handle({"token": token, "op": "look"}, os.getpid())[0], 200)
        self.assertLess(time.monotonic() - t, 0.5)                     # looking is never held
        status, out = svc._handle({"token": token, "op": "drop", "arg": "0 0"}, os.getpid())
        self.assertGreaterEqual(time.monotonic() - t, 0.5)             # dropping waited for the go
        self.assertEqual((status, out["ok"]), (200, True))

    def test_the_client_refuses_bad_arity_without_a_call(self):
        src = gamesvc.client_source("hill", hl.OPS, hl.USAGE)
        with tempfile.TemporaryDirectory(dir="/tmp") as d:
            (Path(d) / "hill").write_text(src)
            out = subprocess.run([sys.executable, "hill", "drop", "3"], cwd=d, capture_output=True, text=True)
            self.assertEqual(out.returncode, 2)
            self.assertEqual(json.loads(out.stdout)["error"], hl.USAGE)


# ---- HTTP ----

class Server:
    def __init__(self, config, tmp: Path, games=None):
        (tmp / "runs").mkdir(exist_ok=True)
        self.manager = play.PlayManager(config)
        self.arcade = arcade.Arcade(self.manager, games)
        router = arcade.Router(play.PlayAPI(self.manager), self.arcade)
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), solo.handler_for(solo.Library(tmp), FRONT, None, router))
        self.httpd.daemon_threads = True
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.port = self.httpd.server_port
        self.base = f"http://127.0.0.1:{self.port}"
        self.bodies: list[str] = []

    def close(self):
        for r in list(self.arcade.rounds.values()):
            self.arcade.give_up(r)
        for r in list(self.manager.rounds.values()):
            self.manager.give_up(r)
        deadline = time.monotonic() + 40
        while time.monotonic() < deadline and any(r.end_event is None for r in self.arcade.rounds.values()):
            time.sleep(0.1)
        self.httpd.shutdown()
        self.httpd.server_close()

    def call(self, method, path, body=None, headers=None):
        data = None if body is None else json.dumps(body).encode()
        req = urllib.request.Request(self.base + path, data=data, method=method,
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=15) as res:
                text, status = res.read().decode(), res.status
        except urllib.error.HTTPError as error:
            text, status = error.read().decode(), error.code
        self.bodies.append(text)
        return status, json.loads(text) if text else None

    def start(self, game, mode="single", count=1, passcode=PASS):
        return self.call("POST", f"/api/play/{game}/rounds", {"passcode": passcode, "rival": {"mode": mode, "count": count}})


class Events:
    def __init__(self, port, path):
        self.events: list = []
        self.done = threading.Event()
        self.conn = http.client.HTTPConnection("127.0.0.1", port, timeout=180)
        self.conn.request("GET", path)
        self.res = self.conn.getresponse()
        self.status = self.res.status
        threading.Thread(target=self._read, daemon=True).start()

    def _read(self):
        kind = None
        try:
            for line in self.res:
                line = line.decode()
                if line.startswith("event: "):
                    kind = line[7:].strip()
                elif line.startswith("data: "):
                    self.events.append((kind, json.loads(line[6:])))
                    if kind == "end":
                        break
        except (OSError, ValueError):
            pass
        finally:
            self.done.set()

    def wait(self, pred, timeout=60):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if any(pred(k, d) for k, d in list(self.events)):
                return True
            time.sleep(0.02)
        return False

    def end(self, timeout=120):
        self.done.wait(timeout)
        return next((d for k, d in self.events if k == "end"), None)


class ArcadeTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix="arcade-", dir="/tmp")
        self.tmp = Path(self._tmp.name)
        self.addCleanup(self._tmp.cleanup)
        self.out = io.StringIO()
        stack = contextlib.ExitStack()
        stack.enter_context(contextlib.redirect_stdout(self.out))
        stack.enter_context(contextlib.redirect_stderr(self.out))
        self.addCleanup(stack.close)

    def server(self, games=None, **over):
        cfg = dict(ante=fake_ante(self.tmp), passcode_sha256=PASS_HASH, provider="standin", data=self.tmp / "play",
                   round_timeout_s=60)
        cfg.update(over)
        s = Server(play.PlayConfig(**cfg), self.tmp, games)
        self.addCleanup(s.close)
        return s

    def assert_no_leak(self, s):
        everything = "".join(s.bodies) + self.out.getvalue()
        for f in (self.tmp / "play").glob("*"):
            everything += f.read_text()
        self.assertNotIn(PASS, everything)
        self.assertNotIn(PASS_HASH, everything)


class AdmissionTests(ArcadeTestCase):
    def test_status_names_the_game_and_its_shapes(self):
        s = self.server()
        for game in ("trail", "hill"):
            status, st = s.call("GET", f"/api/play/{game}/status")
            self.assertEqual(status, 200)
            self.assertEqual((st["game"], st["live"]), (game, True))
        trail, hill = s.call("GET", "/api/play/trail/status")[1], s.call("GET", "/api/play/hill/status")[1]
        self.assertEqual((trail["swarm_style"], trail["sizes"], trail["max_antes"]), ("shared", [1, 3], 3))
        self.assertEqual((hill["swarm_style"], hill["sizes"], hill["max_antes"]), ("pooled", [1, 4], 4))
        self.assertEqual(trail["counts"], {"single": [1], "swarm": [3]})
        self.assertEqual(hill["counts"], {"single": [1], "swarm": [4]})
        self.assertEqual(s.call("GET", "/api/play/pong/status")[0], 404)
        self.assertEqual(s.call("GET", "/api/play/status")[0], 200)   # the live maze is still there

    def test_the_passcode_comes_first_and_shapes_are_checked(self):
        s = self.server()
        self.assertEqual(s.start("trail", passcode="nope"), (403, {"error": "wrong_passcode"}))
        self.assertEqual(s.arcade.rounds, {})
        self.assertEqual(s.start("hill", mode="swarm", count=16)[0], 422)
        self.assertEqual(s.start("hill", mode="council", count=3)[0], 422)
        self.assertEqual(s.start("hill", mode="swarm", count=3), (422, {"error": "bad_count"}))   # hill offers x4
        self.assertEqual(s.start("trail", mode="swarm", count=4), (422, {"error": "bad_count"}))  # trail offers x3
        self.assertEqual(s.call("POST", "/api/play/trail/rounds", {"passcode": PASS})[0], 422)
        for _ in range(4):
            s.start("trail", passcode="nope")
        self.assertEqual(s.start("trail")[0], 429)   # the maze's per-address limit covers the arcade too
        self.assert_no_leak(s)

    def test_one_round_at_a_time_across_maze_and_arcade(self):
        s = self.server()
        status, b = s.start("trail")
        self.assertEqual(status, 201)
        self.assertIs(s.manager.active, s.arcade.rounds[b["round"]])
        self.assertEqual(s.start("hill")[0], 409)
        # a maze round queues behind the arcade round and starts when it ends
        status, mb = s.call("POST", "/api/play/rounds", {"passcode": PASS, "size": "s", "rival": {"mode": "single", "count": 1}})
        self.assertEqual((status, mb["queue"]), (201, 1))
        self.assertEqual(s.call("DELETE", f"/api/play/trail/rounds/{b['round']}", headers={"X-Round-Token": b["token"]})[0], 200)
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline and s.manager.active is not s.manager.rounds.get(mb["round"]):
            time.sleep(0.1)
        self.assertIs(s.manager.active, s.manager.rounds[mb["round"]])

    def test_tokens_rounds_and_acts_are_checked(self):
        s = self.server()
        status, b = s.start("hill")
        rid = b["round"]
        self.assertEqual(s.call("POST", f"/api/play/hill/rounds/{rid}/act", {"col": 0, "rot": 0}, {"X-Round-Token": "x" * 32})[0], 403)
        self.assertEqual(s.call("POST", f"/api/play/trail/rounds/{rid}/act", {"dir": "N"}, {"X-Round-Token": b["token"]})[0], 404)
        self.assertEqual(s.call("POST", "/api/play/hill/rounds/ffffffffffff/act", {"col": 0}, {"X-Round-Token": b["token"]})[0], 404)
        # the fake Ante never looks, so the round waits in "ready": no act counts yet
        self.assertEqual(s.call("POST", f"/api/play/hill/rounds/{rid}/act", {"col": 0, "rot": 0}, {"X-Round-Token": b["token"]})[0], 409)


@unittest.skipUnless(REAL_ANTE.exists(), "the real Ante binary is not installed")
class StandinRoundTests(ArcadeTestCase):
    """Whole rounds with real Ante processes driven by the scripted model (the games' stand-in policies)."""

    def run_round(self, game, games, mode, count, act):
        s = self.server(games=games, ante=REAL_ANTE)
        status, b = s.start(game, mode=mode, count=count)
        self.assertEqual(status, 201)
        reader = Events(s.port, f"/api/play/{game}/rounds/{b['round']}/events?t={b['token']}")
        self.addCleanup(reader.conn.close)
        self.assertTrue(reader.wait(lambda k, d: k == "state" and d["phase"] == "running", 60), "the clock never started")
        r = s.arcade.rounds[b["round"]]
        act(s, b)
        end = reader.end(120)
        self.assertIsNotNone(end)
        deadline = time.monotonic() + 30
        while r.cleanup == {} and time.monotonic() < deadline:
            time.sleep(0.1)
        self.assertTrue(r.cleanup["killed"] and r.cleanup["sandboxes_removed"], r.cleanup)
        self.assertEqual(tagged_processes(f"g{r.id}-", prefix=True), {})
        self.assertEqual(hashlib.sha256(f"{end['seed']}:{end['nonce']}".encode()).hexdigest(), b["seed_hash"])
        self.assertTrue(all(c.result.get("requests", 0) > 0 for c in r.cells), [c.result for c in r.cells])
        self.assert_no_leak(s)
        self.assertIn(b["round"], (self.tmp / "play" / "rounds.jsonl").read_text())
        return s, r, reader, end

    def test_trail_one_ante_and_the_human_steers(self):
        def act(s, b):
            # The bad direction goes first: steering can end the round (the human crashes), after which every act is 410.
            self.assertEqual(s.call("POST", f"/api/play/trail/rounds/{b['round']}/act", {"dir": "up"}, {"X-Round-Token": b["token"]})[0], 422)
            for d in "NESW":
                status, out = s.call("POST", f"/api/play/trail/rounds/{b['round']}/act", {"dir": d}, {"X-Round-Token": b["token"]})
                self.assertIn(status, (200, 410))
                time.sleep(0.2)

        games = {"trail": arcade.TrailGame(size="m", tick_s=0.1, clock_ticks=60), "hill": arcade.HillGame()}
        s, r, reader, end = self.run_round("trail", games, "single", 1, act)
        self.assertIn(end["reason"], ("clock", "out"))
        self.assertEqual({p["who"] for p in end["frame"]["players"]}, {"you", "ante"})
        frames = [d for k, d in reader.events if k == "frame"]
        self.assertTrue(frames)
        self.assertEqual([f["tick"] for f in frames], sorted(f["tick"] for f in frames))
        self.assertTrue(any(k == "call" and d["op"] == "turn" for k, d in reader.events))

    def test_hill_four_antes_drop_and_the_clock_ends_it(self):
        def act(s, b):
            # The refused drop goes first, while the round is certainly running (a refused drop is not a drop).
            self.assertEqual(s.call("POST", f"/api/play/hill/rounds/{b['round']}/act", {"col": 99, "rot": 0}, {"X-Round-Token": b["token"]})[0], 422)
            status, out = s.call("POST", f"/api/play/hill/rounds/{b['round']}/act", {"col": 0, "rot": 0}, {"X-Round-Token": b["token"]})
            self.assertEqual((status, out["ok"]), (200, True))

        games = {"trail": arcade.TrailGame(), "hill": arcade.HillGame(clock_s=6, fuse_s=2)}
        s, r, reader, end = self.run_round("hill", games, "swarm", 4, act)
        self.assertIn(end["reason"], ("clock", "out"))
        self.assertEqual(set(end["stats"]["drops"]), {"you", "a01", "a02", "a03", "a04"})
        self.assertGreaterEqual(end["stats"]["drops"]["you"], 2)   # one by hand, then the fuse
        self.assertTrue(all(end["stats"]["drops"][w] > 0 for w in ("a01", "a02", "a03", "a04")))
        self.assertEqual(set(end["stats"]["refused"]), {"you", "a01", "a02", "a03", "a04"})   # ids and spacing held

    def test_trail_a_swarm_of_three_plays_as_one_team(self):
        games = {"trail": arcade.TrailGame(tick_s=0.1, clock_ticks=60), "hill": arcade.HillGame()}
        s, r, reader, end = self.run_round("trail", games, "swarm", 3, lambda s, b: None)
        self.assertEqual(end["frame"]["cols"], tr.SIZES["l"][0])                  # a swarm plays the big grid
        teams = {t["team"]: t for t in end["frame"]["teams"]}
        self.assertEqual(teams["swarm"]["members"], ["a01", "a02", "a03"])
        self.assertEqual(end["stats"]["teams"]["swarm"], sum(end["stats"]["tokens"][w] for w in ("a01", "a02", "a03")))
        self.assertEqual(end["stats"]["swarm_style"], "shared")
        self.assertTrue(all(end["stats"]["ops"][w].get("note") == 1 for w in ("a01", "a02", "a03")), end["stats"]["ops"])
        self.assertEqual(end["stats"]["notes"], {"swarm": 3})
        state = next(d for k, d in reader.events if k == "state")
        self.assertEqual(state["swarm_style"], "shared")
        during = json.dumps([d for k, d in reader.events if k != "end"])
        self.assertNotIn("scripted stand-in", during)                              # private during the round
        self.assertEqual(len(end["notes"]["swarm"]), 3)                            # revealed at the end
        row = json.loads((self.tmp / "play" / "rounds.jsonl").read_text().splitlines()[-1])
        self.assertEqual(len(row["notes"]["swarm"]), 3)
        self.assertIn(end["winner"], ("you", "swarm", None))

    def test_hill_rows_pool_and_the_swarms_garbage_goes_to_you(self):
        def act(s, b):
            pass

        games = {"trail": arcade.TrailGame(), "hill": arcade.HillGame(clock_s=8, fuse_s=2)}
        s, r, reader, end = self.run_round("hill", games, "swarm", 4, act)
        pooled = end["stats"]["pooled"]
        self.assertEqual(pooled["swarm"]["rows"], sum(end["stats"]["rows"][w] for w in ("a01", "a02", "a03", "a04")))
        self.assertEqual(pooled["swarm"]["wells"], 4)
        teams = {t["team"]: t for t in end["frame"]["teams"]}
        self.assertEqual((teams["swarm"]["members"], teams["you"]["members"]), (["a01", "a02", "a03", "a04"], ["you"]))
        you = end["frame"]["wells"][0]
        self.assertLessEqual(you["got"] + you["pending"], sum(end["stats"]["sent"][w] for w in ("a01", "a02", "a03", "a04")))
        self.assertEqual(end["stats"]["swarm_style"], "pooled")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class LiveFrontTests(unittest.TestCase):
    """front-solo/arcadelive.js: opponent keys come from the status sizes, in live and scripted mode alike."""
    SCRIPT = """
const L = require(process.argv[process.argv.length - 1] + '/arcadelive.js');
const calls = [];
const store = { v: { 'maze-live:passcode': 'pw' }, getItem(k) { return this.v[k] || null; }, setItem(k, v) { this.v[k] = v; }, removeItem(k) { delete this.v[k]; } };
const reply = (status, body) => ({ status, ok: status < 300, json: async () => body });
(async () => {
  const out = {};
  out.defaults = [L.shape('trail', null), L.shape('hill', { error: 'absent' })];
  out.server = L.shape('trail', { swarm_style: 'shared', sizes: [1, 3] });
  out.bad = L.shape('hill', { swarm_style: 'nope', sizes: [3, 'x'] });
  out.labels = [1, 3, 4].map(L.label);
  const c = new L.Client({ game: 'trail', storage: store, now: () => 0, EventSource: null,
    fetch: async (url, o) => { calls.push([url, o && o.body ? JSON.parse(o.body) : null]);
      return url.endsWith('/status') ? reply(200, { live: true, swarm_style: 'shared', sizes: [1, 3] }) : reply(201, { round: 'abcdefabcdef', token: 't' }); } });
  await c.status();
  out.shape = c.shape;
  out.refused = await c.start({ count: 4 });
  out.started = await c.start({ count: 3 });
  out.posted = calls.filter(([u]) => u.endsWith('/rounds')).map(([, b]) => b.rival);
  c.handle('end', { winner: 'swarm', reason: 'clock', notes: { swarm: ['a01 t3: claim 4,5', 7] } });
  out.notes = c.r.notes;
  console.log(JSON.stringify(out));
})();
"""

    def test_sizes_and_labels_come_from_the_status(self):
        r = subprocess.run(["node", "-e", self.SCRIPT, str(FRONT)], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        out = json.loads(r.stdout)
        self.assertEqual([(d["style"], d["sizes"], d["server"]) for d in out["defaults"]],
                         [("shared", [1, 3], False), ("pooled", [1, 4], False)])
        self.assertEqual((out["server"]["sizes"], out["server"]["server"]), ([1, 3], True))
        self.assertEqual(out["bad"]["sizes"], [1, 4])   # no 1 offered: the default stands
        self.assertEqual(out["labels"], ["ante ×1", "swarm ×3", "swarm ×4"])
        self.assertEqual(out["shape"]["sizes"], [1, 3])
        self.assertEqual(out["refused"], {"error": "not offered"})
        self.assertEqual(out["started"], {"ok": True})
        self.assertEqual(out["posted"], [{"mode": "swarm", "count": 3}])
        self.assertEqual(out["notes"], {"swarm": ["a01 t3: claim 4,5"]})   # revealed at the end, strings only
        # the server offers what the front labels: trail x3, hill x4
        self.assertEqual([arcade.TrailGame.sizes, arcade.HillGame.sizes], [(1, 3), (1, 4)])


class GameDefaultsTests(unittest.TestCase):
    def test_live_defaults_suit_a_model_that_takes_seconds(self):
        t, h = arcade.TrailGame(), arcade.HillGame()
        self.assertEqual((t.tick_s, t.clock_ticks, t.limit_s()), (0.8, 225, 180.0))
        self.assertIn("./trail plan", t.prompt("single", 1))
        self.assertEqual((h.fuse_s, h.ante_fuse_s, h.min_drop_s), (10.0, 12.0, 1.0))   # the human's page fuse is unchanged
        self.assertIsInstance(h.new_model(1, ["you", "ante"]), hl.LiveHill)
        self.assertIn("<id>", h.prompt("single", 1))


class PageTests(unittest.TestCase):
    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="arcade-pages-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, True))
        (tmp / "runs").mkdir()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), solo.handler_for(solo.Library(tmp), FRONT,
                                                                              FRONT.parent / "front-maze"))
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()
        self.addCleanup(self.httpd.server_close)
        self.addCleanup(self.httpd.shutdown)
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def get(self, path):
        opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,), {"redirect_request": lambda *a: None}))
        try:
            with opener.open(self.base + path) as r:
                return r.status, r.headers, r.read().decode()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read().decode()

    def test_the_cartridges_are_served_under_the_console_csp(self):
        _, console, _ = self.get("/replay")
        for path, script, core in (("/", "/trail.js", "/trail-core.js"), ("/trail/", "/trail.js", "/trail-core.js"),
                                   ("/hill/", "/hill.js", "/hill-core.js")):
            status, headers, html = self.get(path)
            self.assertEqual(status, 200, path)
            csp = headers["Content-Security-Policy"]
            self.assertEqual(csp, console["Content-Security-Policy"], path)
            self.assertIn("script-src 'self'", csp)
            self.assertNotIn("unsafe-inline", csp)
            for src in (script, core, "/arcade.js", "/arcadelive.js", "/arcade.css"):
                self.assertIn(f'"{src}"', html, path)
            for stop in ('href="/maze/"', "replay"):
                self.assertIn(stop, html, path)
        for path in ("/trail.js", "/hill.js", "/trail-core.js", "/hill-core.js", "/arcade.js", "/arcadelive.js"):
            status, headers, _ = self.get(path)
            self.assertEqual((status, headers["Content-Type"].split(";")[0]), (200, "text/javascript"), path)
        self.assertEqual(self.get("/trail")[:2][0], 308)
        self.assertEqual(self.get("/trail")[1]["Location"], "/trail/")
        self.assertEqual(self.get("/hill")[1]["Location"], "/hill/")
        self.assertEqual(self.get("/trail/x")[0], 404)

    def test_every_page_dials_all_three_cartridges_and_keeps_the_replays(self):
        for path in ("/", "/hill/", "/maze/"):
            html = self.get(path)[2]
            for stop in ("/trail/", "/hill/", "/maze/"):
                if not (path == "/" and stop == "/trail/") and not (path == "/hill/" and stop == "/hill/") and not (path == "/maze/" and stop == "/maze/"):
                    self.assertIn(f'href="{stop}"', html, (path, stop))
            self.assertIn('href="/replay', html, path)
        replay = self.get("/replay")[2]
        self.assertIn("/solo.js", replay)
        self.assertIn('href="/trail/"', replay)
        self.assertIn('data-board="hill"', replay)   # the stored runs' hill replay board is still there


if __name__ == "__main__":
    unittest.main()
