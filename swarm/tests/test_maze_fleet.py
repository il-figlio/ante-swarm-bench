"""Fleet modes (single, swarm, parallel, delegate, council) on the maze: Python/JS parity and each mode's rules."""
import json
import subprocess
import unittest
from collections import Counter

from swarm import maze as mz
from test_maze import CORE_JS, NODE, TASK_IDS, task_params

# (mode, count, member_budget): every mode at its edges and in the middle.
CASES = [("single", 1, None), ("swarm", 4, None), ("parallel", 2, None), ("parallel", 3, None), ("parallel", 6, None),
         ("delegate", 1, None), ("delegate", 3, None), ("delegate", 6, None), ("council", 2, 300),
         ("council", 3, 300), ("council", 6, None)]


def run(params, mode, count, budget=None):
    log = []
    out = mz.simulate_fleet(params, mode, count, budget, log)
    return json.loads(json.dumps(out)), json.loads(json.dumps(log))


@unittest.skipUnless(NODE, "node is not installed")
class FleetParityTest(unittest.TestCase):
    def js(self, params: dict, cases) -> list:
        script = (f"const m=require({json.dumps(str(CORE_JS))});const p={json.dumps(params)};"
                  f"const out=[];for(const [mode,n,b] of {json.dumps(cases)}){{const log=[];"
                  "const r=m.simulateFleet(p,mode,n,b,log);out.push([r,log]);}"
                  "console.log(JSON.stringify(out));")
        done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=300)
        self.assertEqual(done.returncode, 0, done.stderr)
        return json.loads(done.stdout)

    def test_every_mode_on_every_task(self):
        for task_id in TASK_IDS:
            params = task_params(task_id)
            for (mode, count, budget), (js_out, js_log) in zip(CASES, self.js(params, CASES)):
                py_out, py_log = run(params, mode, count, budget)
                self.assertEqual(js_out, py_out, (task_id, mode, count))
                self.assertEqual(js_log, py_log, (task_id, mode, count))

    def test_small_shapes_and_limits(self):
        cases = [("delegate", 4, None), ("parallel", 5, None), ("council", 4, 15), ("swarm", 9, None)]
        for params in ({"cols": 4, "rows": 4, "seed": 0, "start": "corner"},
                       {"cols": 12, "rows": 12, "seed": 99, "call_budget": 40},
                       {"cols": 20, "rows": 20, "seed": 5, "step_limit": 30}):
            for (mode, count, budget), (js_out, js_log) in zip(cases, self.js(params, cases)):
                py_out, py_log = run(params, mode, count, budget)
                self.assertEqual(js_out, py_out, (params, mode, count))
                self.assertEqual(js_log, py_log, (params, mode, count))

    def test_js_validation_errors(self):
        script = (f"const m=require({json.dumps(str(CORE_JS))});const bad=[['parallel',1],['parallel',7],"
                  "['delegate',0],['delegate',7],['council',1],['council',7],['single',2],['swarm',0],['solo',1],"
                  "['council',3,0],['council',3,1.5],['delegate',3,10],['parallel',2.5]];let n=0;"
                  "for(const [mo,c,b] of bad){try{m.simulateFleet({},mo,c,b)}catch(e){n++}}console.log(n)")
        done = subprocess.run([NODE, "-e", script], capture_output=True, text=True, timeout=30)
        self.assertEqual(done.stdout.strip(), "13", done.stderr)


class FleetRulesTest(unittest.TestCase):
    def test_validation_errors(self):
        for mode, count, budget in (("parallel", 1, None), ("parallel", 7, None), ("delegate", 0, None),
                                    ("delegate", 7, None), ("council", 1, None), ("council", 7, None),
                                    ("single", 2, None), ("swarm", 0, None), ("solo", 1, None), ("council", 3, 0),
                                    ("council", 3, 1.5), ("delegate", 3, 10), ("parallel", True, None),
                                    ("parallel", 2.5, None)):
            with self.assertRaises(ValueError, msg=(mode, count, budget)):
                mz.simulate_fleet({}, mode, count, budget)
        with self.assertRaises(ValueError):
            mz.simulate_fleet({"nope": 1}, "swarm", 4)

    def test_single_and_swarm_equal_simulate(self):
        for task_id in TASK_IDS:
            params = task_params(task_id)
            single = mz.simulate_fleet(params, "single", 1)
            swarm = mz.simulate_fleet(params, "swarm", 4)
            for out, ref in ((single, mz.simulate(params, 1)), (swarm, mz.simulate({**params, "map": "shared"}, 4))):
                for key, value in ref.items():
                    self.assertEqual(out[key], value, (task_id, out["mode"], key))

    def test_delegate_lead_makes_at_most_one_call_per_tick(self):
        for task_id in ("maze-branchy-shared", "maze-large-shared"):
            for workers in (1, 3, 6):
                out, log = run(task_params(task_id), "delegate", workers)
                assigns = Counter(row[0] for row in log if row[1] == "lead" and row[2] == "assign")
                self.assertLessEqual(max(assigns.values()), 1)
                self.assertEqual(sum(assigns.values()), out["lead_calls"])
                # Workers never call next-edge themselves; all calls add up.
                self.assertFalse([row for row in log if row[2] == "next-edge"])
                self.assertEqual(out["calls"], sum(out["per_agent_calls"]) + out["lead_calls"])
                self.assertIsNotNone(out["exit_tick"])
                self.assertGreater(out["wait_ticks"], 0)

    def test_parallel_never_runs_more_than_three(self):
        for params in (task_params("maze-branchy-shared"), {"cols": 20, "rows": 20, "seed": 5, "step_limit": 30},
                       {"cols": 12, "rows": 12, "seed": 99, "call_budget": 40}):
            for count in (2, 4, 6):
                out, log = run(params, "parallel", count)
                running, peak = set(), 0
                for tick in range(out["ticks"]):
                    for row in log:
                        if row[0] == tick and row[2] == "start":
                            running.add(row[1])
                    peak = max(peak, len(running))
                    for row in log:
                        if row[0] == tick and row[2] in ("final", "cancel"):
                            running.discard(row[1])
                self.assertLessEqual(peak, mz.PARALLEL_CONCURRENCY, (params, count))
                self.assertEqual(out["concurrency"], 3)

    def test_parallel_extra_runs_start_only_after_one_finishes(self):
        # Expected finding: in a race, runs past the concurrency cap never start, so parallel 6 == parallel 3.
        params = task_params("maze-branchy-shared")
        three, six = mz.simulate_fleet(params, "parallel", 3), mz.simulate_fleet(params, "parallel", 6)
        self.assertEqual(six["per_agent_calls"][3:], [0, 0, 0])
        self.assertEqual((six["exit_tick"], six["calls"]), (three["exit_tick"], three["calls"]))
        # With a tight step limit runs do finish early, and the waiting ones then get their turn.
        out, log = run({"cols": 20, "rows": 20, "seed": 5, "step_limit": 30}, "parallel", 6)
        self.assertTrue(all(c > 0 for c in out["per_agent_calls"]))

    def test_council_route_is_legal_and_credited(self):
        for task_id in TASK_IDS:
            maze = mz.generate(task_params(task_id))
            for count, budget in ((3, 300), (3, None), (6, 800)):
                out = mz.simulate_fleet(task_params(task_id), "council", count, budget)
                self.assertTrue(all(c <= (budget or 10 ** 9) for c in out["per_agent_calls"]))
                if out["route"] is None:
                    self.assertFalse(out["verified"])
                    self.assertIsNone(out["exit_tick"])
                    continue
                self.assertTrue(out["verified"])
                route = out["route"]
                self.assertEqual((route[0], route[-1]), (maze.start, maze.exit))
                for a, b in zip(route, route[1:]):
                    self.assertIn(b, [j for _, j in maze.neighbours(a)])
                self.assertGreaterEqual(len(route), len(maze.shortest_route()))
                self.assertEqual(sum(out["credit"].values()), len(route))
                self.assertEqual(out["calls"], sum(out["per_agent_calls"]) + 1)
                if out["best_member_route_len"] is not None:
                    self.assertLessEqual(out["route_len"], out["best_member_route_len"])

    def test_parallel_credit_goes_only_to_the_winner(self):
        for task_id in TASK_IDS:
            params = task_params(task_id)
            out = mz.simulate_fleet(params, "parallel", 3)
            if out["exit_agent"] is None:
                self.assertEqual(out["credit"], {})
                continue
            self.assertEqual(out["credit"], {out["exit_agent"]: len(mz.generate(params).shortest_route())})
        self.assertEqual(mz.simulate_fleet({"cols": 16, "rows": 10, "seed": 1}, "parallel", 3)["credit"],
                         {2: 14})
        # single on an own-map task still credits by first entry, which for one agent is the whole route.
        one = mz.simulate_fleet(task_params("maze-branchy-own"), "single", 1)
        self.assertEqual(one["credit"], {0: len(mz.generate(task_params("maze-branchy-own")).shortest_route())})

    def test_council_exit_tick_is_last_call_tick_plus_two(self):
        out, log = run(task_params("maze-branchy-shared"), "council", 3, 40)
        self.assertEqual(out["per_agent_calls"], [40, 40, 40])
        self.assertEqual(out["ticks"], 41)  # no route found with 40 calls, so no exit_tick; ticks still end at 41
        last_call = max(row[0] for row in log if row[2] not in ("start", "final", "synthesize"))
        self.assertEqual(last_call, 39)
        self.assertEqual(log[-1][:3], [40, "synth", "synthesize"])
        out, _ = run(task_params("maze-line-shared"), "council", 3)
        self.assertEqual(out["exit_tick"], 1001)

    def test_council_without_budget_finds_the_exit_and_does_not_stop_early(self):
        out, log = run(task_params("maze-branchy-shared"), "council", 3)
        self.assertTrue(out["verified"])
        # Every member made calls up to its own final, not just up to the first member's exit.
        finals = {row[1]: row[0] for row in log if row[2] == "final"}
        self.assertEqual(sorted(finals), [0, 1, 2])
        self.assertEqual(log[-1][1:3], ["synth", "synthesize"])
        self.assertEqual(log[-1][0], out["ticks"] - 1)

    def test_council_on_serpentine_matches_a_member_that_reached_the_exit(self):
        params = task_params("maze-line-shared")
        maze = mz.generate(params)
        out = mz.simulate_fleet(params, "council", 3)
        self.assertEqual(out["route"], maze.shortest_route())
        self.assertEqual(out["route_len"], out["best_member_route_len"])
        self.assertFalse(out["union_shorter"])
        # A single member alone on the same maze reaches the exit and knows exactly that route.
        model = mz.MazeModel(maze, {**params, "map": "own"})
        model.register("0", 0)
        explorer, last = mz.Explorer(params["seed"], 0, False), None
        while True:
            step = explorer.next(last)
            if step["op"] == "final":
                break
            last = json.dumps(model.call("0", *mz.parse_command(step["command"])))
        self.assertTrue(model.agents["0"].reached)
        self.assertEqual(mz._known_route(maze, model.agents["0"].own), out["route"])


if __name__ == "__main__":
    unittest.main()
