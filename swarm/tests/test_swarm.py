import json
import os
import shutil
import sys
import tempfile
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import OK_SCRIPT, make_spec, make_task, tempdir  # noqa: E402
from swarm import harnesses  # noqa: E402
from swarm.bundle import verify  # noqa: E402
from swarm.plan import check_plan, freeze  # noqa: E402
from swarm.runner import run  # noqa: E402
from swarm.scripted import ScriptedModel  # noqa: E402
from swarm.tasks import load_task  # noqa: E402


def post(url, body):
    request = urllib.request.Request(url + "/responses", data=json.dumps(body).encode(), headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, json.loads(response.read())
    except urllib.error.HTTPError as error:
        return error.code, json.loads(error.read() or b"{}")


def plan_file(base, **kwargs):
    plan = freeze(make_spec(base, **kwargs))
    path = base / "plan.json"
    path.write_text(json.dumps(plan))
    return plan, path


class TaskTests(unittest.TestCase):
    def test_script_must_end_with_final_and_stay_inside_workdir(self):
        with tempdir() as d:
            for bad in ([{"op": "bash", "command": "x"}], [{"op": "write", "path": "../x", "content": ""}, {"op": "final"}],
                        [{"op": "nope"}, {"op": "final"}]):
                root = make_task(Path(d) / str(len(os.listdir(d))), "t", bad)
                with self.assertRaises(ValueError):
                    load_task(root)


class ScriptedModelTests(unittest.TestCase):
    def setUp(self):
        self.model = ScriptedModel()
        self.addCleanup(self.model.close)

    def body(self, tools, outputs=0, **over):
        items = [{"role": "user", "content": "go"}] + [{"type": "function_call_output", "call_id": str(i), "output": ""} for i in range(outputs)]
        return dict({"model": "m", "reasoning": {"effort": "medium"}, "max_output_tokens": 100, "input": items,
                     "tools": [{"type": "function", "name": n} for n in tools]}, **over)

    def test_maps_steps_to_each_harness_native_tools(self):
        for tools, expected in ((["Read", "Write", "Bash"], ["Read", "Write", "Bash"]),
                                (["read_file", "write_file", "terminal"], ["read_file", "write_file", "terminal"])):
            url = self.model.register(expected[0], OK_SCRIPT, "/w", "m", "medium", 100)
            names = [post(url, self.body(tools, i))[1]["output"][0].get("name") for i in range(3)]
            self.assertEqual(names, expected)
            final = post(url, self.body(tools, 3))[1]["output"][0]
            self.assertEqual(final["type"], "message")
            rows, final_served = self.model.release(expected[0])
            self.assertTrue([r for r in rows if r.get("tool") in ("Write", "write_file")])
            self.assertTrue(final_served)

    def test_rejects_settings_mismatch_and_missing_tools(self):
        url = self.model.register("a", OK_SCRIPT, "/w", "m", "medium", 100)
        self.assertEqual(post(url, self.body(["Read"], reasoning={"effort": "high"}))[0], 422)
        self.assertEqual(post(url, self.body(["Skill"]))[0], 400)  # step 0 is a read; no read or shell tool offered
        errors = [r["error"] for r in self.model.release("a")[0]]
        self.assertEqual(errors, ["settings_mismatch", "tool_unavailable"])

    def test_output_cap_above_plan_is_rejected_below_is_flagged(self):
        url = self.model.register("c", OK_SCRIPT, "/w", "m", "medium", 100)
        self.assertEqual(post(url, self.body(["Read"], max_output_tokens=200))[0], 422)
        self.assertEqual(post(url, self.body(["Read"], max_output_tokens=60))[0], 200)
        self.assertEqual(post(url, self.body(["Read"], max_output_tokens=True))[0], 422)
        rows = self.model.release("c")[0]
        self.assertEqual([r.get("error") for r in rows], ["settings_mismatch", None, "settings_mismatch"])
        self.assertTrue(rows[1]["cap_lowered"])

    def test_request_budget_is_enforced(self):
        url = self.model.register("b", OK_SCRIPT, "/w", "m", "medium", 100, max_requests=2)
        statuses = [post(url, self.body(["Read"]))[0] for _ in range(3)]
        self.assertEqual(statuses, [200, 200, 429])

    def test_count_tokens_does_not_spend_the_request_budget(self):
        url = self.model.register("tok", OK_SCRIPT, "/w", "m", "medium", 100, max_requests=2)
        request = urllib.request.Request(url + "/messages/count_tokens", data=json.dumps({"model": "m", "messages": []}).encode(),
                                         headers={"Content-Type": "application/json"})
        with urllib.request.urlopen(request, timeout=10) as response:
            self.assertEqual(response.status, 200)
        statuses = [post(url, self.body(["Read"]))[0] for _ in range(2)]
        self.assertEqual(statuses, [200, 200])
        self.assertEqual(post(url, self.body(["Read"]))[0], 429)
        rows = self.model.release("tok")[0]
        self.assertEqual([r.get("kind") for r in rows], ["count_tokens", "responses", "responses", "responses"])
        self.assertFalse(rows[0]["billable"])
        self.assertTrue(all(r["billable"] for r in rows[1:]))


def read_rows(bundle):
    """{attempt id: its request rows} from a bundle's logs."""
    return {p.parent.name: [json.loads(line) for line in p.read_text().splitlines()] for p in sorted((bundle / "logs").glob("*/requests.jsonl"))}


# Attempts as bundles recorded before the anatomy fields existed hold them; the recorded hash is of the summary the
# code produced before those fields (computed from the previous summary.py), so old bundles still verify.
OLD_SHAPE_ATTEMPTS = [
    {"harness": "mini", "level": 2, "passed": passed, "failure": None if passed else "check_failed", "admission": {"wait_s": 0.0},
     "agent": {"wall_s": 1.0 + i, "cpu_s": 0.5, "max_process_rss_kb": 30720, "peak_tree_rss_kb": 40960, "disk_kb": 8},
     "requests": {"count": 4, "model_turns": 4, "probes": 0, "synthetic_input_tokens": 100 * (i + 1)},
     "timing": {"first_request_s": 0.1 * (i + 1), "step_gap_s_median": 0.01, "after_final_s": 0.02}}
    for i, passed in enumerate((True, True, False))]
OLD_SHAPE_CELLS = [{"harness": "mini", "level": 2, "batch_wall_s": 3.0, "max_running": 2, "first_pass_s": 1.5}]
OLD_SHAPE_SUMMARY_SHA256 = "a4fc01d2b60fefd74b45285efa565922d12cc63dc4a92a90ba621573201fe91b"


class SummaryShapeTests(unittest.TestCase):
    def test_summary_of_old_shape_attempts_is_unchanged(self):
        from swarm.summary import summarize
        from swarm.util import canonical, sha256_bytes
        summary = summarize(OLD_SHAPE_ATTEMPTS, OLD_SHAPE_CELLS)
        self.assertEqual(sha256_bytes(canonical(summary)), OLD_SHAPE_SUMMARY_SHA256)
        group = summary["groups"]["mini@2"]
        self.assertFalse([k for k in group if k.startswith("first_request_b") or "canary" in k or "cache_write" in k])

    def test_sealed_010_maze_bundle_verifies_with_its_recorded_summary(self):
        from swarm.bundle import Bundle
        from swarm.plan import plan_digest
        from swarm.summary import summarize
        from swarm.util import append_jsonl, write_json

        with tempdir() as td:
            bundle = Bundle.create(Path(td) / "old-maze")
            planned = {"index": 0, "harness": "mini", "round": 1, "level": 1}
            plan = {"swarm_version": "0.1.0", "cells": [planned]}
            write_json(bundle.root / "plan.json", dict(plan, plan_sha256=plan_digest(plan)))
            attempt = bundle.append_attempt(dict(OLD_SHAPE_ATTEMPTS[0], level=1, cell=0))
            cell = dict(OLD_SHAPE_CELLS[0], cell=0, round=1, level=1, attempted=1, max_running=1)
            # 0.1.0 summarized the ordinary cell fields and kept maze details only in cells.jsonl.
            summary = summarize([attempt], [cell])
            write_json(bundle.root / "summary.json", dict(summary, complete=True, cells_run=1))
            append_jsonl(bundle.root / "cells.jsonl", dict(cell, maze={"m": {"summary": {"calls": 2}}}))
            bundle.seal()
            self.assertEqual(verify(bundle.root), [])

    def test_new_fields_add_medians_and_the_commonest_canary(self):
        import copy
        from swarm.summary import summarize
        attempts = copy.deepcopy(OLD_SHAPE_ATTEMPTS)
        for i, record in enumerate(attempts):
            record["requests"]["first_request"] = {
                "canonical_bytes": 100 * (i + 1), "system_bytes": 10, "tools_bytes": 50, "user_bytes": 20, "instruction_bytes": None,
                "injected_bytes": None, "other_bytes": 5, "tool_count": 3, "canary": {"sha256": "bb" if i == 2 else "aa"}}
            record["requests"]["cache_write_input_tokens"] = 40 if i == 0 else None
        group = summarize(attempts, OLD_SHAPE_CELLS)["groups"]["mini@2"]
        self.assertEqual((group["first_request_bytes_median"], group["first_request_tools_bytes_median"],
                          group["first_request_instruction_bytes_median"], group["first_request_tool_count_median"]), (200, 50, None, 3))
        self.assertEqual((group["canary_sha256"], group["canary_distinct"], group["cache_write_input_tokens_total"]), ("aa", 2, 40))


class EndToEndTests(unittest.TestCase):
    def test_new_rows_chain_carry_anatomy_and_the_canary_is_stable_across_sandboxes(self):
        with tempdir() as d:
            base = Path(d)
            make_task(base / "tasks", "one", OK_SCRIPT)
            make_task(base / "tasks", "two", OK_SCRIPT)
            _, path = plan_file(base)
            bundle = run(path, base / "bundle")
            self.assertEqual(verify(bundle), [])
            rows = read_rows(bundle)
            self.assertEqual(len(rows), 3)
            for attempt, items in rows.items():
                self.assertEqual([r["request_id"] for r in items], [f"{attempt}#{i}" for i in range(len(items))])
                models = [r for r in items if r.get("api")]
                self.assertIsNone(models[0]["prev_request_id"])
                self.assertIsNone(models[0]["prev_source"])
                self.assertEqual([r["prev_request_id"] for r in models[1:]], [r["request_id"] for r in models[:-1]])
                self.assertEqual({r["prev_source"] for r in models[1:]}, {"sequence"})
                self.assertTrue(all(r["upstream_id"].startswith("resp_") for r in models))
                self.assertTrue(all(r["user_agent"] for r in models))
                self.assertEqual([i for i, r in enumerate(items) if "anatomy" in r], [0])  # first model row only
            attempts = [json.loads(line) for line in (bundle / "attempts.jsonl").read_text().splitlines()]
            firsts = [a["requests"]["first_request"] for a in attempts]
            for a in attempts:
                first = a["requests"]["first_request"]
                self.assertEqual(first["wire"], "responses")
                self.assertTrue(first["request_bytes"] >= first["canonical_bytes"] > 0)
                self.assertEqual(first["system_bytes"] + first["tools_bytes"] + first["user_bytes"] + first["other_bytes"], first["canonical_bytes"])
                self.assertEqual(first["tools_offered"], sorted(first["tools_offered"]))
                self.assertEqual(first["tool_count"], len(first["tools_offered"]))
                self.assertTrue(first["instruction_bytes"])  # the task's instruction was found in the user text
                self.assertEqual(a["requests"]["cache_write_input_tokens"], None)
            # two attempts in two different sandboxes, one canary
            self.assertEqual(len({f["canary"]["sha256"] for f in firsts}), 1)
            group = json.loads((bundle / "summary.json").read_text())["groups"]["mini@2"]
            self.assertEqual((group["canary_sha256"], group["canary_distinct"]), (firsts[0]["canary"]["sha256"], 1))
            self.assertEqual(group["first_request_bytes_median"], firsts[0]["canonical_bytes"])
            self.assertNotIn("cache_write_input_tokens_total", group)

    def test_previous_response_id_links_the_row_it_names(self):
        model = ScriptedModel()
        self.addCleanup(model.close)
        url = model.register("p", OK_SCRIPT, "/w", "m", "medium", 100)
        body = lambda outputs, **kw: {"model": "m", "reasoning": {"effort": "medium"}, "max_output_tokens": 100, "tools": [{"type": "function", "name": n} for n in ("Read", "Write", "Bash")],
                                      "input": [{"role": "user", "content": "go"}] + [{"type": "function_call_output", "call_id": str(i), "output": ""} for i in range(outputs)], **kw}
        first = post(url, body(0))[1]
        post(url, body(1))
        post(url, body(2, previous_response_id=first["id"]))  # names the first row, not the latest
        rows = model.release("p")[0]
        self.assertEqual(first["id"], rows[0]["upstream_id"])
        self.assertEqual([(r["prev_request_id"], r["prev_source"]) for r in rows],
                         [(None, None), ("p#0", "sequence"), ("p#0", "previous_response_id")])

    def test_mini_run_passes_verifies_and_detects_tampering(self):
        with tempdir() as d:
            base = Path(d)
            make_task(base / "tasks", "one", OK_SCRIPT)
            make_task(base / "tasks", "two", OK_SCRIPT)
            _, path = plan_file(base)
            bundle = run(path, base / "bundle")
            summary = json.loads((bundle / "summary.json").read_text())
            self.assertTrue(summary["complete"])
            self.assertEqual([(g["level"], g["passes"], g["attempts"]) for g in summary["groups"].values()], [(1, 1, 1), (2, 2, 2)])
            self.assertEqual(verify(bundle), [])
            lines = (bundle / "attempts.jsonl").read_text().splitlines()
            record = json.loads(lines[0])
            record["passed"] = not record["passed"]
            lines[0] = json.dumps(record, sort_keys=True)
            (bundle / "attempts.jsonl").write_text("\n".join(lines) + "\n")
            self.assertTrue(verify(bundle))

    def test_wrong_answer_and_off_target_changes_fail(self):
        with tempdir() as d:
            base = Path(d)
            wrong = [{"op": "write", "path": "out.txt", "content": "no"}, {"op": "final"}]
            stray = [{"op": "write", "path": "out.txt", "content": "ok"}, {"op": "write", "path": "seed.txt", "content": "x"}, {"op": "final"}]
            make_task(base / "tasks", "wrong", wrong)
            make_task(base / "tasks", "stray", stray)
            _, path = plan_file(base, levels=(2,))
            bundle = run(path, base / "bundle")
            failures = sorted(json.loads(line)["failure"] for line in (bundle / "attempts.jsonl").read_text().splitlines())
            self.assertEqual(failures, ["check_failed", "off_target"])

    def test_a_killed_agent_is_recorded_and_its_cpu_not_compared(self):
        from swarm.sandbox import execute
        with tempdir() as d:
            base = Path(d)
            argv = [sys.executable, "-c", "import os, signal; os.kill(os.getpid(), signal.SIGKILL)"]
            result = execute(argv, base, {"PATH": os.environ["PATH"], "SWARM_ATTEMPT": f"killed-{os.getpid()}"}, 10, base / "a.log")
            self.assertEqual((result["exit_code"], result["signal"], result.get("cpu_s_partial")), (-9, 9, True))
            self.assertNotEqual(result["termination"], "oom_killed")  # no OOM happened

    def test_adapter_teardown_runs_before_the_leak_check(self):
        """A daemon the adapter stops in teardown (Hermes's gateway) is not a leak; one it leaves still is."""
        from swarm.sandbox import execute
        daemon = ("import os, sys, time\nif os.fork():\n    sys.exit(0)\nos.setsid()\nif os.fork():\n    sys.exit(0)\n"
                  "open(sys.argv[1], 'w').write(str(os.getpid()))\ntime.sleep(60)\n")
        with tempdir() as d:
            base = Path(d)
            (base / "d.py").write_text(daemon)
            env = {"PATH": os.environ["PATH"], "SWARM_ATTEMPT": f"teardown-{os.getpid()}"}

            def stop():
                for _ in range(100):
                    if (base / "pid").exists() and (base / "pid").read_text():
                        break
                    import time as _t
                    _t.sleep(0.02)
                os.kill(int((base / "pid").read_text()), 15)

            argv = [sys.executable, str(base / "d.py"), str(base / "pid")]
            result = execute(argv, base, env, 10, base / "a.log", on_exit=stop)
            self.assertEqual((result["termination"], result.get("escaped_processes", 0)), ("exited", 0))
            self.assertIn("teardown_s", result)
            (base / "pid").unlink()
            env["SWARM_ATTEMPT"] += "-b"
            result = execute(argv, base, env, 10, base / "b.log")
            if Path("/proc/self/environ").exists() or shutil.which("ps"):
                self.assertEqual(result["termination"], "descendant_leak")

    def test_detached_process_is_found_recorded_and_stopped(self):
        with tempdir() as d:
            base = Path(d)
            daemon = "import os, sys, time\nif os.fork():\n    sys.exit(0)\nos.setsid()\nif os.fork():\n    sys.exit(0)\ntime.sleep(60)\n"
            script = [{"op": "write", "path": "out.txt", "content": "ok"}, {"op": "write", "path": "daemon.py", "content": daemon},
                      {"op": "bash", "command": "python3 daemon.py >/dev/null 2>&1"}, {"op": "final"}]
            make_task(base / "tasks", "leaky", script, allowed=("out.txt", "daemon.py"))
            _, path = plan_file(base, levels=(1,))
            bundle = run(path, base / "bundle")
            record = json.loads((bundle / "attempts.jsonl").read_text().splitlines()[0])
            self.assertEqual(record["failure"], "descendant_leak")
            self.assertGreaterEqual(record["agent"]["escaped_processes"], 1)
            self.assertEqual(record["agent"]["escaped_after_cleanup"], 0)
            self.assertEqual(record["check"]["check_exit"], 0)  # the task itself was done

    def test_runner_failures_are_recorded_never_dropped(self):
        from swarm import sandbox as sandbox_module
        with tempdir() as d:
            base = Path(d)
            make_task(base / "tasks", "one", OK_SCRIPT)
            _, path = plan_file(base, levels=(2,))
            original = sandbox_module.ProcessSandbox.create
            calls = {"n": 0}

            def flaky(self, key, task):
                calls["n"] += 1
                if calls["n"] == 1:
                    raise RuntimeError("can't start new thread")
                return original(self, key, task)

            sandbox_module.ProcessSandbox.create = flaky
            try:
                bundle = run(path, base / "bundle")
            finally:
                sandbox_module.ProcessSandbox.create = original
            records = [json.loads(l) for l in (bundle / "attempts.jsonl").read_text().splitlines()]
            self.assertEqual(sorted(r["failure"] or "pass" for r in records), ["infra", "pass"])
            self.assertTrue(json.loads((bundle / "summary.json").read_text())["complete"])
            self.assertEqual(verify(bundle), [])

    def test_verify_reports_cells_with_missing_attempts(self):
        with tempdir() as d:
            base = Path(d)
            make_task(base / "tasks", "one", OK_SCRIPT)
            _, path = plan_file(base, levels=(2,))
            bundle = run(path, base / "bundle")
            cells = (bundle / "cells.jsonl").read_text().splitlines()
            cell = json.loads(cells[0])
            cell["level"] = 3
            (bundle / "cells.jsonl").write_text(json.dumps(cell) + "\n")
            self.assertTrue(any("recorded 2 attempts" in p for p in verify(bundle)))

    def test_plan_refuses_changed_task(self):
        with tempdir() as d:
            base = Path(d)
            root = make_task(base / "tasks", "one", OK_SCRIPT)
            plan, path = plan_file(base, levels=(1,))
            self.assertEqual(check_plan(plan), [])
            (root / "instruction.md").write_text("changed\n")
            with self.assertRaisesRegex(RuntimeError, "task one changed"):
                run(path, base / "bundle")

    def test_ramp_climbs_in_order_stops_on_failure_and_winds_down(self):
        with tempdir() as d:
            base = Path(d)
            make_task(base / "tasks", "ok", OK_SCRIPT)
            make_task(base / "tasks", "bad", [{"op": "write", "path": "out.txt", "content": "no"}, {"op": "final"}])
            spec = make_spec(base, levels=(1,), extra="")
            text = spec.read_text().replace("levels = [1]\n", "").replace("[budget]", "[ramp]\nstart = 1\nmax = 16\nstop_pass_rate = 0.9\n[budget]")
            spec.write_text(text)
            plan = freeze(spec)
            self.assertEqual(plan["spec"]["levels"], [1, 2, 4, 8, 16])
            self.assertEqual([c["level"] for c in plan["cells"]], [1, 2, 4, 8, 16])
            path = base / "plan.json"
            path.write_text(json.dumps(plan))
            bundle = run(path, base / "bundle")
            summary = json.loads((bundle / "summary.json").read_text())
            # n=1 runs the "bad" task (index rotation) or "ok"; n=2 always includes "bad": 1/2 < 0.9 stops the ramp.
            self.assertIn("mini", summary["ramp_stops"])
            self.assertTrue(summary["complete"])
            cells = [json.loads(l) for l in (bundle / "cells.jsonl").read_text().splitlines()]
            self.assertLessEqual(len(cells), 2)
            self.assertTrue(all(c["winddown"]["survivors"] == 0 for c in cells))
            self.assertEqual(verify(bundle), [])

    def test_ante_requires_explicit_telemetry(self):
        with self.assertRaisesRegex(ValueError, "telemetry"):
            harnesses.get("ante", {}).validate_options()
        harnesses.get("ante", {"telemetry": "off"}).validate_options()
        with self.assertRaisesRegex(ValueError, "telemetry_env"):
            harnesses.get("ante", {"telemetry": "on"}).validate_options()
        harnesses.get("ante", {"telemetry": "on", "telemetry_env": "swarm-bench-test"}).validate_options()
        env = harnesses.get("ante", {"telemetry": "on", "telemetry_env": "swarm-bench-test"}).prepare(Path(tempfile.mkdtemp()), "http://x/v1", "m", "medium", 1)
        self.assertEqual((env["ANTE_TELEMETRY"], env["ANTE_ENV"]), ("on", "swarm-bench-test"))


@unittest.skipUnless(shutil.which("ante") or Path.home().joinpath(".ante/bin/ante").exists(), "ante not installed")
class AnteTests(unittest.TestCase):
    def test_real_ante_completes_scripted_task_with_native_tools(self):
        with tempdir() as d:
            base = Path(d)
            make_task(base / "tasks", "one", OK_SCRIPT)
            _, path = plan_file(base, harnesses=("ante",), levels=(1,), extra='[harness_options.ante]\ntelemetry = "off"\n')
            bundle = run(path, base / "bundle")
            record = json.loads((bundle / "attempts.jsonl").read_text().splitlines()[0])
            self.assertTrue(record["passed"], record)
            self.assertEqual(record["requests"]["tools_used"], ["Bash", "Read", "Write"])
            self.assertLess(record["agent"]["wall_s"], 2.0)  # telemetry off: no exporter flush on exit


if __name__ == "__main__":
    unittest.main()
