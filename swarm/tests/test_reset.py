"""Lane reset: after a run or a crash the machine is put back and checked, and a dirty lane takes no run."""
import json
import os
import signal
import subprocess
import sys
import time
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from helpers import OK_SCRIPT, make_task, tempdir  # noqa: E402
from swarm import cli  # noqa: E402
from swarm import reset as lane_reset  # noqa: E402
from swarm.sandbox import ROOT_MARKER  # noqa: E402
from swarm.serve import Service  # noqa: E402

HUGE = 10**12  # MB of free disk no machine has: makes the check fail on purpose


def legacy_root(tmp: Path, name="swarm-abcd1234", attempts=("c000-hermes-n256-s000", "c000-hermes-n256-s051")) -> Path:
    """A root as the runner left it before roots carried a marker: attempt directories and nothing else."""
    root = tmp / name
    for attempt in attempts:
        (root / attempt / "home" / ".hermes").mkdir(parents=True)
        (root / attempt / "home" / ".hermes" / "partial.bin").write_bytes(b"x" * 50_000)
    return root


def marked_root(tmp: Path, name: str, **marker) -> Path:
    root = tmp / name
    (root / "c000-mini-n1-s000" / "work").mkdir(parents=True)
    (root / ROOT_MARKER).write_text(json.dumps(marker))
    return root


def dead_pid() -> int:
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait()
    return child.pid


class RootTests(unittest.TestCase):
    def test_only_runner_roots_are_recognised(self):
        with tempdir() as d:
            tmp = Path(d).resolve()
            mine = [legacy_root(tmp), marked_root(tmp, "swarm-deadbeef", pid=dead_pid()),
                    marked_root(tmp, "swarm-finished", pid=os.getpid(), finished=True)]
            (tmp / "swarm-test-abcdefgh").mkdir()                       # the test suite's own temp dirs
            (tmp / "swarm-notes12345" / "c000-mini-n1-s000").mkdir(parents=True)  # not a root name (wrong length)
            (tmp / "swarm-mixed123" / "c000-mini-n1-s000").mkdir(parents=True)
            (tmp / "swarm-mixed123" / "thesis.txt").write_text("someone's file")  # holds something that is not an attempt
            (tmp / "swarm-empty123").mkdir()                            # nothing says a runner made it
            (tmp / "other").mkdir()
            (tmp / "swarm-linked12").symlink_to(tmp / "other")
            live = marked_root(tmp, "swarm-running1", pid=os.getpid())   # a run in progress in a live runner
            born = tmp / "swarm-newborn1"                               # a root whose marker is still being written
            born.mkdir()
            (born / (ROOT_MARKER + ".tmp")).write_text("")
            unread = marked_root(tmp, "swarm-unread12")
            (unread / ROOT_MARKER).write_text("")
            found = {r["path"].name: r["in_use"] for r in lane_reset.runner_roots(tmp)}
            self.assertEqual(found, {**{p.name: False for p in mine}, live.name: True, born.name: True, unread.name: True})
            old = time.time() - 3600
            os.utime(unread, (old, old))
            self.assertFalse({r["path"].name: r["in_use"] for r in lane_reset.runner_roots(tmp)}[unread.name])

    def test_reset_removes_leftovers_records_figures_and_is_idempotent(self):
        with tempdir() as d:
            tmp, data = Path(d).resolve() / "tmp", Path(d).resolve() / "data"
            tmp.mkdir()
            legacy_root(tmp)
            marked_root(tmp, "swarm-deadbeef", pid=dead_pid())
            keep = tmp / "swarm-mixed123"
            (keep / "c000-mini-n1-s000").mkdir(parents=True)
            (keep / "notes.txt").write_text("not ours")
            record = lane_reset.reset(data, tmp=tmp, run="abc")
            self.assertEqual((record["ok"], record["problems"], record["sandbox_roots_removed"], record["sandbox_roots_failed"]),
                             (True, [], 2, 0))
            self.assertEqual((record["before"]["sandbox_roots"], record["after"]["sandbox_roots"]), (2, 0))
            self.assertGreater(record["before"]["sandbox_kb"], 90)
            self.assertIsNotNone(record["after"]["disk_free_mb"])
            self.assertEqual(sorted(p.name for p in tmp.iterdir()), ["swarm-mixed123"])  # never anything else
            self.assertEqual(json.loads((data / lane_reset.RECORD).read_text()), record)
            self.assertNotIn(str(tmp), json.dumps(record))  # figures and counts, no paths
            again = lane_reset.reset(data, tmp=tmp)
            self.assertEqual((again["ok"], again["sandbox_roots_removed"]), (True, 0))
            self.assertEqual(len((data / lane_reset.HISTORY).read_text().splitlines()), 2)

    def test_a_run_in_progress_is_left_alone_unless_forced(self):
        with tempdir() as d:
            tmp = Path(d).resolve()
            live = marked_root(tmp, "swarm-running1", pid=os.getpid())
            record = lane_reset.reset(None, tmp=tmp)
            self.assertEqual((record["ok"], record["problems"], record["sandbox_roots_in_use"]), (False, ["runner_active"], 1))
            self.assertTrue(live.exists())
            self.assertTrue(lane_reset.reset(None, tmp=tmp, force=True)["ok"])
            self.assertFalse(live.exists())

    def test_low_disk_fails_the_check(self):
        with tempdir() as d:
            record = lane_reset.reset(None, min_free_mb=HUGE, tmp=Path(d).resolve())
            self.assertEqual((record["ok"], record["problems"]), (False, ["disk_low"]))

    def test_cli_command(self):
        import contextlib
        import io
        with tempdir() as d:
            data, tmp = Path(d) / "data", Path(d).resolve() / "tmp"
            tmp.mkdir()
            root = legacy_root(tmp)
            with contextlib.redirect_stdout(io.StringIO()) as out, contextlib.redirect_stderr(io.StringIO()) as err:
                self.assertEqual(cli.main(["lane-reset", "--data", str(data), "--min-free-mb", "0", "--tmp", str(tmp)]), 0)
                self.assertEqual(cli.main(["lane-reset", "--data", str(data), "--min-free-mb", str(HUGE), "--tmp", str(tmp)]), 1)
            self.assertFalse(root.exists())
            self.assertEqual(json.loads(out.getvalue().split("\n}\n")[0] + "\n}")["sandbox_roots_removed"], 1)
            self.assertIn("lane NOT clean: disk_low", err.getvalue())
            self.assertFalse(json.loads((data / lane_reset.RECORD).read_text())["ok"])


@unittest.skipUnless(lane_reset.HAS_PROC, "processes are examined through /proc only")
class ProcessTests(unittest.TestCase):
    def test_only_processes_a_runner_started_are_stopped(self):
        with tempdir() as d:
            tmp = Path(d).resolve()
            sleeper = [sys.executable, "-c", "import time; time.sleep(120)"]
            agent = subprocess.Popen(sleeper, env={"PATH": os.environ["PATH"], "SWARM_ATTEMPT": "c000-mini-n1-s000"}, start_new_session=True)
            bystander = subprocess.Popen(sleeper, env={"PATH": os.environ["PATH"], "NOT_SWARM_ATTEMPT": "x"}, start_new_session=True)
            try:
                time.sleep(0.3)
                self.assertIn(agent.pid, lane_reset.agent_processes()[0])
                self.assertNotIn(os.getpid(), lane_reset.agent_processes()[0])
                record = lane_reset.reset(None, tmp=tmp)
                self.assertEqual((record["ok"], record["processes_killed"], record["after"]["agent_processes"]), (True, record["processes_found"], 0))
                self.assertGreaterEqual(record["processes_found"], 1)
                self.assertIsNotNone(agent.wait(5))
                self.assertIsNone(bystander.poll())  # same user, not started by a runner: untouched
            finally:
                for p in (agent, bystander):
                    if p.poll() is None:
                        p.send_signal(signal.SIGKILL)
                    p.wait()

    def test_agents_of_a_run_in_progress_are_not_stopped(self):
        """Wherever its sandboxes are: the agent itself says which runner started it."""
        from swarm.sandbox import runner_tag
        with tempdir() as d:
            tmp = Path(d).resolve()  # holds no root at all, like a by-hand reset pointed at the wrong temp dir
            agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True,
                                     env={"PATH": os.environ["PATH"], "SWARM_ATTEMPT": "c000-mini-n1-s000", "SWARM_RUNNER": runner_tag()})
            try:
                time.sleep(0.3)
                record = lane_reset.reset(None, tmp=tmp)
                self.assertEqual((record["ok"], record["problems"], record["processes_killed"], record["processes_in_live_runs"]),
                                 (False, ["runner_active"], 0, 1))
                self.assertIsNone(agent.poll())
                # The service itself, between runs, knows its own agents are leftovers.
                self.assertEqual(lane_reset.reset(None, tmp=tmp, own=True)["processes_killed"], 1)
                self.assertIsNotNone(agent.wait(5))
            finally:
                if agent.poll() is None:
                    agent.kill()
                agent.wait()


class ServiceTests(unittest.TestCase):
    def start(self, base: Path, **kw):
        make_task(base / "tasks", "one", OK_SCRIPT)
        presets = base / "presets.json"
        presets.write_text(json.dumps({"presets": [{"id": "demo", "title": "Demo", "blurb": "b", "tasks": ["tasks/one"], "sizes": [1]}]}))
        (base / "tmp").mkdir(exist_ok=True)
        svc = Service(presets, base / "data", harness="mini", reset_tmp=base / "tmp", reset_retry_s=3600, **kw)
        self.addCleanup(svc.close)
        return svc

    def wait(self, svc, rid):
        for _ in range(600):
            rec = svc.record(rid)
            with svc.lock:
                idle = svc.current is None
            if rec["state"] in ("done", "failed") and idle:
                return rec
            time.sleep(0.05)
        self.fail("run did not finish")

    def tmp(self) -> Path:
        t = tempdir()
        self.addCleanup(t.cleanup)
        return Path(t.name).resolve()

    def test_service_start_cleans_what_a_crash_left(self):
        base = self.tmp()
        (base / "tmp").mkdir()
        root = legacy_root(base / "tmp")
        svc = self.start(base)
        self.assertFalse(root.exists())
        self.assertEqual((svc.lane_state, svc.last_reset["sandbox_roots_removed"]), ("ready", 1))

    def test_every_run_is_followed_by_a_reset_recorded_with_the_run(self):
        base = self.tmp()
        svc = self.start(base)
        status, out = svc.launch("demo", 1, "1.2.3.4")
        self.assertEqual(status, 202)
        rec = self.wait(svc, out["id"])
        self.assertEqual(rec["result"]["passes"], 1)
        saved = json.loads((svc.run_dir(out["id"]) / "reset.json").read_text())
        self.assertEqual((saved["ok"], saved["run"]), (True, out["id"]))
        public = svc.snapshot(out["id"], 0)["run"]["reset"]
        self.assertEqual((public["ok"], public["problems"], public["after"]["sandbox_roots"]), (True, [], 0))
        self.assertNotIn(str(base), json.dumps(public))
        self.assertEqual(rec["warnings"], [])

    def test_a_dirty_lane_refuses_launches_until_a_reset_succeeds(self):
        base = self.tmp()
        svc = self.start(base, reset_min_free_mb=HUGE)
        self.assertEqual(svc.lane_state, "failed")
        status, out = svc.launch("demo", 1, "1.2.3.4")
        self.assertEqual((status, out["lane"], out["problems"]), (503, "reset_failed", ["disk_low"]))
        self.assertIn("not clean", out["error"])
        self.assertEqual(list((svc.data / "runs").iterdir()), [])
        # The operator fixes the machine and resets by hand: the service takes launches again without a restart.
        time.sleep(1.1)  # records are stamped to the second
        self.assertTrue(lane_reset.reset(svc.data, tmp=base / "tmp")["ok"])
        self.assertEqual(svc.launch("demo", 1, "1.2.3.4")[0], 202)

    def test_a_run_accepted_earlier_does_not_start_on_a_dirty_lane(self):
        base = self.tmp()
        svc = self.start(base)
        status, out = svc.launch("demo", 1, "1.2.3.4")
        self.wait(svc, out["id"])
        # A second run was queued; then the lane turned out dirty and stays so.
        rid = "0123456789ab"
        svc.run_dir(rid).mkdir()
        svc.save(rid, {"id": rid, "preset": "demo", "title": "Demo", "size": 1, "harness": "mini", "state": "queued", "created": "2026-11-12T00:00:00Z"})
        svc.reset_min_free_mb, svc.lane_state = HUGE, "failed"
        svc.last_reset = {"at": "9999", "t": 1e12, "ok": False, "problems": ["disk_low"]}
        svc._work(rid)
        rec = svc.record(rid)
        self.assertEqual((rec["state"], rec["refused"]), ("failed", "lane_not_clean"))
        self.assertFalse((svc.run_dir(rid) / "bundle").exists())  # nothing ran
        self.assertIn("not clean", svc.snapshot(rid, 0)["run"]["error"])


if __name__ == "__main__":
    unittest.main()
