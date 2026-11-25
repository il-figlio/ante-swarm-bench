"""Attacks found in review. Each must fail as an attack, never pass or hang."""
import json
import os
import sys
import time
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import make_spec, make_task, tempdir  # noqa: E402
from swarm.bundle import verify  # noqa: E402
from swarm.plan import freeze  # noqa: E402
from swarm.runner import run  # noqa: E402
from swarm.sandbox import execute  # noqa: E402
from swarm.scripted import ScriptedModel  # noqa: E402

PY_TEST = 'python3 -c "assert open(\'out.txt\').read() == \'ok\'"'


def run_one(base, script, levels=(1,), test=PY_TEST, allowed=("out.txt", "*.py", "**")):
    make_task(base / "tasks", "t", script, allowed=allowed, test=test)
    plan = freeze(make_spec(base, levels=levels))
    path = base / "plan.json"
    path.write_text(json.dumps(plan))
    bundle = run(path, base / "bundle")
    return bundle, [json.loads(line) for line in (bundle / "attempts.jsonl").read_text().splitlines()]


def detach(body: str) -> str:
    """A daemon in a new session with an empty environment (no SWARM_ATTEMPT tag)."""
    return ("import os, sys, time\nif os.fork():\n    sys.exit(0)\nos.setsid()\nif os.fork():\n    sys.exit(0)\n"
            "os.execve(sys.executable, [sys.executable, '-c', " + repr(body) + "], {})\n")


class AdversarialTests(unittest.TestCase):
    def test_user_site_injection_cannot_fake_a_pass(self):
        with tempdir() as d:
            base = Path(d)
            # Writes a .pth into every user-site location python might use, then does no work.
            plant = ("import site, os\npath = site.getusersitepackages()\nos.makedirs(path, exist_ok=True)\n"
                     "open(os.path.join(path, 'z.pth'), 'w').write('import os; os._exit(0)\\n')\n")
            script = [{"op": "write", "path": "plant.py", "content": plant}, {"op": "bash", "command": "python3 plant.py"},
                      {"op": "final"}]
            _, records = run_one(base, script)
            self.assertFalse(records[0]["passed"], records[0])
            self.assertEqual(records[0]["failure"], "check_failed")

    def test_detached_daemon_writing_later_cannot_fake_a_pass(self):
        with tempdir() as d:
            base = Path(d)
            body = "import time\ntime.sleep(1.5)\nopen('out.txt', 'w').write('ok')\n"
            script = [{"op": "write", "path": "d.py", "content": detach(body)},
                      {"op": "bash", "command": "python3 d.py >/dev/null 2>&1"}, {"op": "final"}]
            _, records = run_one(base, script)
            time.sleep(2)
            self.assertFalse(records[0]["passed"], records[0])

    def test_one_attempt_cannot_reach_another_attempts_script(self):
        model = ScriptedModel()
        try:
            url = model.register("victim", [{"op": "final"}], "/w", "m", "medium", 1)
            guess = url.replace(url.split("/s/")[1].split("/")[0], "victim")
            for target in (guess, f"http://127.0.0.1:{model.port}/s/c000-mini-n1-s000/v1"):
                request = urllib.request.Request(target + "/responses", data=b"{not json", headers={"Content-Type": "application/json"})
                with self.assertRaises(urllib.error.HTTPError) as caught:
                    urllib.request.urlopen(request, timeout=5)
                self.assertEqual(caught.exception.code, 404)
            rows, _ = model.release("victim")
            self.assertEqual(rows, [])
        finally:
            model.close()

    def test_process_holding_the_pipe_cannot_hang_the_run(self):
        with tempdir() as d:
            log = Path(d) / "log"
            holder = detach("import time\ntime.sleep(30)\n").replace("os.execve", "os.dup2(1, 1)\nos.execve")
            started = time.monotonic()
            result = execute([sys.executable, "-c", holder], d, {"PATH": "/usr/bin:/bin"}, 3, log)
            self.assertLess(time.monotonic() - started, 12)
            self.assertTrue(result.get("output_pipe_held") or result.get("leaked_processes"), result)
            self.assertEqual(result["termination"], "descendant_leak")

    def test_planted_links_and_hidden_files_are_seen(self):
        with tempdir() as d:
            base = Path(d)
            script = [{"op": "write", "path": "out.txt", "content": "ok"},
                      {"op": "bash", "command": "ln -s /etc/hosts planted && mkdir -p __pycache__ && echo x > __pycache__/m.pyc && touch ._x .DS_Store"},
                      {"op": "final"}]
            _, records = run_one(base, script, allowed=("out.txt",))
            changed = set(records[0]["check"]["changed"])
            self.assertTrue({"planted", "__pycache__", "__pycache__/m.pyc", "._x", ".DS_Store"} <= changed, changed)
            self.assertEqual(records[0]["failure"], "off_target")

    @unittest.skipIf(sys.platform == "darwin", "APFS rejects names that are not valid UTF-8")
    def test_non_utf8_filename_is_recorded_not_dropped(self):
        with tempdir() as d:
            base = Path(d)
            script = [{"op": "write", "path": "out.txt", "content": "ok"},
                      {"op": "bash", "command": "touch \"$(printf 'bad\\377')\""}, {"op": "final"}]
            bundle, records = run_one(base, script, allowed=("out.txt",))
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["failure"], "off_target")
            self.assertEqual(verify(bundle), [])


class ScaleTests(unittest.TestCase):
    def test_output_is_drained_when_descriptors_exceed_1024(self):
        import resource
        soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
        if hard != resource.RLIM_INFINITY and hard < 2100:
            self.skipTest("descriptor limit too low to test")
        resource.setrlimit(resource.RLIMIT_NOFILE, (max(soft, 2100), hard))
        held = [os.open(os.devnull, os.O_RDONLY) for _ in range(1100)]
        try:
            with tempdir() as d:
                log = Path(d) / "log"
                result = execute([sys.executable, "-c", "print('x' * 200000)"], d, {"PATH": "/usr/bin:/bin"}, 10, log)
                self.assertEqual(result["termination"], "exited", result)
                self.assertGreaterEqual(result["output_bytes"], 200000)
        finally:
            for fd in held:
                os.close(fd)
            resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


class AdmissionTests(unittest.TestCase):
    def test_running_attempts_keep_their_task_slots_until_released(self):
        from unittest import mock
        from swarm import host
        from swarm.runner import RUNNER_THREADS_PER_ATTEMPT, Admission
        import threading
        state = {"pressure": "normal", "available_mb": 100000, "total_mb": 0, "swap_used_mb": 0, "pressure_source": "test"}
        tasks = {"tasks_free": 900, "tasks_limit": 1000, "tasks_source": "test"}
        with mock.patch.object(host, "memory_state", return_value=state), mock.patch.object(host, "task_headroom", return_value=tasks):
            admission = Admission({"max_pressure": "normal", "reserve_mb": 0, "reserve_tasks": 100, "allow_unknown": False,
                                   "wait_timeout_s": 0.3}, None, lambda row: None)
            admission.learn("agent", 1.0, 97)  # 97 + 3 runner threads = 100 slots per attempt
            per = 97 + RUNNER_THREADS_PER_ATTEMPT
            # limit 1000, baseline 100 in use, reserve 100: room for (1000 - 100 - 100) // per attempts.
            admitted = [admission.acquire("agent", f"a{i}", threading.Event())["admitted"] for i in range(10)]
            self.assertEqual(sum(admitted), (1000 - 100 - 100) // per)
            admission.release("a0")
            self.assertTrue(admission.acquire("agent", "again", threading.Event())["admitted"])
            # A waiting launch must not block a finishing attempt from releasing its slots.
            admission.config["wait_timeout_s"] = 10
            releaser = threading.Timer(0.5, admission.release, args=("a1",))
            releaser.start()
            started = time.monotonic()
            self.assertTrue(admission.acquire("agent", "waits", threading.Event())["admitted"])
            self.assertLess(time.monotonic() - started, 5)

    def test_unheld_concurrent_acquires_record_zero_wait(self):
        from unittest import mock
        from swarm import host
        from swarm.runner import Admission
        import threading
        state = {"pressure": "normal", "available_mb": 100000, "total_mb": 0, "swap_used_mb": 0, "pressure_source": "test"}
        tasks = {"tasks_free": 900, "tasks_limit": 1000, "tasks_source": "test"}

        def slow_state():
            time.sleep(0.01)
            return state

        with mock.patch.object(host, "memory_state", side_effect=slow_state), mock.patch.object(host, "task_headroom", return_value=tasks):
            admission = Admission({"max_pressure": "normal", "reserve_mb": 0, "reserve_tasks": 100, "allow_unknown": False,
                                   "wait_timeout_s": 5}, None, lambda row: None)
            decisions = []
            threads = [threading.Thread(target=lambda i=i: decisions.append(admission.acquire("mini", f"a{i}", threading.Event()))) for i in range(4)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(len(decisions), 4)
        self.assertTrue(all(d["admitted"] and d["held_for"] == [] and d["wait_s"] == 0.0 for d in decisions), decisions)


class SpecAndVerifyTests(unittest.TestCase):
    def test_unknown_keys_and_missing_allowed_changes_are_rejected(self):
        with tempdir() as d:
            base = Path(d)
            make_task(base / "tasks", "t", [{"op": "final"}])
            spec = make_spec(base, extra="")
            spec.write_text(spec.read_text().replace("[budget]", "[budget]\nmax_usd = 5"))
            with self.assertRaisesRegex(ValueError, "max_usd"):
                freeze(spec)
            toml = base / "tasks" / "t" / "task.toml"
            toml.write_text(toml.read_text().replace('allowed_changes = ["out.txt"]\n', ""))
            from swarm.tasks import load_task
            with self.assertRaisesRegex(ValueError, "allowed_changes"):
                load_task(base / "tasks" / "t")

    def test_verify_rejects_a_summary_claiming_completeness(self):
        with tempdir() as d:
            base = Path(d)
            script = [{"op": "write", "path": "out.txt", "content": "ok"}, {"op": "final"}]
            bundle, _ = run_one(base, script, levels=(2,))
            summary = json.loads((bundle / "summary.json").read_text())
            summary["complete"] = False
            (bundle / "summary.json").write_text(json.dumps(summary))
            problems = verify(bundle)
            self.assertTrue(any("complete" in p for p in problems), problems)


if __name__ == "__main__":
    unittest.main()
