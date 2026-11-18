"""Maze eval wiring: the scripted model's policy mode, tasks with [maze], and the runner's per-cell maze services.

Unit parts use fakes and never need swarm.maze. The runner parts run a tiny real harness (mini) against the real
maze service on a small temporary task, and, when the sealed tasks exist, against tasks/maze-branchy-shared and
tasks/maze-branchy-own."""
import json
import os
import shutil
import sys
import tempfile
import types
import unittest
import urllib.error
import urllib.request
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import OK_SCRIPT, ROOT, make_task, tempdir  # noqa: E402
import swarm  # noqa: E402
from swarm import harnesses  # noqa: E402
from swarm.bundle import verify  # noqa: E402
from swarm.plan import freeze  # noqa: E402
from swarm.runner import infra_reason, run  # noqa: E402
from swarm.scripted import ScriptedModel, latest_tool_result, policy_input  # noqa: E402
from swarm.tasks import load_task  # noqa: E402

try:
    from swarm import maze as real_maze  # noqa: E402
except ImportError:
    real_maze = None


def post(url, path, body):
    request = urllib.request.Request(url + path, data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


class FakePolicy:
    """Asks for `./maze look` n times, then finishes. Records what it was shown."""

    def __init__(self, n=3):
        self.n, self.seen = n, []

    def next(self, last_output):
        self.seen.append(last_output)
        if len(self.seen) > self.n:
            return {"op": "final", "text": "done"}
        return {"op": "bash", "command": f"./maze move N#{len(self.seen)}"}


def responses_body(outputs, tools=("bash",), output_kind="function_call_output", **over):
    items = [{"role": "user", "content": "go"}]
    for i, out in enumerate(outputs):
        items += [{"type": "function_call", "call_id": str(i), "name": "bash", "arguments": "{}"},
                  {"type": output_kind, "call_id": str(i), "output": out}]
    return dict({"model": "m", "reasoning": {"effort": "medium"}, "max_output_tokens": 100, "input": items,
                 "tools": [{"type": "function", "name": n} for n in tools]}, **over)


def messages_body(outputs, tools=("Bash",), **over):
    messages = [{"role": "user", "content": "go"}]
    for i, out in enumerate(outputs):
        messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}]},
                     {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": out}]}]
    return dict({"model": "m", "max_tokens": 100, "output_config": {"effort": "medium"}, "messages": messages,
                 "tools": [{"name": n, "input_schema": {}} for n in tools]}, **over)


class LatestToolResultTests(unittest.TestCase):
    def test_responses_takes_the_last_output_item_of_either_kind(self):
        self.assertEqual(latest_tool_result(responses_body(["a", "b"]), False), "b")
        self.assertEqual(latest_tool_result(responses_body(["a", "c"], output_kind="custom_tool_call_output"), False), "c")
        self.assertEqual(latest_tool_result(responses_body([[{"type": "output_text", "text": "x"}, {"type": "output_text", "text": "y"}]]), False), "xy")
        self.assertEqual(latest_tool_result(responses_body([]), False), "")
        self.assertEqual(latest_tool_result({"input": "just text"}, False), "")

    def test_messages_takes_the_last_tool_result_block_string_or_text_blocks(self):
        self.assertEqual(latest_tool_result(messages_body(["a", "b"]), True), "b")
        blocks = [{"type": "text", "text": "{\"at\":"}, {"type": "text", "text": "[1,2]}"}]
        self.assertEqual(latest_tool_result(messages_body(["a", blocks]), True), '{"at":[1,2]}')
        body = messages_body(["a", "b"])
        body["messages"][-1]["content"].append({"type": "text", "text": "reminder"})  # text after the result is not the result
        self.assertEqual(latest_tool_result(body, True), "b")
        self.assertEqual(latest_tool_result(messages_body([]), True), "")

    def test_policy_input_finds_the_json_line_in_a_harness_wrapper(self):
        line = '{"at":[1,2],"open":["N"],"new":["N"],"exit":false,"steps":3}'
        self.assertEqual(policy_input(line), line)
        self.assertEqual(policy_input(f"exit=0\n{line}\n"), line)
        self.assertEqual(policy_input(f"Chunk ID: 1\nProcess exited with code 0\nOutput:\n{line}\n"), line)
        self.assertEqual(policy_input("no json here"), "no json here")
        self.assertEqual(policy_input("[1, 2]"), "[1, 2]")


class PolicyModeTests(unittest.TestCase):
    def setUp(self):
        self.model = ScriptedModel()
        self.addCleanup(self.model.close)

    def test_responses_wire_walks_the_policy_and_maps_steps_to_native_tools(self):
        policy = FakePolicy(2)
        url = self.model.register("p", policy, "/w", "m", "medium", 100)
        outs = ["r0", "r1"]
        calls = []
        for n in range(3):
            status, reply = post(url, "/responses", responses_body(outs[:n]))
            self.assertEqual(status, 200)
            calls.append(reply["output"][0])
        self.assertEqual([c["type"] for c in calls], ["function_call", "function_call", "message"])
        self.assertEqual(json.loads(calls[1]["arguments"]), {"command": "./maze move N#2"})
        self.assertEqual(policy.seen, [None, "r0", "r1"])
        rows, final_served = self.model.release("p")
        self.assertTrue(final_served)
        self.assertEqual([r["op"] for r in rows], ["bash", "bash", "final"])
        self.assertEqual([r["arg"] for r in rows][:2], ["./maze move N#1", "./maze move N#2"])

    def test_messages_wire_gets_the_same_steps_with_the_clients_own_tool_name(self):
        policy = FakePolicy(1)
        url = self.model.register("q", policy, "/w", "m", "medium", 100)
        status, reply = post(url, "/messages", messages_body([]))
        self.assertEqual(status, 200)
        self.assertEqual((reply["content"][0]["type"], reply["content"][0]["name"]), ("tool_use", "Bash"))
        self.assertEqual(reply["content"][0]["input"]["command"], "./maze move N#1")
        status, reply = post(url, "/messages", messages_body([[{"type": "text", "text": "seen"}]]))
        self.assertEqual(reply["stop_reason"], "end_turn")
        self.assertEqual(policy.seen, [None, "seen"])
        self.assertTrue(self.model.release("q")[1])

    def test_a_repeated_request_gets_the_same_step_and_asks_the_policy_once(self):
        policy = FakePolicy(3)
        url = self.model.register("r", policy, "/w", "m", "medium", 100)
        first = post(url, "/responses", responses_body([]))[1]["output"][0]["arguments"]
        again = post(url, "/responses", responses_body([]))[1]["output"][0]["arguments"]
        self.assertEqual(first, again)
        self.assertEqual(len(policy.seen), 1)

    def test_requests_after_the_final_step_or_ahead_of_it_are_refused_and_recorded(self):
        policy = FakePolicy(0)
        url = self.model.register("s", policy, "/w", "m", "medium", 100)
        self.assertEqual(post(url, "/responses", responses_body(["x"]))[0], 400)  # a result before any step was issued
        self.assertEqual(post(url, "/responses", responses_body([]))[1]["output"][0]["type"], "message")  # final
        self.assertEqual(post(url, "/responses", responses_body(["x"]))[0], 400)  # past the final
        rows, final_served = self.model.release("s")
        self.assertEqual([r.get("error") for r in rows], ["extra_request", None, "extra_request"])
        self.assertTrue(final_served)
        self.assertEqual(policy.seen, [None])

    def test_a_failing_policy_is_a_recorded_request_error(self):
        class Broken:
            def next(self, last):
                raise RuntimeError("boom")

        url = self.model.register("b", Broken(), "/w", "m", "medium", 100)
        self.assertEqual(post(url, "/responses", responses_body([]))[0], 500)
        rows, _ = self.model.release("b")
        self.assertEqual((rows[0]["error"], rows[0]["policy_error"]), ("policy_error", "RuntimeError: boom"))

    def test_a_policy_that_returns_nonsense_is_an_error_not_a_hang(self):
        class Nonsense:
            def next(self, last):
                return {"op": "teleport"}

        url = self.model.register("n", Nonsense(), "/w", "m", "medium", 100)
        self.assertEqual(post(url, "/responses", responses_body([]))[0], 500)

    def test_list_scripts_behave_as_before(self):
        url = self.model.register("l", OK_SCRIPT, "/w", "m", "medium", 100)
        names = [post(url, "/responses", responses_body(["x"] * i, tools=("Read", "Write", "Bash")))[1]["output"][0].get("name") for i in range(3)]
        self.assertEqual(names, ["Read", "Write", "Bash"])
        self.assertEqual(post(url, "/responses", responses_body(["x"] * 9, tools=("Read",)))[0], 400)


def fake_maze_module():
    """Stands in for swarm.maze where a test needs the runner to start services without the real one."""
    module = types.ModuleType("swarm.maze")
    module.validate_params = lambda d: {"seed": 5, "map": "shared", **d}
    module.generate = lambda params: ("maze", params)
    return module


def write_maze_task(base: Path, task_id: str, maze_toml: str, test: str | None = None, with_cli=True) -> Path:
    """A maze task with a minimal ./maze (stdlib, calls the service named in .maze.json) and a test that asks for the verdict."""
    root = base / task_id
    (root / "workspace").mkdir(parents=True)
    (root / "tests").mkdir()
    (root / "task.toml").write_text(f'[task]\nid = "{task_id}"\nallowed_changes = []\n[agent]\ntimeout_sec = 120\n{maze_toml}\n')
    (root / "instruction.md").write_text("Find the exit with ./maze.\n")
    if with_cli:
        cli = root / "workspace" / "maze"
        cli.write_text('#!/usr/bin/env python3\nimport json, socket, sys\n'
                       'cfg = json.load(open(".maze.json"))\n'
                       'body = json.dumps({"token": cfg["token"], "op": sys.argv[1], "arg": sys.argv[2] if len(sys.argv) > 2 else None}).encode()\n'
                       's = socket.socket(socket.AF_UNIX)\ns.settimeout(20)\ns.connect(cfg["socket"])\n'
                       's.sendall(b"POST /call HTTP/1.0\\r\\nContent-Length: %d\\r\\n\\r\\n" % len(body) + body)\n'
                       'data = b""\nwhile chunk := s.recv(65536):\n    data += chunk\n'
                       'print(data.split(b"\\r\\n\\r\\n", 1)[1].decode())\n')
        cli.chmod(0o755)
    (root / "tests" / "test.sh").write_text(test or '#!/bin/bash\nset -euo pipefail\n'
                                            './maze verdict | python3 -c "import json,sys; sys.exit(0 if json.load(sys.stdin)[\'passed\'] else 1)"\n')
    return root


class TaskParsingTests(unittest.TestCase):
    def test_maze_table_is_parsed_into_the_task_and_covered_by_the_digest(self):
        with tempdir() as d, mock.patch.dict(sys.modules, {"swarm.maze": fake_maze_module()}), \
                mock.patch.object(swarm, "maze", fake_maze_module(), create=True):
            root = write_maze_task(Path(d), "m", '[maze]\ncols = 8\nrows = 6\nmap = "own"\n')
            task = load_task(root)
            self.assertEqual((task.maze["cols"], task.maze["rows"], task.maze["map"]), (8, 6, "own"))
            self.assertIsNone(task.script)  # no script.json needed
            before = task.digest()
            (root / "task.toml").write_text((root / "task.toml").read_text().replace("cols = 8", "cols = 9"))
            self.assertNotEqual(load_task(root).digest(), before)

    def test_a_task_without_maze_never_imports_it(self):
        with tempdir() as d, mock.patch.dict(sys.modules, {"swarm.maze": None}), mock.patch.object(swarm, "maze", None, create=True):
            task = load_task(make_task(Path(d), "plain", OK_SCRIPT))
            self.assertIsNone(task.maze)

    def test_maze_must_be_a_table_and_bad_params_are_rejected(self):
        with tempdir() as d:
            root = write_maze_task(Path(d), "m", "")
            (root / "task.toml").write_text('maze = 3\n' + (root / "task.toml").read_text())
            with self.assertRaises(ValueError):
                load_task(root)
        if real_maze:
            with tempdir() as d:
                root = write_maze_task(Path(d), "m", "[maze]\ncols = 2\n")
                with self.assertRaises(ValueError):
                    load_task(root)
                root = write_maze_task(Path(d), "m2", '[maze]\ncols = 8\nrows = 8\ngenerator = "prim"\nwat = 1\n')
                with self.assertRaises(ValueError):
                    load_task(root)

    def test_a_scripted_plan_needs_no_script_for_a_maze_task_and_freezes_its_params(self):
        with tempdir() as d, mock.patch.dict(sys.modules, {"swarm.maze": fake_maze_module()}), \
                mock.patch.object(swarm, "maze", fake_maze_module(), create=True):
            base = Path(d)
            write_maze_task(base / "tasks", "m", "[maze]\ncols = 8\nrows = 6\n")
            plan = freeze(make_spec_for(base))
            self.assertEqual(plan["tasks"][0]["maze"]["cols"], 8)
            self.assertIsNone(plan["tasks"][0].get("script"))


def make_spec_for(base: Path, tasks=None, levels=(1,), extra="", max_requests=40, top=""):
    spec = base / "spec.toml"
    spec.write_text(f'''{top}
question = "maze test"
primary_outcome = "successes_per_minute"
model = {{ source = "scripted", id = "m-1", effort = "medium", max_output_tokens = 16384 }}
harnesses = ["mini"]
levels = {json.dumps(list(levels))}
tasks = {json.dumps(tasks or ["tasks/*"])}
seed = 3
stop_rule = "all cells once"
[budget]
max_requests_per_attempt = {max_requests}
attempt_timeout_s = 300  # 736 calls per Ante on the 40x25 own-map maze; a loaded Mac makes ~5 calls/s
[admission]
reserve_mb = 0
max_pressure = "warn"
{extra}''')
    return spec


class FakeService:
    """Records what the runner does to it; answers verdicts like a service that approves everyone."""
    instances = []

    def __init__(self, maze, params, log_path, seed_info=None):
        self.log_path, self.registered, self.events = Path(log_path), [], []
        self.params, self.seed_info, self.bound = params, seed_info, {}
        FakeService.instances.append(self)

    def start(self):
        self.events.append("start")
        return "/tmp/fake-maze.sock"

    def register(self, agent_id, index, pid=None):
        self.events.append(("register", agent_id, index))
        self.registered.append((agent_id, index))
        return f"tok-{index}"

    def bind(self, agent_id, pid):
        self.events.append(("bind", agent_id, pid))
        self.bound.setdefault(agent_id, []).append(pid)

    def verdict(self, agent_id):
        self.events.append(("verdict", agent_id))
        return {"legal": True, "within_limit": True, "exit_reached": True, "passed": True, "calls": 0}

    def summary(self):
        return {"calls": 0, "exit": None}

    def stop(self):
        self.events.append("stop")
        self.log_path.with_suffix(".json").write_text("{}")


class FakeExplorer:
    def __init__(self, seed, index, shared):
        self.args, self.n = (seed, index, shared), 0

    def next(self, last):
        self.n += 1
        return {"op": "bash", "command": "echo hi"} if self.n <= 2 else {"op": "final", "text": "done"}


class MazeSeedTests(unittest.TestCase):
    def test_plain_is_the_task_seed_and_secret_is_a_keyed_32_bit_derivation(self):
        from swarm.runner import maze_seed
        self.assertEqual(maze_seed(None, 2026, 3, "m"), 2026)
        key, other = os.urandom(32), os.urandom(32)
        seeds = {maze_seed(key, 2026, cell, "m") for cell in range(50)}
        self.assertEqual(len(seeds), 50)                      # every cell its own maze
        self.assertNotIn(2026, seeds)
        self.assertTrue(all(0 <= s <= 0xFFFFFFFF for s in seeds))
        self.assertEqual(maze_seed(key, 2026, 0, "m"), maze_seed(key, 2026, 0, "m"))
        self.assertNotEqual(maze_seed(key, 2026, 0, "m"), maze_seed(other, 2026, 0, "m"))
        self.assertNotEqual(maze_seed(key, 2026, 0, "m"), maze_seed(key, 2026, 0, "n"))

    def test_plan_records_the_mode_and_rejects_others(self):
        with tempdir() as d, mock.patch.dict(sys.modules, {"swarm.maze": fake_maze_module()}), \
                mock.patch.object(swarm, "maze", fake_maze_module(), create=True):
            base = Path(d)
            write_maze_task(base / "tasks", "m", "[maze]\ncols = 8\nrows = 6\n")
            self.assertEqual(freeze(make_spec_for(base))["spec"]["maze_seed"], "secret")
            self.assertEqual(freeze(make_spec_for(base, top='maze_seed = "plain"'))["spec"]["maze_seed"], "plain")
            with self.assertRaises(ValueError):
                freeze(make_spec_for(base, top='maze_seed = "zero"'))


class RunnerWiringTests(unittest.TestCase):
    """The runner's side with a fake maze module: registration, .maze.json, ordering, failures."""

    def setUp(self):
        FakeService.instances = []
        module = fake_maze_module()
        module.MazeService, module.Explorer = FakeService, FakeExplorer
        for patcher in (mock.patch.dict(sys.modules, {"swarm.maze": module}), mock.patch.object(swarm, "maze", module, create=True)):
            patcher.start()
            self.addCleanup(patcher.stop)

    def run_plan(self, base, test, maze_toml='[maze]\ncols = 8\nrows = 6\nmap = "own"\n', levels=(2,)):
        write_maze_task(base / "tasks", "m", maze_toml, test=test)
        path = base / "plan.json"
        path.write_text(json.dumps(freeze(make_spec_for(base, levels=levels))))
        return run(path, base / "bundle")

    def test_each_attempt_is_registered_gets_maze_json_in_the_check_copy_and_the_service_outlives_the_checks(self):
        test = ('#!/bin/bash\nset -euo pipefail\n'
                'python3 -c "import json; c = json.load(open(\'.maze.json\')); assert c[\'socket\'] == \'/tmp/fake-maze.sock\'; '
                'assert c[\'token\'].startswith(\'tok-\')"\n')
        with tempdir() as d:
            base = Path(d)
            bundle = self.run_plan(base, test)
            (service,) = FakeService.instances
            self.assertEqual(sorted(i for _, i in service.registered), [0, 1])  # agent id = attempt id, index = slot
            self.assertEqual(sorted(a for a, _ in service.registered),
                             [f"c000-mini-n2-s00{i}" for i in (0, 1)])
            attempts = [json.loads(l) for l in (bundle / "attempts.jsonl").read_text().splitlines()]
            self.assertEqual([a["failure"] for a in attempts], [None, None], [a.get("infra_error") for a in attempts])
            self.assertTrue(all(a["maze"]["passed"] for a in attempts))
            self.assertEqual([a["check"]["off_target"] for a in attempts], [[], []])  # the runner's own file is not a change
            # stop comes after both verdicts: every attempt checked before the service goes
            order = [e if isinstance(e, str) else e[0] for e in service.events]
            self.assertEqual(order[0], "start")
            self.assertEqual(order[-1], "stop")
            self.assertEqual(order.count("verdict"), 2)
            cell = json.loads((bundle / "cells.jsonl").read_text().splitlines()[0])
            self.assertEqual(cell["maze"]["m"]["log"], "logs/maze-c000-m.jsonl")
            self.assertEqual(cell["maze"]["m"]["summary"], {"calls": 0, "exit": None})
            # each token is bound twice: to the agent at launch, then to its checker; never to the same process
            self.assertEqual(sorted(service.bound), [f"c000-mini-n2-s00{i}" for i in (0, 1)])
            for pids in service.bound.values():
                self.assertEqual(len(pids), 2)
                self.assertNotEqual(pids[0], pids[1])
                self.assertTrue(all(isinstance(p, int) and p > 1 for p in pids))
            # the maze is built on the secret effective seed, recorded on the cell once it is over
            seed = cell["maze"]["m"]["seed"]
            self.assertEqual((seed["task"], seed["mode"]), (5, "secret"))
            self.assertEqual(service.params["seed"], seed["effective"])
            self.assertEqual(service.seed_info, seed)
            self.assertEqual(verify(bundle), [])

    def test_a_service_that_fails_to_start_fails_every_attempt_as_infra_never_silently(self):
        class Broken(FakeService):
            def start(self):
                raise OSError("address in use")

        sys.modules["swarm.maze"].MazeService = Broken
        with tempdir() as d:
            bundle = self.run_plan(Path(d), "#!/bin/bash\ntrue\n")
            attempts = [json.loads(l) for l in (bundle / "attempts.jsonl").read_text().splitlines()]
            self.assertEqual([a["failure"] for a in attempts], ["infra", "infra"])
            self.assertEqual([a["infra_reason"] for a in attempts], ["maze_unavailable"] * 2)
            self.assertIn("address in use", attempts[0]["infra_error"])
            cell = json.loads((bundle / "cells.jsonl").read_text().splitlines()[0])
            self.assertIn("address in use", cell["maze"]["m"]["start_error"])
            self.assertTrue(json.loads((bundle / "summary.json").read_text())["complete"])

    def test_infra_reason_codes(self):
        from swarm.runner import LimitsNotApplied, MazeUnavailable
        self.assertEqual(infra_reason(MazeUnavailable("x"), "sandbox"), "maze_unavailable")
        self.assertEqual(infra_reason(LimitsNotApplied("x"), "run"), "limits_unavailable")
        self.assertEqual(infra_reason(RuntimeError("x"), "run"), "runner_error")


@unittest.skipIf(real_maze is None, "swarm.maze is not present")
class RealMazeRunTests(unittest.TestCase):
    """mini against the real service on a small maze, then against the sealed tasks when they exist."""

    def setUp(self):
        # mini stops itself after 300 turns by default; a maze run is as long as the maze.
        original = harnesses.get("mini").__class__.argv

        def argv(this, binary, prompt, workdir, model, effort):
            return original(this, binary, prompt, workdir, model, effort) + ["--max-turns", "6000"]

        patcher = mock.patch.object(harnesses.get("mini").__class__, "argv", argv)
        patcher.start()
        self.addCleanup(patcher.stop)

    def run_tasks(self, base, task_paths, levels, max_requests=4100, top=""):
        path = base / "plan.json"
        path.write_text(json.dumps(freeze(make_spec_for(base, tasks=[str(p) for p in task_paths], levels=levels,
                                                        max_requests=max_requests, top=top))))
        return run(path, base / "bundle")

    def solo_cell(self, top=""):
        """One scripted Ante alone on a small own-map maze: (task params, the cell's maze entry, the summary file)."""
        with tempdir() as d:
            base = Path(d)
            root = write_maze_task(base / "tasks", "tiny-solo", '[maze]\ncols = 10\nrows = 8\nseed = 7\nmap = "own"\n')
            bundle = self.run_tasks(base, [root], levels=(1,), top=top)
            attempts = [json.loads(l) for l in (bundle / "attempts.jsonl").read_text().splitlines()]
            self.assertEqual([a["failure"] for a in attempts], [None], [a.get("infra_error") for a in attempts])
            cell = json.loads((bundle / "cells.jsonl").read_text().splitlines()[0])
            entry = cell["maze"]["tiny-solo"]
            return load_task(root).maze, entry, json.loads((bundle / entry["summary_file"]).read_text())

    def test_offline_regeneration_from_task_toml_is_not_the_served_maze_and_the_cell_replays(self):
        params, entry, summary = self.solo_cell()
        seed = entry["seed"]
        self.assertEqual((seed["task"], seed["mode"]), (7, "secret"))
        self.assertNotEqual(seed["effective"], 7)
        effective = {**params, "seed": seed["effective"]}
        self.assertEqual(summary["params"], effective)
        self.assertEqual(summary["seed"], seed)
        # What an agent could build from task.toml or plan.json is a different maze from the one it walked.
        offline, served = real_maze.generate(params), real_maze.generate(effective)
        self.assertNotEqual(bytes(offline.walls), bytes(served.walls))
        self.assertEqual(summary["route"], served.shortest_route())
        # The recorded seed replays the cell: the simulation on it matches the real run call for call.
        sim = real_maze.simulate(effective, 1)
        self.assertEqual((summary["calls"], summary["unique_cells"]), (sim["calls"], sim["unique_cells"]))

    def test_plain_seed_mode_serves_the_task_seed_for_the_cross_check(self):
        params, entry, summary = self.solo_cell(top='maze_seed = "plain"')
        self.assertEqual(entry["seed"], {"task": 7, "effective": 7, "mode": "plain"})
        self.assertEqual(summary["route"], real_maze.generate(params).shortest_route())
        self.assertEqual(summary["calls"], real_maze.simulate(params, 1)["calls"])

    def test_an_exception_mid_cell_still_stops_the_services_and_closes_the_group(self):
        from swarm import limits as cgroup_limits, runner as runner_module
        services = []

        class Tracked(real_maze.MazeService):
            def __init__(self, *a, **k):
                super().__init__(*a, **k)
                services.append(self)

        class FakeGroup:
            name, closed = "c000-fake", False

            def wrap(self, argv):
                return list(argv)

            def stats(self):
                return {}

            def close(self):
                self.closed = True
                return True

        group = FakeGroup()
        with tempdir() as d, mock.patch.object(real_maze, "MazeService", Tracked), \
                mock.patch.object(cgroup_limits, "probe", return_value=None), \
                mock.patch.object(cgroup_limits, "prepare", return_value=Path(d)), \
                mock.patch.object(cgroup_limits, "release", return_value={}), \
                mock.patch.object(cgroup_limits.CellGroup, "create", return_value=group), \
                mock.patch.object(runner_module, "_stop_mazes", side_effect=RuntimeError("injected mid-cell")):
            base = Path(d)
            root = write_maze_task(base / "tasks", "tiny-crash", '[maze]\ncols = 6\nrows = 5\nseed = 1\nmap = "own"\n')
            path = base / "plan.json"
            path.write_text(json.dumps(freeze(make_spec_for(base, tasks=[str(root)], levels=(1,), max_requests=4100,
                                                            extra="[limits]\ncpu = 1.0\n"))))
            bundle = run(path, base / "bundle")
            summary = json.loads((bundle / "summary.json").read_text())
            self.assertIn("injected mid-cell", summary["crashed"])
            (svc,) = services
            self.assertFalse(svc.running)
            self.assertFalse(svc._thread.is_alive())
            self.assertEqual(svc._server.socket.fileno(), -1)          # the listener is closed
            self.assertFalse(Path(svc.socket_path).exists())
            self.assertFalse(Path(svc.socket_path).parent.exists())
            self.assertTrue(svc.log_path.exists())                    # log and summary written on this path too
            self.assertEqual(json.loads(svc.log_path.with_suffix(".json").read_text())["calls"], svc.summary()["calls"])
            self.assertTrue(group.closed)

    def check_bundle(self, bundle, size, shared):
        attempts = [json.loads(l) for l in (bundle / "attempts.jsonl").read_text().splitlines()]
        self.assertEqual(len(attempts), size)
        cell = json.loads((bundle / "cells.jsonl").read_text().splitlines()[0])
        (task_id, mz), = cell["maze"].items()
        summary = mz["summary"]
        self.assertIsNotNone(summary["exit"], "the swarm never reached the exit")
        for a in attempts:
            verdict = a["maze"]
            self.assertTrue(verdict["legal"], a["attempt"])
            self.assertTrue(verdict["within_limit"], a["attempt"])
            self.assertEqual(a["check"]["off_target"], [])
            if shared:
                self.assertTrue(verdict["exit_reached"])  # the cell reached the exit, for everyone's verdict
        if not shared:
            # alone: an attempt passes exactly when that agent reached the exit itself
            self.assertEqual(sum(a["passed"] for a in attempts), sum(a["maze"]["passed"] for a in attempts))
        # call counts in the requests match the service log: one bash request per ./maze call, the verdict excepted
        rows = [json.loads(l) for l in (bundle / mz["log"]).read_text().splitlines() if l.strip()]
        logged = [r for r in rows if r["op"] != "verdict"]
        asked = sum(sum(1 for r in read_rows(bundle, a["attempt"]) if r.get("op") == "bash") for a in attempts)
        self.assertEqual(len(logged), asked)
        self.assertEqual(len(logged), summary["calls"])
        for a in attempts:
            mine = sum(1 for r in logged if r["agent"] == a["attempt"])
            self.assertEqual(mine, a["maze"]["calls"])
            self.assertEqual(mine, summary["per_agent"][a["attempt"]]["calls"])
        self.assertEqual(verify(bundle), [])
        return attempts, summary

    def test_small_shared_maze_four_agents_through_the_real_service(self):
        with tempdir() as d:
            base = Path(d)
            write_maze_task(base / "tasks", "tiny-shared", '[maze]\ncols = 10\nrows = 8\nseed = 11\nmap = "shared"\n')
            bundle = self.run_tasks(base, [base / "tasks" / "tiny-shared"], levels=(4,))
            attempts, summary = self.check_bundle(bundle, 4, shared=True)
            self.assertEqual([a["failure"] for a in attempts], [None] * 4, [a.get("infra_error") for a in attempts])

    def test_small_own_maze_two_agents_each_reach_the_exit_alone(self):
        with tempdir() as d:
            base = Path(d)
            write_maze_task(base / "tasks", "tiny-own", '[maze]\ncols = 8\nrows = 6\nseed = 3\nmap = "own"\n')
            bundle = self.run_tasks(base, [base / "tasks" / "tiny-own"], levels=(2,))
            attempts, summary = self.check_bundle(bundle, 2, shared=False)
            self.assertEqual([a["failure"] for a in attempts], [None] * 2, [a.get("infra_error") for a in attempts])

    def test_sealed_branchy_shared_at_size_four(self):
        task = ROOT / "tasks" / "maze-branchy-shared"
        if not (task / "task.toml").exists():
            self.skipTest("tasks/maze-branchy-shared is not present")
        with tempdir() as d:
            # Plain seed: the sealed maze as written, so the run length is fixed (a secret seed draws a new maze
            # each time, and a long draw can outrun the attempt timeout). Secret seeds are tested on small mazes.
            bundle = self.run_tasks(Path(d), [task], levels=(4,), top='maze_seed = "plain"')
            attempts, _ = self.check_bundle(bundle, 4, shared=True)
            self.assertEqual([a["failure"] for a in attempts], [None] * 4, [a.get("infra_error") for a in attempts])

    def test_sealed_branchy_own_at_size_two(self):
        task = ROOT / "tasks" / "maze-branchy-own"
        if not (task / "task.toml").exists():
            self.skipTest("tasks/maze-branchy-own is not present")
        with tempdir() as d:
            # Plain seed: the sealed maze as written, so the run length is fixed (a secret seed draws a new maze
            # each time, and a long draw can outrun the attempt timeout). Secret seeds are tested on small mazes.
            bundle = self.run_tasks(Path(d), [task], levels=(2,), top='maze_seed = "plain"')
            attempts, _ = self.check_bundle(bundle, 2, shared=False)
            self.assertEqual([a["failure"] for a in attempts], [None] * 2, [a.get("infra_error") for a in attempts])


def read_rows(bundle, attempt):
    path = bundle / "logs" / attempt / "requests.jsonl"
    return [json.loads(l) for l in path.read_text().splitlines() if l.strip()]


if __name__ == "__main__":
    unittest.main()
