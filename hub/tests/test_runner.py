import json, shlex, sys, tempfile, unittest
from pathlib import Path
from unittest import mock
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "runner"))
import swarm_run as sr

class P:  # fake CompletedProcess
    def __init__(self, rc=0, out="", err=""): self.returncode, self.stdout, self.stderr = rc, out, err

class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp()); self.R = sr.Run(self.tmp)

    def test_parse_extracts_tokens_check_and_files(self):
        out = "RC=0\npeak_rss_kb=25600 user_s=0.1 sys_s=0.0 wall_s=9.1\nCHECK=OK\nTOKENS=1200 34 0\nFILES=toolbox/slugify.py \n"
        r = sr.parse(out)
        self.assertEqual(r["rc"], "0"); self.assertEqual(r["check"], "OK"); self.assertEqual(r["files"], "toolbox/slugify.py")
        self.assertEqual(r["tokens"], {"input": 1200, "output": 34, "cache_read": 0})

    def test_parse_without_tokens_is_none(self):
        self.assertIsNone(sr.parse("RC=124\nCHECK=\nTOKENS=\nFILES=\n")["tokens"])

    def test_ensure_cell_tracks_name_before_create_and_on_failure(self):
        with mock.patch.object(sr, "ssh", return_value=(P(255), 0.1)), mock.patch.object(sr, "exe", return_value=(P(1, "", "quota"), 0.2)):
            ok = sr.ensure_cell("bench-c-01", self.R)
        self.assertFalse(ok); self.assertIn("bench-c-01", self.R.cells)

    def test_destroy_all_removes_every_tracked_cell_once(self):
        self.R.track("bench-c-01"); self.R.track("bench-c-02"); self.R.track("bench-c-01")
        calls = []
        with mock.patch.object(sr, "exe", side_effect=lambda cmd, timeout=120: (calls.append(cmd), (P(0, '{"deleted":[]}'), 0.1))[1]):
            sr.destroy_all(self.R)
        self.assertEqual(sorted(calls), ["rm bench-c-01 --json", "rm bench-c-02 --json"])

    def test_cancel_watcher_kills_ante_on_tracked_cells(self):
        self.R.track("bench-c-01"); (self.tmp / "cancel").write_text("now")
        kills = []
        with mock.patch.object(sr, "ssh", side_effect=lambda host, cmd, timeout=600: (kills.append((host, cmd)), (P(0), 0.1))[1]):
            sr.cancel_watcher(self.R)
        self.assertTrue(self.R.cancel_flag.is_set()); self.assertEqual(kills[0][0], "bench-c-01.exe.xyz"); self.assertIn("/tmp/pid-*", kills[0][1])
        self.assertTrue(any(r["action"] == "hard cancel" for r in self.R.rows))

    def test_ante_cmd_passes_model_and_json_output(self):
        c = sr.ante_cmd("~/workspace/t", "t01", "do it", "tests.t", 60, "medium", "openai/gpt-5.6-luna")
        self.assertIn("-m openai/gpt-5.6-luna", c); self.assertIn("--output-format json", c); self.assertIn("timeout -k 10 60", c)

    def test_prompt_survives_bash_c_wrapper(self):
        # r21/r23 regression: an unquoted inner command let the remote shell cut the prompt at its
        # first word and run its backticks as substitution. Emulate that one remote parse.
        prompt = "Implement slugify in toolbox/slugify.py so tests pass. Run `python3 -m unittest t` now."
        for harness in ("ante", "pi", "hermes"):
            toks = shlex.split(sr.ante_cmd("~/workspace/t", "t01", prompt, "tests.t", 60, "medium", "", harness))
            inner = toks[toks.index("bash") + 2]  # the single word after `bash -c`
            argv = shlex.split(inner.split("exec ", 1)[1])
            self.assertIn(prompt, argv, harness)

    def test_log_is_appended_as_jsonl(self):
        self.R.log("brain", "start", 0, "ok", "x")
        self.assertEqual(json.loads((self.tmp / "log.jsonl").read_text().strip())["action"], "start")

    def test_reconcile_requeues_infra_loss_then_succeeds(self):
        task = {"id": "t01", "prompt": "p", "check": "t"}
        outcomes = iter([
            {"task": "t01", "cell": "c1", "passed": False, "failure": "infra", "requeue": "cell_lost"},
            {"task": "t01", "cell": "c1", "passed": True, "failure": "", "requeue": None},
        ])
        results, superseded = sr.reconcile([task], 1, lambda i, item: next(outcomes), self.R)
        self.assertEqual(len(results), 1); self.assertTrue(results[0]["passed"]); self.assertEqual(results[0]["attempt"], 2)
        self.assertEqual(len(superseded), 1); self.assertEqual(superseded[0]["requeue"], "cell_lost")

    def test_reconcile_caps_at_two_attempts(self):
        task = {"id": "t01", "prompt": "p", "check": "t"}
        def spawn(i, item): return {"task": "t01", "cell": "c1", "passed": False, "failure": "infra", "requeue": "fetch_failed"}
        results, superseded = sr.reconcile([task], 1, spawn, self.R)
        self.assertEqual(len(results), 1); self.assertFalse(results[0]["passed"]); self.assertEqual(results[0]["attempt"], 2)
        self.assertEqual(len(superseded), 1)  # attempt 1 superseded; attempt 2 kept as the terminal row

    def test_reconcile_does_not_retry_task_failures(self):
        task = {"id": "t01", "prompt": "p", "check": "t"}
        def spawn(i, item): return {"task": "t01", "cell": "c1", "passed": False, "failure": "check_failed", "requeue": None}
        results, superseded = sr.reconcile([task], 1, spawn, self.R)
        self.assertEqual(len(results), 1); self.assertEqual(results[0]["attempt"], 1); self.assertEqual(superseded, [])

    def test_reconcile_drains_queue_across_workers(self):
        tasks = [{"id": f"t{i:02d}", "prompt": "p", "check": "t"} for i in range(6)]
        def spawn(i, item): return {"task": item["task"]["id"], "cell": f"c{i}", "passed": True, "failure": "", "requeue": None}
        results, superseded = sr.reconcile(tasks, 3, spawn, self.R)
        self.assertEqual([r["task"] for r in results], [f"t{i:02d}" for i in range(6)])

    def test_kill_instance_marks_label_for_requeue(self):
        self.R.instances["bench-c-01/i02"] = ("bench-c-01.exe.xyz", "t02")
        with mock.patch.object(sr, "ssh", return_value=(P(0), 0.1)) as m:
            sr.kill_instance(self.R, "bench-c-01/i02")
        self.assertIn("bench-c-01/i02", self.R.killed); self.assertIn("pid-t02", m.call_args[0][1])

    def args(self):
        return mock.Mock(harness="ante", timeout=60, effort="medium", model="")

    def test_run_one_ship_failure_requeues_as_infra(self):
        with mock.patch.object(sr, "ship_base", return_value=False):
            r = sr.run_one("h", "c1", "~/w/t", {"id": "t01", "prompt": "p", "check": "t"}, self.tmp, self.R, self.args())
        self.assertFalse(r["passed"]); self.assertEqual(r["requeue"], "ship_failed"); self.assertEqual(r["failure"], "infra")

    def test_run_one_killed_instance_requeues(self):
        self.R.killed["c1"] = "t01"
        out = "RC=143\npeak_rss_kb=1 user_s=0 sys_s=0 wall_s=1\nCHECK=\nTOKENS=\nFILES=\n"
        with mock.patch.object(sr, "ship_base", return_value=True), \
             mock.patch.object(sr, "ssh", return_value=(P(0, out), 1.0)), \
             mock.patch.object(sr, "fetch", return_value=True):
            r = sr.run_one("h", "c1", "~/w/t", {"id": "t01", "prompt": "p", "check": "t"}, self.tmp, self.R, self.args())
        self.assertEqual(r["requeue"], "killed")

    def test_run_one_dropped_ssh_requeues_as_cell_lost(self):
        with mock.patch.object(sr, "ship_base", return_value=True), \
             mock.patch.object(sr, "ssh", return_value=(P(255, "", "connection lost"), 1.0)), \
             mock.patch.object(sr, "fetch", return_value=True):
            r = sr.run_one("h", "c1", "~/w/t", {"id": "t01", "prompt": "p", "check": "t"}, self.tmp, self.R, self.args())
        self.assertEqual(r["requeue"], "cell_lost")

    def test_split_events_parses_blocks_and_preserves_metrics(self):
        out = "1024 12.5 1\n0 0 0\n2048\n0.50\n__EVENTS__\n@@F t01-01 4\n{\"type\":\"x\"}\n@@F t02-02 12\npartial-line"
        body, rows = sr.split_events(out)
        self.assertEqual(len(body.split()), 8)
        self.assertEqual(rows[0][:2], ("t01-01", 4)); self.assertIn('"type":"x"', rows[0][2])
        self.assertEqual(rows[1], ("t02-02", 12, "partial-line"))

    def test_ante_cmd_resets_task_branch_for_retries(self):
        c = sr.ante_cmd("~/workspace/t", "t01", "p", "t", 60, "medium", "")
        self.assertIn("git checkout -q -B task/t01 main", c)

    def test_reconcile_survives_spawn_exception(self):
        tasks = [{"id": "t1", "prompt": "p"}, {"id": "t2", "prompt": "p"}]
        calls = []
        def spawn(i, item):
            calls.append(item["task"]["id"])
            if item["task"]["id"] == "t1": raise RuntimeError("boom")
            return {"task": item["task"]["id"], "cell": "c", "passed": True}
        final, superseded = sr.reconcile(tasks, 2, spawn, self.R)
        self.assertEqual(sorted(x["task"] for x in final), ["t1", "t2"])
        t1 = next(x for x in final if x["task"] == "t1")
        self.assertFalse(t1["passed"]); self.assertEqual(t1["attempt"], 2)
        self.assertEqual(calls.count("t1"), 2)  # infra-style: one retry, then terminal

    def test_run_decodes_bytes_on_timeout(self):
        import subprocess
        with mock.patch.object(sr.subprocess, "run", side_effect=subprocess.TimeoutExpired(["x"], 1, output=b"partial", stderr=b"oops")):
            p, _ = sr.run(["x"], timeout=1)
        self.assertEqual(p.returncode, 124); self.assertIsInstance(p.stdout, str)
        self.assertEqual(p.stdout, "partial"); self.assertIn("oops", p.stderr)

if __name__ == "__main__": unittest.main()
