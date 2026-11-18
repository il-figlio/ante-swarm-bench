import functools
import http.client
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
import tomllib
import unittest
from pathlib import Path
from unittest import mock

from helpers import ROOT
from swarm import maze as mz
from swarm.tasks import load_task

TASK_IDS = ["maze-branchy-shared", "maze-branchy-own", "maze-corridor-shared", "maze-line-shared", "maze-large-shared",
            "maze-budget-shared"]
CORE_JS = ROOT / "front-maze" / "maze-core.js"
NODE = shutil.which("node")


class UnixConnection(http.client.HTTPConnection):
    def __init__(self, path, timeout=10):
        super().__init__("localhost", timeout=timeout)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def raw(sock: str, method: str, path: str, body: bytes | None = None, headers=None, timeout=10):
    """(status, body bytes) for one request over the service's Unix socket."""
    conn = UnixConnection(sock, timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        response = conn.getresponse()
        return response.status, response.read()
    finally:
        conn.close()


def task_params(task_id: str) -> dict:
    return tomllib.loads((ROOT / "tasks" / task_id / "task.toml").read_text())["maze"]


def post(sock: str, token, op: str, arg=None):
    body = json.dumps({"token": token, "op": op, "arg": arg}).encode()
    status, data = raw(sock, "POST", "/call", body, {"Content-Type": "application/json"})
    return status, json.loads(data)


def components(maze: mz.Maze) -> tuple[int, int]:
    """(cells reachable from start, open edges): a perfect maze has all cells and cells - 1 edges."""
    seen, queue, edges = {maze.start}, [maze.start], 0
    for i in queue:
        for _, j in maze.neighbours(i):
            edges += 1
            if j not in seen:
                seen.add(j)
                queue.append(j)
    return len(seen), edges // 2


class ParamsTest(unittest.TestCase):
    def test_defaults_fill(self):
        p = mz.validate_params({"cols": 10})
        self.assertEqual(p["cols"], 10)
        self.assertEqual(p["generator"], "prim")
        self.assertEqual(p["step_limit"], 4000)
        self.assertEqual(set(p), set(mz.PARAMS_DEFAULTS))

    def test_bad_values_raise(self):
        for bad in ({"cols": 3}, {"rows": 129}, {"cols": "40"}, {"cols": True}, {"seed": -1}, {"seed": 2 ** 32},
                    {"generator": "kruskal"}, {"loops": 0.31}, {"loops": -0.1}, {"loops": "x"}, {"start": "middle"},
                    {"map": "both"}, {"step_limit": 0}, {"call_budget": -1}, {"colz": 5}):
            with self.assertRaises(ValueError, msg=str(bad)):
                mz.validate_params(bad)

    def test_loops_accepts_int_zero(self):
        self.assertEqual(mz.validate_params({"loops": 0})["loops"], 0.0)


class GeneratorTest(unittest.TestCase):
    def test_deterministic_and_seed_sensitive(self):
        for gen in mz.GENERATORS:
            a = mz.generate({"generator": gen, "cols": 20, "rows": 12, "seed": 7})
            b = mz.generate({"generator": gen, "cols": 20, "rows": 12, "seed": 7})
            c = mz.generate({"generator": gen, "cols": 20, "rows": 12, "seed": 8})
            self.assertEqual(bytes(a.walls), bytes(b.walls))
            self.assertNotEqual(bytes(a.walls), bytes(c.walls))

    def test_perfect_maze_without_loops(self):
        for gen in mz.GENERATORS:
            for start in ("center", "corner"):
                m = mz.generate({"generator": gen, "loops": 0, "cols": 23, "rows": 14, "seed": 3, "start": start})
                reach, edges = components(m)
                self.assertEqual(reach, m.cols * m.rows, (gen, start))
                self.assertEqual(edges, m.cols * m.rows - 1, (gen, start))

    def test_loops_add_openings_and_stay_connected(self):
        m = mz.generate({"loops": 0.2, "cols": 23, "rows": 14, "seed": 3})
        reach, edges = components(m)
        self.assertEqual(reach, 23 * 14)
        self.assertGreater(edges, 23 * 14 - 1)

    def test_walls_are_symmetric_and_bounded(self):
        m = mz.generate({"cols": 16, "rows": 9, "seed": 11, "loops": 0.3})
        for i in range(m.cols * m.rows):
            x, y = i % m.cols, i // m.cols
            for name, bit, dx, dy in mz.DIRS:
                inside = 0 <= x + dx < m.cols and 0 <= y + dy < m.rows
                if not inside:
                    self.assertTrue(m.walls[i] & bit)
                else:
                    j = (y + dy) * m.cols + x + dx
                    self.assertEqual(bool(m.walls[i] & bit), bool(m.walls[j] & {1: 4, 2: 8, 4: 1, 8: 2}[bit]))

    def test_start_and_exit(self):
        self.assertEqual(mz.generate({"cols": 10, "rows": 6}).start, 3 * 10 + 5)
        self.assertEqual(mz.generate({"cols": 10, "rows": 6, "start": "corner"}).start, 0)
        self.assertEqual(mz.generate({"cols": 10, "rows": 6}).exit, 59)

    def test_shortest_route_is_valid_and_shortest(self):
        m = mz.generate({"cols": 30, "rows": 20, "seed": 5, "loops": 0.1})
        route = m.shortest_route()
        self.assertEqual((route[0], route[-1]), (m.start, m.exit))
        for i, j in zip(route, route[1:]):
            self.assertIn(j, [c for _, c in m.neighbours(i)])
        self.assertEqual(len(set(route)), len(route))
        # No walk can be shorter than the BFS layer of the exit: check against a second BFS by layers.
        layer, frontier, seen = 0, [m.start], {m.start}
        while m.exit not in frontier:
            frontier = [j for i in frontier for _, j in m.neighbours(i) if not (j in seen or seen.add(j))]
            layer += 1
        self.assertEqual(len(route), layer + 1)

    def test_stats(self):
        m = mz.generate({"cols": 30, "rows": 20, "seed": 5, "loops": 0})
        s = m.stats()
        self.assertEqual(s["cells"], 600)
        self.assertEqual(s["shortest"], len(m.shortest_route()))
        self.assertGreater(s["dead_ends"], 0)
        self.assertGreater(s["branch_cells"], 0)

    def test_generators_differ_in_character(self):
        prim = mz.generate({"cols": 40, "rows": 25, "loops": 0}).stats()
        corridor = mz.generate({"cols": 40, "rows": 25, "loops": 0, "generator": "backtracker"}).stats()
        self.assertGreater(prim["dead_ends"], corridor["dead_ends"])


@unittest.skipUnless(NODE, "node is not installed")
class JsParityTest(unittest.TestCase):
    def js(self, params: dict, sizes=(1, 4, 16)) -> dict:
        script = (f"const m=require({json.dumps(str(CORE_JS))});const p={json.dumps(params)};"
                  f"const g=m.generate(p);const out={{walls:Buffer.from(g.walls).toString('hex'),sim:{{}}}};"
                  f"for(const n of {json.dumps(list(sizes))})out.sim[n]=m.simulate(p,n);"
                  "console.log(JSON.stringify(out));")
        done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_all_five_task_configs(self):
        for task_id in TASK_IDS:
            params = task_params(task_id)
            js = self.js(params)
            self.assertEqual(js["walls"], bytes(mz.generate(params).walls).hex(), task_id)
            for n in (1, 4, 16):
                py = json.loads(json.dumps(mz.simulate(params, n)))
                self.assertEqual(js["sim"][str(n)], py, (task_id, n))

    def test_other_shapes(self):
        for params in ({"cols": 4, "rows": 4, "seed": 0, "start": "corner", "map": "own"},
                       {"cols": 17, "rows": 9, "seed": 4294967295, "generator": "backtracker", "loops": 0.3},
                       {"cols": 12, "rows": 12, "seed": 99, "map": "own", "call_budget": 40},
                       {"cols": 20, "rows": 20, "seed": 5, "step_limit": 30}):
            js = self.js(params, sizes=(1, 5))
            self.assertEqual(js["walls"], bytes(mz.generate(params).walls).hex(), params)
            for n in (1, 5):
                self.assertEqual(js["sim"][str(n)], json.loads(json.dumps(mz.simulate(params, n))), (params, n))

    def test_rng_matches(self):
        r = mz.mulberry32(2026)
        py = [r() for _ in range(50)]
        done = subprocess.run([NODE, "-e", f"const m=require({json.dumps(str(CORE_JS))});const r=m.rng(2026);"
                               "console.log(JSON.stringify(Array.from({length:50},()=>r())))"],
                              capture_output=True, text=True, timeout=30)
        self.assertEqual(json.loads(done.stdout), py)


class SimulateTest(unittest.TestCase):
    def test_solo_reaches_exit_with_credit_for_the_whole_route(self):
        p = {"cols": 14, "rows": 9, "seed": 3, "map": "own"}
        r = mz.simulate(p, 1)
        self.assertEqual(r["exit_tick"], r["calls"])
        self.assertEqual(r["exit_agent"], 0)
        self.assertEqual(r["per_agent_calls"], [r["calls"]])
        self.assertEqual(sum(r["credit"].values()), len(mz.generate(p).shortest_route()))

    def test_swarm_is_deterministic_and_splits_credit(self):
        p = {"cols": 30, "rows": 20, "seed": 9}
        a, b = mz.simulate(p, 8), mz.simulate(p, 8)
        self.assertEqual(a, b)
        self.assertEqual(sum(a["per_agent_calls"]), a["calls"])
        self.assertGreater(len(a["credit"]), 1)

    def test_agents_start_four_per_tick(self):
        r = mz.simulate({"cols": 30, "rows": 20, "seed": 9, "step_limit": 500}, 9)
        self.assertTrue(all(c > 0 for c in r["per_agent_calls"]))
        # Agent 8 starts at tick 2, so it has made at most (exit_tick - 2) calls.
        self.assertLessEqual(r["per_agent_calls"][8], r["exit_tick"] - 2)

    def test_budget_stops_the_run(self):
        r = mz.simulate({"cols": 30, "rows": 20, "seed": 9, "call_budget": 25}, 4)
        self.assertIsNone(r["exit_tick"])
        self.assertLessEqual(r["calls"], 25 + 4)   # each agent may see one refusal before it stops

    def test_swarm_beats_solo_on_branchy_and_not_much_on_corridor(self):
        gain = {}
        for gen in mz.GENERATORS:
            p = {"generator": gen}
            gain[gen] = mz.simulate({**p, "map": "own"}, 1)["exit_tick"] / mz.simulate(p, 16)["exit_tick"]
        self.assertGreater(gain["prim"], 2)
        self.assertGreater(gain["prim"], gain["backtracker"])


class ExplorerTest(unittest.TestCase):
    def test_first_call_is_look(self):
        self.assertEqual(mz.Explorer(1, 0, True).next(None), {"op": "bash", "command": "./maze look"})

    def test_error_and_done_end_the_run(self):
        e = mz.Explorer(1, 0, True)
        self.assertEqual(e.next('{"error":"wall","done":false}')["op"], "final")
        self.assertIn("wall", e.next('{"error":"wall","done":false}')["text"])
        self.assertEqual(mz.Explorer(1, 0, True).next('{"at":[1,1],"open":[],"new":["E"],"exit":false,"steps":1,"done":true}')["op"], "final")

    def test_own_backtracks_and_shared_asks_for_an_edge(self):
        look = '{"at":[1,1],"open":["N","E"],"new":["E"],"exit":false,"steps":1,"done":false}'
        none = '{"at":[2,1],"open":["W"],"new":[],"exit":false,"steps":2,"done":false}'
        own = mz.Explorer(1, 0, False)
        self.assertEqual(own.next(look)["command"], "./maze move E")
        self.assertEqual(own.next(none)["command"], "./maze move W")
        self.assertEqual(own.next(none)["op"], "final")
        shared = mz.Explorer(1, 0, True)
        shared.next(look)
        self.assertEqual(shared.next(none)["command"], "./maze next-edge")
        self.assertEqual(shared.next('{"path":["W","N"],"to":[1,0],"done":false}')["command"], "./maze move W")
        self.assertEqual(shared.next(none)["command"], "./maze move N")
        self.assertEqual(shared.next('{"path":null,"done":false}')["op"], "final")


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.services = []

    def service(self, **over):
        params = {"cols": 12, "rows": 8, "seed": 4, **over}
        svc = mz.MazeService(mz.generate(params), params, Path(self.tmp.name) / f"maze-{len(self.services)}.jsonl")
        url = svc.start()
        # Calls in these tests come from this process: every token is bound to it.
        svc.register = functools.partial(svc.register, pid=os.getpid())
        self.services.append(svc)
        self.addCleanup(svc.stop)
        return svc, url

    def walk(self, url, token, cells, maze):
        """Send the moves that walk `cells` (consecutive neighbours) and return the last response."""
        res = None
        for i, j in zip(cells, cells[1:]):
            name = {-maze.cols: "N", 1: "E", maze.cols: "S", -1: "W"}[j - i]
            status, res = post(url, token, "move", name)
            self.assertEqual(status, 200)
            self.assertNotIn("error", res)
        return res

    def test_look_shape_and_wall_refusal(self):
        seed = next(s for s in range(50) if mz.generate({"cols": 12, "rows": 8, "seed": s}).walls[
            mz.generate({"cols": 12, "rows": 8, "seed": s}).start])
        svc, url = self.service(seed=seed)
        token = svc.register("a01", 0)
        status, res = post(url, token, "look")
        self.assertEqual(status, 200)
        m = svc.maze
        self.assertEqual(res["at"], [m.start % m.cols, m.start // m.cols])
        self.assertEqual(res["new"], res["open"])
        self.assertEqual((res["exit"], res["done"], res["steps"]), (False, False, 1))
        wall = next(n for n in "NESW" if n not in res["open"])
        status, res = post(url, token, "move", wall)
        self.assertEqual((status, res["error"]), (200, "wall"))
        self.assertFalse(svc.verdict("a01")["legal"])
        status, res = post(url, token, "move", "X")
        self.assertEqual(res["error"], "bad_arg")
        self.assertEqual(post(url, token, "dance")[1]["error"], "bad_op")
        self.assertEqual(post(url, token, "move", ["N"])[1]["error"], "bad_arg")

    def test_move_updates_new_and_exit(self):
        svc, url = self.service(map="own")
        token = svc.register("a01", 0)
        route = svc.maze.shortest_route()
        res = self.walk(url, token, route, svc.maze)
        self.assertTrue(res["exit"])
        self.assertTrue(res["done"])
        # walking the route explored exactly its cells (own map), so stepping back shows no new side along it
        back = post(url, token, "move", next(n for n in res["open"]))[1]
        self.assertNotIn("error", back)
        self.assertEqual(svc.summary()["unique_cells"], len(route) - 1)

    def test_step_limit(self):
        svc, url = self.service(step_limit=3)
        token = svc.register("a01", 0)
        results = [post(url, token, "look")[1] for _ in range(5)]
        self.assertNotIn("error", results[2])
        self.assertEqual([r.get("error") for r in results[3:]], ["step_limit", "step_limit"])
        v = svc.verdict("a01")
        self.assertFalse(v["within_limit"])
        self.assertFalse(v["passed"])
        self.assertEqual(v["calls"], 5)   # refused calls count
        # the verdict itself is not a call
        self.assertEqual(post(url, token, "verdict")[1]["calls"], 5)
        self.assertEqual(svc.verdict("a01")["calls"], 5)

    def test_budget_is_shared_across_agents(self):
        svc, url = self.service(call_budget=4)
        t1, t2 = svc.register("a01", 0), svc.register("a02", 1)
        got = [post(url, t, "look")[1] for t in (t1, t2, t1, t2)]
        self.assertTrue(all("error" not in r for r in got))
        self.assertEqual(post(url, t1, "look")[1]["error"], "budget")
        self.assertEqual(post(url, t2, "look")[1]["error"], "budget")
        self.assertFalse(svc.verdict("a02")["within_limit"])
        self.assertEqual(svc.summary()["calls"], 6)

    def test_forged_token_is_403_and_logged(self):
        svc, url = self.service()
        svc.register("a01", 0)
        for token in ("deadbeef", None, 7, ""):
            status, res = post(url, token, "look")
            self.assertEqual(status, 403)
            self.assertEqual(res["error"], "forbidden")
        status, _ = post(url, "00" * 16, "move", "N")
        self.assertEqual(status, 403)
        self.assertEqual(svc.summary()["forbidden"], 5)
        self.assertEqual(svc.summary()["calls"], 0)
        svc.stop()
        rows = [json.loads(line) for line in svc.log_path.read_text().splitlines()]
        self.assertEqual([r["error"] for r in rows], ["forbidden"] * 5)
        self.assertTrue(all(r["ok"] is False and r["agent"] is None for r in rows))

    def test_register_rejects_duplicates_and_tokens_are_unguessable(self):
        svc, _ = self.service()
        t = svc.register("a01", 0)
        self.assertEqual(len(t), 32)
        self.assertNotEqual(t, svc.register("a02", 1))
        with self.assertRaises(ValueError):
            svc.register("a01", 2)
        with self.assertRaises(ValueError):
            svc.register("a03", 1)

    def test_bad_requests_and_other_paths(self):
        svc, url = self.service()
        for method, path, data in (("GET", "/", None), ("GET", "/call", None), ("POST", "/walls", b"{}"),
                                   ("POST", "/call", b"not json"), ("POST", "/call", b"[1]")):
            status, body = raw(url, method, path, data)
            self.assertGreaterEqual(status, 400)
            self.assertNotIn("walls", body.decode())

    def test_concurrent_next_edge_never_double_claims(self):
        svc, url = self.service(cols=40, rows=25, seed=2026)
        scout = svc.register("a00", 0)
        explorer, last = mz.Explorer(2026, 0, True), None
        for _ in range(120):   # open up a map with many frontiers
            op, arg = mz.parse_command(explorer.next(last)["command"])
            last = json.dumps(post(url, scout, op, arg)[1])
        racers = 8
        tokens = [svc.register(f"r{k:02d}", k + 1) for k in range(racers)]
        for round_ in range(3):
            frontier = sum(1 for i in range(len(svc._model.seen)) if svc._model.seen[i] and svc._model._is_frontier(i))
            self.assertGreaterEqual(frontier, racers + 2)
            barrier = threading.Barrier(racers)
            out = [None] * racers

            def race(k):
                barrier.wait()
                out[k] = post(url, tokens[k], "next-edge")

            threads = [threading.Thread(target=race, args=(k,)) for k in range(racers)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
            self.assertTrue(all(status == 200 and res["path"] is not None for status, res in out))
            targets = [tuple(res["to"]) for _, res in out]
            self.assertEqual(len(set(targets)), racers, f"round {round_}: double claim {targets}")

    def explore(self, url, token, maze, leave=0):
        """Depth-first through the API, leaving `leave` cells unexplored. Returns where the agent stopped."""
        total, seen = maze.cols * maze.rows, {maze.start}
        names = {-maze.cols: "N", 1: "E", maze.cols: "S", -1: "W"}

        def visit(cell):
            for _, nxt in maze.neighbours(cell):
                if nxt in seen:
                    continue
                if len(seen) == total - leave:
                    return True
                seen.add(nxt)
                self.assertEqual(post(url, token, "move", names[nxt - cell])[0], 200)
                if visit(nxt):
                    return True
                post(url, token, "move", names[cell - nxt])
            return False

        visit(maze.start)

    def test_next_edge_prefers_unclaimed_then_falls_back_then_none(self):
        svc, url = self.service(cols=5, rows=4, seed=1, loops=0)
        scout, b, c = svc.register("s", 0), svc.register("b", 1), svc.register("c", 2)
        self.explore(url, scout, svc.maze, leave=1)
        first, second = post(url, b, "next-edge")[1], post(url, c, "next-edge")[1]
        self.assertIsNotNone(first["path"])
        self.assertEqual(second["to"], first["to"])   # the only frontier: handed out twice rather than leave c idle
        self.assertEqual(len(post(url, b, "map")[1]["claims"]), 2)
        # with two frontiers, the second agent gets the other one
        svc2, url2 = self.service(cols=12, rows=8, seed=1, loops=0)
        s2, x, y = svc2.register("s", 0), svc2.register("x", 1), svc2.register("y", 2)
        self.explore(url2, s2, svc2.maze, leave=60)
        gx, gy = post(url2, x, "next-edge")[1], post(url2, y, "next-edge")[1]
        frontiers = sum(1 for i in range(96) if i != svc2.maze.start and svc2._model.seen[i] and svc2._model._is_frontier(i))
        self.assertGreaterEqual(frontiers, 2)
        self.assertNotEqual(gx["to"], gy["to"])

    def test_next_edge_none_when_fully_explored(self):
        svc, url = self.service(cols=5, rows=4, seed=2, loops=0)
        scout = svc.register("s", 0)
        self.explore(url, scout, svc.maze)
        other = svc.register("o", 1)
        self.assertIsNone(post(url, other, "next-edge")[1]["path"])

    def test_done_flag_shared_vs_own(self):
        for mode, other_done in (("shared", True), ("own", False)):
            svc, url = self.service(map=mode)
            a, b = svc.register("a01", 0), svc.register("a02", 1)
            res = self.walk(url, a, svc.maze.shortest_route(), svc.maze)
            self.assertTrue(res["done"], mode)
            self.assertEqual(post(url, b, "look")[1]["done"], other_done, mode)
            self.assertEqual(svc.verdict("a01")["passed"], True, mode)
            self.assertEqual(svc.verdict("a02")["exit_reached"], other_done, mode)
            self.assertEqual(svc.verdict("a02")["passed"], other_done, mode)

    def test_own_map_next_edge_is_refused(self):
        svc, url = self.service(map="own")
        token = svc.register("a01", 0)
        self.assertEqual(post(url, token, "next-edge")[1]["error"], "own_map")
        self.assertTrue(svc.verdict("a01")["legal"])

    def test_map_lists_explored_cells_and_claims(self):
        svc, url = self.service(map="shared")
        a, b = svc.register("a01", 0), svc.register("a02", 1)
        look = post(url, a, "look")[1]
        post(url, a, "move", look["open"][0])
        post(url, b, "next-edge")
        res = post(url, b, "map")[1]
        self.assertEqual(len(res["cells"]), 2)
        self.assertEqual(len(res["claims"]), 1)
        self.assertEqual(res["claims"][0][2], "a02")

    def test_verdict_over_http_and_unknown_agent(self):
        svc, url = self.service()
        token = svc.register("a01", 0)
        status, res = post(url, token, "verdict")
        self.assertEqual(status, 200)
        self.assertEqual({k: res[k] for k in ("legal", "within_limit", "exit_reached", "passed", "calls")},
                         {"legal": True, "within_limit": True, "exit_reached": False, "passed": False, "calls": 0})
        self.assertFalse(svc.verdict("nobody")["passed"])

    def test_log_and_summary_files(self):
        svc, url = self.service(map="shared", cols=10, rows=6)
        a, b = svc.register("a01", 0), svc.register("a02", 1)
        self.walk(url, a, svc.maze.shortest_route(), svc.maze)
        post(url, b, "look")
        post(url, b, "move", "Q")
        svc.stop()
        rows = [json.loads(line) for line in svc.log_path.read_text().splitlines()]
        self.assertEqual(len(rows), len(svc.maze.shortest_route()) - 1 + 2)
        self.assertEqual(set(rows[0]), {"t", "agent", "op", "arg", "at", "ok"})
        self.assertEqual(set(rows[-1]), {"t", "agent", "op", "arg", "at", "ok", "error"})
        self.assertEqual(rows[-1]["error"], "bad_arg")
        self.assertEqual([r["t"] for r in rows], sorted(r["t"] for r in rows))
        summary = json.loads(svc.log_path.with_suffix(".json").read_text())
        self.assertEqual(summary["params"], svc.params)
        self.assertEqual(summary["exit"]["agent"], "a01")
        self.assertGreaterEqual(summary["exit"]["t"], 0)
        self.assertEqual(summary["calls"], len(rows))
        self.assertEqual(summary["route"], svc.maze.shortest_route())
        self.assertEqual(summary["credit"], {"a01": len(svc.maze.shortest_route())})
        self.assertEqual(summary["per_agent"]["a02"], {"calls": 2, "firsts": 0, "refused": 1})
        self.assertEqual(summary["shortest"], len(summary["route"]))
        svc.stop()   # idempotent


class CliEndToEndTest(unittest.TestCase):
    def run_solo(self, mode: str):
        params = {"cols": 10, "rows": 7, "seed": 5, "map": mode}
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        work = tmp / "work"
        work.mkdir()
        (work / "maze").write_text(mz.CLI_SOURCE)
        (work / "maze").chmod(0o755)
        svc = mz.MazeService(mz.generate(params), params, tmp / "logs" / "maze-c0.jsonl")
        url = svc.start()
        self.addCleanup(svc.stop)
        (work / ".maze.json").write_text(json.dumps({"socket": url, "token": svc.register("a01", 0, pid=os.getpid())}))
        expected_model = mz.MazeModel(mz.generate(params), params)
        expected_model.register("a01", 0)
        explorer, twin, last, last_expected, sent = mz.Explorer(5, 0, mode == "shared"), mz.Explorer(5, 0, mode == "shared"), None, None, 0
        while True:
            step = explorer.next(last)
            expect = twin.next(last_expected)
            self.assertEqual(step, expect)
            if step["op"] == "final":
                break
            done = subprocess.run(["python3"] + step["command"].replace("./maze", "maze", 1).split(), cwd=work,
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            self.assertEqual(done.stdout.count("\n"), 1)
            last = done.stdout.strip()
            op, arg = mz.parse_command(step["command"])
            last_expected = json.dumps(expected_model.call("a01", op, arg))
            self.assertEqual(json.loads(last), json.loads(last_expected))   # the HTTP path equals the in-process rule
            sent += 1
        sim = mz.simulate(params, 1)
        self.assertEqual(sent, sim["calls"])
        self.assertEqual(svc.summary()["calls"], sim["calls"])
        self.assertEqual(svc.summary()["unique_cells"], sim["unique_cells"])
        self.assertEqual(sim["exit_tick"], sim["calls"])
        self.assertEqual(svc.summary()["exit"]["agent"], "a01")
        self.assertTrue(svc.verdict("a01")["passed"])
        return work

    def test_solo_own_matches_simulate_call_for_call(self):
        self.run_solo("own")

    def test_solo_shared_matches_simulate_call_for_call(self):
        self.run_solo("shared")

    def test_cli_usage_and_transport_errors(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        (tmp / "maze").write_text(mz.CLI_SOURCE)
        run = lambda *args: subprocess.run(["python3", "maze", *args], cwd=tmp, capture_output=True, text=True, timeout=30)
        self.assertEqual(run().returncode, 2)
        self.assertEqual(run("move").returncode, 2)
        self.assertEqual(run("look", "N").returncode, 2)
        self.assertEqual(run("fly").returncode, 2)
        no_config = run("look")
        self.assertEqual(no_config.returncode, 1)
        self.assertIn("transport", json.loads(no_config.stdout)["error"])
        (tmp / ".maze.json").write_text(json.dumps({"socket": str(tmp / "no-such.sock"), "token": "x"}))
        down = run("look")
        self.assertEqual(down.returncode, 1)
        self.assertIn("transport", json.loads(down.stdout)["error"])

    def test_cli_forged_token_prints_error_and_fails(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        svc = mz.MazeService(mz.generate({"cols": 6, "rows": 6}), {"cols": 6, "rows": 6}, tmp / "m.jsonl")
        url = svc.start()
        self.addCleanup(svc.stop)
        (tmp / "maze").write_text(mz.CLI_SOURCE)
        (tmp / ".maze.json").write_text(json.dumps({"socket": url, "token": "forged"}))
        done = subprocess.run(["python3", "maze", "look"], cwd=tmp, capture_output=True, text=True, timeout=30)
        self.assertEqual(done.returncode, 1)
        self.assertEqual(json.loads(done.stdout)["error"], "forbidden")

    def test_test_sh_passes_only_after_the_exit(self):
        params = {"cols": 8, "rows": 6, "seed": 2, "map": "own"}
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, tmp, True)
        work = tmp / "work"
        shutil.copytree(ROOT / "tasks" / "maze-branchy-own" / "workspace", work)
        svc = mz.MazeService(mz.generate(params), params, tmp / "m.jsonl")
        url = svc.start()
        self.addCleanup(svc.stop)
        (work / ".maze.json").write_text(json.dumps({"socket": url, "token": svc.register("a01", 0, pid=os.getpid())}))
        test = ROOT / "tasks" / "maze-branchy-own" / "tests" / "test.sh"
        before = subprocess.run(["bash", str(test)], cwd=work, capture_output=True, text=True, timeout=30)
        self.assertEqual(before.returncode, 1, before.stdout)
        m, token = svc.maze, svc.register("a02", 1)
        route = m.shortest_route()
        for i, j in zip(route, route[1:]):
            post(url, json.loads((work / ".maze.json").read_text())["token"], "move",
                 {-m.cols: "N", 1: "E", m.cols: "S", -1: "W"}[j - i])
        after = subprocess.run(["bash", str(test)], cwd=work, capture_output=True, text=True, timeout=30)
        self.assertEqual(after.returncode, 0, after.stdout + after.stderr)
        self.assertIn('"passed":true', after.stdout.replace(" ", ""))
        self.assertIsNotNone(token)


class TaskDirsTest(unittest.TestCase):
    def test_tasks_load_and_carry_their_params(self):
        expect = {
            "maze-branchy-shared": {"generator": "prim", "map": "shared", "cols": 40, "rows": 25},
            "maze-branchy-own": {"generator": "prim", "map": "own", "cols": 40, "rows": 25},
            "maze-corridor-shared": {"generator": "backtracker", "map": "shared", "cols": 40, "rows": 25},
            "maze-line-shared": {"generator": "serpentine", "map": "shared", "cols": 40, "rows": 25},
            "maze-large-shared": {"generator": "prim", "map": "shared", "cols": 64, "rows": 40},
            "maze-budget-shared": {"generator": "prim", "map": "shared", "cols": 40, "rows": 25},
        }
        for task_id in TASK_IDS:
            task = load_task(ROOT / "tasks" / task_id)
            self.assertEqual(task.id, task_id)
            self.assertEqual(task.allowed_changes, [])
            self.assertEqual(task.tags, ["maze"])
            self.assertIsNone(task.script)
            params = mz.validate_params(task_params(task_id))
            for key, value in expect[task_id].items():
                self.assertEqual(params[key], value, (task_id, key))
            loops = 0.0 if task_id in ("maze-corridor-shared", "maze-line-shared") else 0.05  # controls: no extra openings
            start = "corner" if task_id == "maze-line-shared" else "center"
            self.assertEqual((params["seed"], params["loops"], params["start"]), (2026, loops, start))
            self.assertIn("./maze", task.instruction)

    def test_budget_equals_solo_calls(self):
        params = task_params("maze-budget-shared")
        solo = mz.simulate({**params, "map": "own", "call_budget": 0}, 1)
        self.assertEqual(params["call_budget"], solo["calls"])
        self.assertEqual(task_params("maze-branchy-shared")["call_budget"], 0)

    def test_every_task_lets_a_solo_ante_finish(self):
        for task_id in TASK_IDS:
            params = {**task_params(task_id), "map": "own", "call_budget": 0}
            self.assertIsNotNone(mz.simulate(params, 1)["exit_tick"], task_id)

    def test_workspace_is_only_the_identical_cli(self):
        clis = []
        for task_id in TASK_IDS:
            ws = ROOT / "tasks" / task_id / "workspace"
            self.assertEqual([p.name for p in ws.iterdir()], ["maze"], task_id)
            self.assertFalse((ws / "maze").is_symlink())
            self.assertTrue(os.access(ws / "maze", os.X_OK))
            clis.append((ws / "maze").read_bytes())
            self.assertEqual(clis[-1], mz.CLI_SOURCE.encode())
        self.assertEqual(len(set(clis)), 1)

    def test_no_symlinks_and_no_maze_data_anywhere(self):
        for task_id in TASK_IDS:
            root = ROOT / "tasks" / task_id
            maze = mz.generate(task_params(task_id))
            walls_hex = bytes(maze.walls).hex().encode()
            for path in root.rglob("*"):
                self.assertFalse(path.is_symlink(), path)
                if path.is_file():
                    data = path.read_bytes()
                    self.assertNotIn(walls_hex[:64], data, path)
                    self.assertNotIn(b"walls", data.replace(b"through a wall", b""), path) if path.name != "maze" else None
            self.assertEqual(sorted(p.name for p in root.iterdir()), ["instruction.md", "task.toml", "tests", "workspace"])
            self.assertEqual([p.name for p in (root / "tests").iterdir()], ["test.sh"])

    def test_generator_tool_is_deterministic(self):
        with tempfile.TemporaryDirectory() as tmp:
            done = subprocess.run([sys.executable, str(ROOT / "tools" / "make_maze_tasks.py"), tmp],
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            for task_id in TASK_IDS:
                mine = sorted(p.relative_to(ROOT / "tasks" / task_id) for p in (ROOT / "tasks" / task_id).rglob("*") if p.is_file())
                fresh = sorted(p.relative_to(Path(tmp) / task_id) for p in (Path(tmp) / task_id).rglob("*") if p.is_file())
                self.assertEqual(mine, fresh)
                for rel in mine:
                    self.assertEqual((ROOT / "tasks" / task_id / rel).read_bytes(), (Path(tmp) / task_id / rel).read_bytes(), (task_id, rel))


class IsolationTest(unittest.TestCase):
    def test_maze_is_not_on_disk_or_in_the_client(self):
        params = {"cols": 14, "rows": 9, "seed": 31}
        maze = mz.generate(params)
        with tempfile.TemporaryDirectory() as tmp:
            tmp = Path(tmp)
            work = tmp / "work"
            work.mkdir()
            (work / "maze").write_text(mz.CLI_SOURCE)
            svc = mz.MazeService(maze, params, tmp / "logs" / "maze-c0.jsonl")
            url = svc.start()
            token = svc.register("a01", 0, pid=os.getpid())
            (work / ".maze.json").write_text(json.dumps({"socket": url, "token": token}))
            post(url, token, "look")
            svc.stop()
            needles = [bytes(maze.walls).hex().encode(), bytes(maze.walls)[:24], json.dumps(list(maze.walls)).encode()]
            for path in tmp.rglob("*"):
                if path.is_file():
                    data = path.read_bytes()
                    for needle in needles:
                        self.assertNotIn(needle, data, path)
            # the workspace holds the client and the connection file, nothing else
            self.assertEqual(sorted(p.name for p in work.iterdir()), [".maze.json", "maze"])
            self.assertEqual(set(json.loads((work / ".maze.json").read_text())), {"socket", "token"})
            # the socket is in the service's own directory, not the workspace, and goes with the service
            self.assertFalse(Path(url).is_relative_to(tmp))
            self.assertFalse(Path(url).exists())

    def test_service_listens_only_on_a_unix_socket_in_a_private_directory(self):
        params = {"cols": 6, "rows": 6}
        with tempfile.TemporaryDirectory() as tmp:
            svc = mz.MazeService(mz.generate(params), params, Path(tmp) / "m.jsonl")
            sock = svc.start()
            try:
                self.assertEqual(svc._server.socket.family, socket.AF_UNIX)
                self.assertEqual(os.stat(Path(sock).parent).st_mode & 0o777, 0o700)
                self.assertLess(len(sock), 104)
            finally:
                svc.stop()
            self.assertFalse(Path(sock).parent.exists())



class HardeningTest(unittest.TestCase):
    """The review of 69f611ec: stolen tokens, the live log, oversized requests, and the start order."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, True)
        self.params = {"cols": 8, "rows": 6, "seed": 2, "map": "own"}
        self.svc = mz.MazeService(mz.generate(self.params), self.params, self.tmp / "logs" / "maze-c000.jsonl")
        self.sock = self.svc.start()
        self.addCleanup(self.svc.stop)
        self.work = self.tmp / "work"
        self.work.mkdir()
        (self.work / "maze").write_text(mz.CLI_SOURCE)

    def agent_root(self, command="python3 maze look"):
        """A stand-in agent: a shell that waits for a line on stdin, then runs `command` as its child."""
        proc = subprocess.Popen(["/bin/sh", "-c", f"read go; {command}"], cwd=self.work, stdin=subprocess.PIPE,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        self.addCleanup(lambda: (proc.kill(), proc.wait(), proc.stdin.close(), proc.stdout.close()))
        return proc

    def test_a_token_from_a_non_descendant_is_refused_and_costs_its_owner_nothing(self):
        owner = self.agent_root()
        token = self.svc.register("a01", 0)
        self.svc.bind("a01", owner.pid)
        (self.work / ".maze.json").write_text(json.dumps({"socket": self.sock, "token": token}))
        # The owner's own child gets an answer.
        out, _ = owner.communicate("go\n", timeout=30)
        self.assertEqual(json.loads(out)["at"], [4, 3])
        before = (self.svc.verdict("a01"), self.svc.summary()["per_agent"]["a01"])
        # A sibling (this test process, then a separate CLI process) that read .maze.json is refused.
        route = self.svc.maze.shortest_route()
        name = {-8: "N", 1: "E", 8: "S", -1: "W"}[route[1] - route[0]]
        wall = "X"   # an illegal move: charged to the owner it would fail the legality check
        for op, arg in (("look", None), ("move", wall), ("move", name), ("verdict", None)):
            status, res = post(self.sock, token, op, arg)
            self.assertEqual((status, res["error"]), (403, "forbidden"))
        sibling = subprocess.run(["python3", "maze", "move", wall], cwd=self.work, capture_output=True, text=True, timeout=30)
        self.assertEqual(sibling.returncode, 1)
        self.assertEqual(json.loads(sibling.stdout)["error"], "forbidden")
        after = (self.svc.verdict("a01"), self.svc.summary()["per_agent"]["a01"])
        self.assertEqual(before, after)
        self.assertTrue(after[0]["legal"])              # the wall bumps were not charged to the owner
        self.assertEqual(after[1]["calls"], 1)
        self.assertEqual(self.svc.summary()["stolen"], 5)
        self.svc.stop()
        rows = [json.loads(line) for line in self.svc.log_path.read_text().splitlines()]
        stolen = [r for r in rows if r.get("error") == "stolen_token"]
        self.assertEqual(len(stolen), 5)
        self.assertTrue(all(r["owner"] == "a01" and r["agent"] is None and r["peer"] != owner.pid for r in stolen))

    def test_an_unbound_token_is_refused_after_a_short_wait(self):
        token = self.svc.register("a01", 0)
        with mock.patch.object(mz, "BIND_WAIT_S", 0.2):
            status, res = post(self.sock, token, "look")
        self.assertEqual(status, 403)
        self.assertEqual(self.svc.summary()["per_agent"]["a01"]["calls"], 0)

    def test_a_call_waiting_for_its_bind_goes_through_once_bound(self):
        token = self.svc.register("a01", 0)
        threading.Timer(0.3, self.svc.bind, ("a01", os.getpid())).start()
        status, res = post(self.sock, token, "look")
        self.assertEqual(status, 200, res)

    def test_bind_refuses_init_and_unknown_agents(self):
        self.svc.register("a01", 0)
        for pid in (0, 1, -5, None):
            with self.assertRaises(ValueError):
                self.svc.bind("a01", pid)
        with self.assertRaises(KeyError):
            self.svc.bind("nobody", os.getpid())

    def test_rebinding_moves_the_token_to_the_checker(self):
        token = self.svc.register("a01", 0, pid=os.getpid())
        self.assertEqual(post(self.sock, token, "look")[0], 200)
        checker = self.agent_root("python3 maze verdict")
        (self.work / ".maze.json").write_text(json.dumps({"socket": self.sock, "token": token}))
        self.svc.bind("a01", checker.pid)
        self.assertEqual(post(self.sock, token, "look")[0], 403)   # the agent's tree no longer counts
        out, _ = checker.communicate("go\n", timeout=30)
        self.assertEqual(json.loads(out)["calls"], 1)

    def test_descends_from_walks_parents_and_fails_closed(self):
        self.assertTrue(mz.descends_from(os.getpid(), os.getpid()))
        self.assertTrue(mz.descends_from(os.getpid(), os.getppid()))
        self.assertFalse(mz.descends_from(os.getppid(), os.getpid()))
        child = subprocess.Popen(["sleep", "5"])
        self.addCleanup(lambda: (child.kill(), child.wait()))
        self.assertTrue(mz.descends_from(child.pid, os.getpid()))
        self.assertFalse(mz.descends_from(os.getpid(), child.pid))
        self.assertFalse(mz.descends_from(4_000_000, os.getpid()))   # no such process
        self.assertEqual(mz.parent_pid(child.pid), os.getpid())

    def test_without_a_peer_pid_lookup_the_service_refuses_to_start(self):
        with mock.patch.object(mz.sys, "platform", "sunos5"):
            self.assertIn("sunos5", mz.peer_check_problem())
            svc = mz.MazeService(mz.generate(self.params), self.params, self.tmp / "x.jsonl")
            with self.assertRaises(mz.PeerUnavailable):
                svc.start()
        self.assertFalse((self.tmp / "x.jsonl").exists())

    def test_nothing_is_on_disk_while_the_cell_runs(self):
        token = self.svc.register("a01", 0, pid=os.getpid())
        for _ in range(5):
            post(self.sock, token, "look")
        post(self.sock, "forged", "look")
        self.assertFalse(self.svc.log_path.exists())
        self.assertFalse(self.svc.log_path.with_suffix(".json").exists())
        self.assertFalse(self.svc.log_path.parent.exists() and any(self.svc.log_path.parent.iterdir()))
        self.svc.stop()
        self.assertEqual(len(self.svc.log_path.read_text().splitlines()), 6)
        self.assertEqual(json.loads(self.svc.log_path.with_suffix(".json").read_text())["calls"], 5)

    def test_long_args_and_ops_are_cut_in_the_log(self):
        token = self.svc.register("a01", 0, pid=os.getpid())
        self.assertEqual(post(self.sock, token, "move", "N" * 3000)[1]["error"], "bad_arg")
        self.assertEqual(post(self.sock, token, "x" * 3000)[1]["error"], "bad_op")
        self.assertEqual(post(self.sock, token, "move", ["N"] * 500)[1]["error"], "bad_arg")
        self.assertEqual(post(self.sock, "forged", "y" * 3000)[0], 403)
        self.assertEqual(post(self.sock, token, "move", "N")[0], 200)
        self.svc.stop()
        rows = [json.loads(line) for line in self.svc.log_path.read_text().splitlines()]
        self.assertEqual((len(rows[0]["arg"]), rows[0]["arg_len"]), (mz.ARG_LOG_CAP, 3000))
        self.assertEqual((len(rows[1]["op"]), rows[1]["op_len"]), (mz.ARG_LOG_CAP, 3000))
        self.assertEqual(len(rows[2]["arg"]), mz.ARG_LOG_CAP)
        self.assertGreater(rows[2]["arg_len"], mz.ARG_LOG_CAP)
        self.assertEqual((len(rows[3]["op"]), rows[3]["op_len"]), (mz.ARG_LOG_CAP, 3000))
        self.assertEqual((rows[4]["arg"], "arg_len" in rows[4]), ("N", False))

    def test_bad_content_lengths_are_refused_without_reading(self):
        token = self.svc.register("a01", 0, pid=os.getpid())
        body = json.dumps({"token": token, "op": "look"}).encode()
        for length in ("-1", "-100000", str(mz.MAX_BODY + 1), "99999999", "abc"):
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
                s.settimeout(5)   # a read that blocks would hit this
                s.connect(self.sock)
                s.sendall(b"POST /call HTTP/1.1\r\nHost: x\r\nContent-Length: " + length.encode() + b"\r\n\r\n" + body)
                reply = b""
                while chunk := s.recv(4096):
                    reply += chunk
                reply = reply.decode()
            self.assertTrue(reply.startswith("HTTP/1.0 400") or reply.startswith("HTTP/1.1 400"), (length, reply))
        self.assertEqual(self.svc.summary()["per_agent"]["a01"]["calls"], 0)
        self.assertEqual(post(self.sock, token, "look")[0], 200)   # the service is still answering

    def test_a_failed_bind_leaves_no_file_socket_or_directory(self):
        base = self.tmp / "sockets"
        base.mkdir()
        svc = mz.MazeService(mz.generate(self.params), self.params, self.tmp / "logs2" / "m.jsonl", socket_dir=base)
        with mock.patch("socketserver.UnixStreamServer.server_bind", side_effect=OSError("bind failed")):
            with self.assertRaises(OSError):
                svc.start()
        self.assertEqual(list(base.iterdir()), [])
        self.assertFalse((self.tmp / "logs2").exists())
        svc.stop()   # nothing to stop; must not raise


if __name__ == "__main__":
    unittest.main()
