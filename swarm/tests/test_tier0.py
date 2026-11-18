"""Tier 0 packs ($0 harness-only tasks) and the startup / per-step timing they report."""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import ROOT, tempdir  # noqa: E402
from swarm.bundle import verify  # noqa: E402
from swarm.plan import freeze  # noqa: E402
from swarm.runner import run, timing  # noqa: E402
from swarm.summary import summarize  # noqa: E402
from swarm.tasks import load_task  # noqa: E402
from swarm.util import tree_hash  # noqa: E402

TIER0 = ["startup", "trivial", "tool-loop-50", "session-250", "triage-20", "triage-60"]


class GeneratorTests(unittest.TestCase):
    def test_committed_packs_match_the_generator(self):
        with tempdir() as d:
            subprocess.run([sys.executable, "-I", str(ROOT / "tools" / "make_tier0_tasks.py"), d], check=True, capture_output=True)
            for name in TIER0:
                self.assertEqual(tree_hash(Path(d) / name), tree_hash(ROOT / "tasks" / name), name)

    def test_triage_has_one_right_answer(self):
        for name, count in (("triage-20", 20), ("triage-60", 60)):
            logs = sorted((ROOT / "tasks" / name / "workspace" / "logs").iterdir())
            self.assertEqual(len(logs), count)
            counts = [sum("ERROR" in line for line in p.read_text().splitlines()) for p in logs]
            self.assertEqual(counts.count(max(counts)), 1, "the busiest file must be unique")
            self.assertTrue(all(8000 < p.stat().st_size < 9600 for p in logs), [p.stat().st_size for p in logs])
            script = load_task(ROOT / "tasks" / name).script
            best = logs[counts.index(max(counts))].name
            self.assertEqual(json.loads(script[-2]["content"]), {"file": best, "errors": max(counts)})


class TimingTests(unittest.TestCase):
    def test_timing_reads_launch_gaps_and_exit_from_rows(self):
        rows = [{"recv_mono": 10.5, "sent_mono": 10.6, "op": "bash"},
                {"kind": "probe", "recv_mono": 10.55},
                {"recv_mono": 10.9, "sent_mono": 11.0, "op": "bash"},
                {"recv_mono": 11.2, "sent_mono": 11.25, "op": "final"}]
        got = timing(rows, launched=10.0, wall_s=1.5)
        self.assertEqual(got["first_request_s"], 0.5)
        self.assertEqual(got["step_gaps"], 2)
        self.assertAlmostEqual(got["step_gap_s_median"], 0.25, places=6)
        self.assertAlmostEqual(got["step_gap_s_p90"], 0.29, places=6)
        self.assertEqual(got["after_final_s"], 0.25)
        self.assertIsNone(got["step_gap_s_early"])
        self.assertIsNone(got["context_slope"])

    def test_unknowns_stay_null(self):
        self.assertEqual(timing([], launched=1.0, wall_s=None), {
            "first_request_s": None, "step_gap_s_median": None, "step_gap_s_p90": None,
            "step_gap_s_early": None, "step_gap_s_late": None, "step_gaps": 0, "after_final_s": None,
            "request_bytes_first": None, "request_bytes_mid": None, "request_bytes_last": None,
            "context_bytes_mid": None, "context_bytes_last": None, "context_slope": None,
        })

    def test_old_attempts_keep_their_summary_shape(self):
        record = {"harness": "mini", "level": 1, "passed": True, "failure": None, "admission": {"wait_s": 0},
                  "agent": {"wall_s": 1.0, "cpu_s": 0.1, "max_process_rss_kb": 1024}, "requests": {"count": 2}}
        cells = [{"harness": "mini", "level": 1, "batch_wall_s": 1.0, "max_running": 1}]
        group = summarize([record], cells)["groups"]["mini@1"]
        self.assertNotIn("first_request_s_median", group)
        timed = dict(record, timing={"first_request_s": 0.2, "step_gap_s_median": 0.01, "step_gaps": 1, "after_final_s": 0.05},
                     requests={"count": 2, "synthetic_input_tokens": 300})
        group = summarize([timed], cells)["groups"]["mini@1"]
        self.assertEqual(group["first_request_s_median"], 0.2)
        self.assertEqual(group["synthetic_input_tokens_median"], 300)
        self.assertNotIn("step_gap_s_p90", group)
        self.assertNotIn("context_slope_median", group)
        self.assertNotIn("model_turns_median", group)
        thin = dict(record, admission={"wait_s": 0.4}, timing={"first_request_s": 0.2})
        held = summarize([thin], [{"harness": "mini", "level": 1, "batch_wall_s": 1.0, "max_running": 0}])
        self.assertIsNone(held["groups"]["mini@1"]["successes_per_minute"])
        filled = summarize([timed], cells)
        self.assertIsNotNone(filled["groups"]["mini@1"]["successes_per_minute"])


class Tier0EndToEndTests(unittest.TestCase):
    def test_every_pack_passes_with_its_own_script_and_reports_timing(self):
        with tempdir() as d:
            base = Path(d)
            for name in TIER0:
                shutil.copytree(ROOT / "tasks" / name, base / "tasks" / name)
            spec = base / "spec.toml"
            # One harness, level 1, six rounds: the plan rotates tasks by cell, so each pack runs once.
            spec.write_text('''question = "tier0 packs"
primary_outcome = "successes_per_minute"
model = { source = "scripted", id = "m-1", effort = "medium", max_output_tokens = 16384 }
harnesses = ["mini"]
levels = [1]
repeats = 6
tasks = ["tasks/*"]
seed = 5
stop_rule = "all cells once"
[budget]
max_requests_per_attempt = 260
attempt_timeout_s = 300
[admission]
reserve_mb = 0
max_pressure = "warn"
''')
            plan = freeze(spec)
            path = base / "plan.json"
            path.write_text(json.dumps(plan))
            bundle = run(path, base / "bundle")
            attempts = [json.loads(line) for line in (bundle / "attempts.jsonl").read_text().splitlines()]
            self.assertEqual(sorted(a["task"] for a in attempts), sorted(TIER0))
            for a in attempts:
                self.assertTrue(a["passed"], (a["task"], a["failure"]))
                t = a["timing"]
                self.assertGreater(t["first_request_s"], 0)
                self.assertGreaterEqual(t["after_final_s"], 0)
                self.assertEqual(t["step_gaps"], a["requests"]["model_turns"] - 1)
            tokens = {a["task"]: a["requests"]["synthetic_input_tokens"] for a in attempts}
            self.assertGreater(tokens["triage-60"], 2 * tokens["triage-20"], "resent context should grow faster than file count")
            self.assertEqual(verify(bundle), [])
            self.assertIn("Startup and per-step overhead", (bundle / "report.html").read_text())

    def test_collapsed_tool_loop_is_one_program(self):
        with tempdir() as d:
            base = Path(d)
            shutil.copytree(ROOT / "tasks" / "tool-loop-50", base / "tasks" / "tool-loop-50")
            spec = base / "spec.toml"
            spec.write_text('''question = "collapsed tool loop"
primary_outcome = "step_gap_s"
protocol = "collapsed"
model = { source = "scripted", id = "m-1", effort = "medium", max_output_tokens = 16384 }
harnesses = ["mini"]
levels = [1]
repeats = 1
tasks = ["tasks/tool-loop-50"]
seed = 5
stop_rule = "all cells once"
[budget]
max_requests_per_attempt = 8
attempt_timeout_s = 60
[admission]
reserve_mb = 0
max_pressure = "warn"
''')
            path = base / "plan.json"
            path.write_text(json.dumps(freeze(spec)))
            bundle = run(path, base / "bundle")
            attempt = json.loads((bundle / "attempts.jsonl").read_text().splitlines()[0])
            self.assertTrue(attempt["passed"], attempt["failure"])
            self.assertEqual(attempt["requests"]["model_turns"], 2)
            self.assertEqual(verify(bundle), [])


if __name__ == "__main__":
    unittest.main()
