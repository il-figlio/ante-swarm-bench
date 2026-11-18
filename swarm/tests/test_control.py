"""Lane control and quiet gate inside a lane run (serve._work): gate, control, reset, gate, run, reset."""
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
from swarm import reset as lane_reset  # noqa: E402
from swarm.bundle import verify  # noqa: E402
from swarm.serve import Service, valid_series  # noqa: E402

BAD_SCRIPT = [{"op": "write", "path": "out.txt", "content": "no"}, {"op": "final", "text": "done"}]


class Quiet:
    """Injected gate readers: a lane whose other CPU is `cores` and whose leftovers can be switched on."""

    def __init__(self, cores=0.0, leftovers=0):
        self.t, self.busy, self.cores, self.leftovers = 0.0, 0.0, cores, leftovers

    def sleep(self, s):
        self.t += s
        self.busy += self.cores * s

    def readers(self):
        return {"memory_state": lambda: {"swap_used_mb": 0, "available_mb": 8000, "pressure": "normal"},
                "cpu_breakdown": lambda: {"busy": self.busy, "steal": 0.0, "total": self.t * 2},
                "own_cpu": lambda: 0.0, "psi": lambda: 0.0, "disk_free": lambda path: 100_000,
                "agent_processes": lambda: self.leftovers, "runner_roots": lambda tmp: 0,
                "clock": lambda: self.t, "sleep": self.sleep}


class ControlTests(unittest.TestCase):
    def tmp(self) -> Path:
        t = tempdir()
        self.addCleanup(t.cleanup)
        return Path(t.name).resolve()

    def start(self, base: Path, control_script=OK_SCRIPT, with_control=True, **kw):
        make_task(base / "tasks", "one", OK_SCRIPT)
        make_task(base / "tasks", "loop", control_script)
        presets = [{"id": "demo", "title": "Demo", "blurb": "b", "tasks": ["tasks/one"], "sizes": [1, 2], "kind": "harness"},
                   {"id": "lane-baseline", "title": "Base", "blurb": "b", "tasks": ["tasks/one"], "sizes": [1],
                    "kind": "baseline", "harnesses": ["mini"]}]
        if with_control:
            presets.append({"id": "lane-control", "title": "Control", "blurb": "b", "tasks": ["tasks/loop"], "sizes": [1],
                            "kind": "control", "repeats": 3, "max_requests": 20, "timeout_s": 60, "harnesses": ["mini"]})
        (base / "presets.json").write_text(json.dumps({"presets": presets}))
        (base / "tmp").mkdir(exist_ok=True)
        kw.setdefault("control", True)
        svc = Service(base / "presets.json", base / "data", harness="mini", per_client_hour=100, max_queue=10,
                      reset_tmp=base / "tmp", reset_retry_s=3600, **kw)
        self.addCleanup(svc.close)
        return svc

    def wait(self, svc, rid):
        for _ in range(1200):
            rec = svc.record(rid)
            with svc.lock:
                idle = svc.current is None and not svc.order
            if rec["state"] in ("done", "failed") and idle:
                return rec
            time.sleep(0.05)
        self.fail("run did not finish")

    def test_control_runs_inside_the_run_and_is_recorded_with_it(self):
        svc = self.start(self.tmp())
        self.assertNotIn("lane-control", [p["id"] for p in svc.public_presets()])
        code, out = svc.launch("demo", 1, "c")
        self.assertEqual(code, 202, out)
        rec = self.wait(svc, out["id"])
        d = svc.run_dir(out["id"])
        self.assertEqual(rec["state"], "done", rec)
        self.assertEqual(verify(d / "control" / "bundle"), [])
        self.assertEqual(verify(d / "bundle"), [])
        c = rec["control"]
        self.assertEqual((c["state"], c["order"], c["attempts"], c["passes"], c["harness"]), ("done", "control_first", 3, 3, "mini"))
        for k in ("first_request_s", "step_gap_s", "cpu_s", "wall_s", "rss_mb", "setup_s", "request_bytes_first"):
            self.assertEqual(len(c["values"][k]), 3, k)
            self.assertIn(k, c["median"])
        self.assertIsNotNone(c["median"]["first_request_s"])
        self.assertRegex(c["key"], r"^[0-9a-f]{12}$")
        self.assertTrue((d / "reset-control.json").exists() and (d / "reset.json").exists())
        self.assertEqual(rec["settings"], {"gate": "off", "gate_limits": None, "control": True, "reset": True})
        self.assertNotIn("control_failed", rec["warnings"])
        ledger = json.loads(svc.ledger_path.read_text().splitlines()[-1])
        self.assertEqual((ledger["control_key"], ledger["state"]), (c["key"], "done"))
        # The same control on the same machine has the same key.
        code, out2 = svc.launch("demo", 1, "c", control_order="control_last")
        rec2 = self.wait(svc, out2["id"])
        self.assertEqual((rec2["control"]["key"], rec2["control"]["order"], rec2["state"]), (c["key"], "control_last", "done"))
        self.assertTrue((svc.run_dir(out2["id"]) / "reset-run.json").exists())

    def test_nothing_runs_between_a_runs_control_and_its_harness_run(self):
        svc = self.start(self.tmp())
        seen = []
        control, execute = svc._control, svc._execute
        svc._control = lambda rid: (seen.append((rid, "control")), control(rid))[1]
        svc._execute = lambda rid: (seen.append((rid, "run")), execute(rid))[1]
        a = svc.launch("demo", 1, "c")[1]["id"]
        b = svc.launch("demo", 1, "c")[1]["id"]
        self.wait(svc, a)
        self.wait(svc, b)
        self.assertEqual(seen, [(a, "control"), (a, "run"), (b, "control"), (b, "run")])

    def test_control_off_missing_or_for_control_presets(self):
        svc = self.start(self.tmp(), control=False)
        rec = self.wait(svc, svc.launch("demo", 1, "c")[1]["id"])
        self.assertEqual((rec["state"], rec["control"]["state"]), ("done", "off"))
        self.assertFalse((svc.run_dir(rec["id"]) / "control").exists())
        svc2 = self.start(self.tmp(), with_control=False)
        rec = self.wait(svc2, svc2.launch("demo", 1, "c")[1]["id"])
        self.assertEqual((rec["control"]["state"], rec["control"]["reason"]), ("off", "not_configured"))
        svc3 = self.start(self.tmp())
        for preset in ("lane-baseline", "lane-control"):
            rec = self.wait(svc3, svc3.launch(preset, 1, "c", harness="mini")[1]["id"])
            self.assertEqual((rec["state"], rec["control"]["state"]), ("done", "off"), preset)

    def test_a_failed_control_is_recorded_and_the_run_still_executes(self):
        svc = self.start(self.tmp(), control_script=BAD_SCRIPT)
        rec = self.wait(svc, svc.launch("demo", 1, "c")[1]["id"])
        self.assertEqual((rec["state"], rec["control"]["state"], rec["control"]["reason"]), ("done", "failed", "attempts_failed"))
        self.assertEqual(rec["result"]["passes"], rec["result"]["attempts"])
        self.assertIn("control_failed", rec["warnings"])

    def test_a_control_that_breaks_or_a_run_that_breaks_keeps_the_record_honest(self):
        svc = self.start(self.tmp())
        first = self.wait(svc, svc.launch("demo", 1, "c")[1]["id"])
        # The control breaks: recorded as failed, and the run keeps the lane's cohort (a broken control is not a new cohort).
        svc._control_record = lambda *a: (_ for _ in ()).throw(RuntimeError("boom"))
        broken = self.wait(svc, svc.launch("demo", 1, "c")[1]["id"])
        self.assertEqual((broken["state"], broken["control"]["state"], broken["control"]["reason"]), ("done", "failed", "control_error: RuntimeError"))
        self.assertIsNone(broken["control"].get("key"))
        self.assertEqual(broken["cohort"], first["cohort"])
        # The run breaks before a control_last control: the control is not left "pending".
        svc._execute = lambda rid: (_ for _ in ()).throw(RuntimeError("run broke"))
        failed = self.wait(svc, svc.launch("demo", 1, "c", control_order="control_last")[1]["id"])
        self.assertEqual((failed["state"], failed["control"]["state"], failed["control"]["reason"]), ("failed", "skipped", "run_error"))

    def test_series_and_control_order_are_validated(self):
        svc = self.start(self.tmp(), control=False)
        self.assertEqual(valid_series({"race": "0123456789ab", "rep": 2, "of": 6}), {"race": "0123456789ab", "rep": 2, "of": 6})
        for bad in ({"race": "x", "rep": 1, "of": 1}, {"race": "0123456789ab", "rep": 0, "of": 6},
                    {"race": "0123456789ab", "rep": 7, "of": 6}, {"race": "0123456789ab", "rep": 1, "of": 65},
                    {"race": "0123456789ab", "rep": True, "of": 6}, {"race": "0123456789ab", "rep": 1, "of": 6, "x": 1}, "s"):
            self.assertEqual(svc.launch("demo", 1, "c", series=bad)[0], 400, bad)
        self.assertEqual(svc.launch("demo", 1, "c", control_order="sideways")[0], 400)
        code, out = svc.launch("demo", 1, "c", series={"race": "0123456789ab", "rep": 1, "of": 6})
        rec = self.wait(svc, out["id"])
        self.assertEqual(rec["series"], {"race": "0123456789ab", "rep": 1, "of": 6})
        self.assertEqual(json.loads(svc.ledger_path.read_text().splitlines()[-1])["series"]["rep"], 1)


class GateTests(unittest.TestCase):
    start, wait, tmp = ControlTests.start, ControlTests.wait, ControlTests.tmp

    def gated(self, mode, lane: Quiet, **kw):
        base = self.tmp()
        svc = self.start(base, gate=mode, gate_readers=lane.readers(), reset=False,
                         gate_limits={"quiet_s": 2, "wait_s": 3}, **kw)
        svc.baseline_available_mb = 8000  # what reset would have measured on a Linux lane
        return svc, base

    def test_enforce_refuses_a_busy_lane_and_the_next_run_is_unaffected(self):
        lane = Quiet(cores=0.6)
        svc, base = self.gated("enforce", lane)
        rec = self.wait(svc, svc.launch("demo", 1, "c")[1]["id"])
        d = svc.run_dir(rec["id"])
        self.assertEqual((rec["state"], rec["refused"]), ("failed", "not_quiet"))
        self.assertFalse((d / "bundle").exists() or (d / "control" / "bundle").exists())
        self.assertIn("cpu_busy", rec["gate"][0]["held_for"])
        self.assertEqual(rec["gate"][0]["values"]["cpu_cores_other"], 0.6)
        public = svc.snapshot(rec["id"], 0)["run"]
        self.assertEqual(public["error"], "This lane was not quiet enough to measure: other CPU busy (0.6 cores).")
        ledger = json.loads(svc.ledger_path.read_text().splitlines()[-1])
        self.assertEqual((ledger["refused"], ledger["gate_ok"]), ("not_quiet", False))
        lane.cores = 0.0
        rec = self.wait(svc, svc.launch("demo", 1, "c")[1]["id"])
        self.assertEqual(rec["state"], "done")
        self.assertEqual([g["stage"] for g in rec["gate"]], ["control", "run"])
        self.assertTrue(all(g["ok"] for g in rec["gate"]))
        gates = (svc.data / "gates.jsonl").read_text().splitlines()
        self.assertEqual(len(gates), 3)
        for text in (json.dumps(rec["gate"]), "\n".join(gates), (svc.run_dir(rec["id"]) / "gate.json").read_text()):
            self.assertNotIn(str(base), text)
        g = rec["gate"][1]
        for k in ("swap_used_mb", "cpu_cores_other", "cpu_cores_self", "steal_cores", "psi_cpu_some_avg10", "pressure",
                  "available_mb", "baseline_available_mb", "disk_free_mb", "agent_processes", "sandbox_roots"):
            self.assertIn(k, g["values"])
        self.assertEqual(set(g["limits"]), {"max_swap_mb", "max_cpu_cores", "tolerance_mb", "min_free_mb", "wait_s"})
        self.assertEqual(rec["settings"]["gate"], "enforce")
        self.assertEqual(rec["settings"]["gate_limits"]["quiet_s"], 2)

    def test_observe_records_the_same_values_and_runs(self):
        svc, _ = self.gated("observe", Quiet(cores=0.6), control=False)
        rec = self.wait(svc, svc.launch("demo", 1, "c")[1]["id"])
        self.assertEqual((rec["state"], rec["gate"][0]["ok"], rec["gate"][0]["mode"]), ("done", False, "observe"))
        self.assertEqual(rec["result"]["passes"], rec["result"]["attempts"])
        status = svc.last_gate
        self.assertFalse(status["ok"])

    def test_leftovers_trigger_one_reset_then_the_gate_passes(self):
        lane = Quiet(leftovers=2)
        svc, _ = self.gated("enforce", lane, control=False)
        calls = []

        def reset_lane(rid, name="reset.json"):
            calls.append(rid)
            lane.leftovers = 0
            return {"ok": True, "problems": []}
        svc.reset_lane = reset_lane
        rec = self.wait(svc, svc.launch("demo", 1, "c")[1]["id"])
        self.assertEqual((rec["state"], calls), ("done", [None]))
        self.assertIn("agent_processes_left", rec["gate"][0]["held_for"])
        self.assertTrue(rec["gate"][0]["ok"])

    @unittest.skipUnless(lane_reset.HAS_PROC, "processes are examined through /proc only")
    def test_real_leftover_process_is_reset_once_and_the_gate_passes(self):
        base = self.tmp()
        lane = Quiet()
        readers = {k: v for k, v in lane.readers().items() if k not in ("agent_processes", "runner_roots")}
        svc = self.start(base, gate="enforce", gate_readers=readers, control=False, gate_limits={"quiet_s": 1, "wait_s": 5})
        svc.baseline_available_mb = 8000
        resets = []
        real = svc.reset_lane
        svc.reset_lane = lambda rid, name="reset.json": (resets.append(rid), real(rid, name))[1]
        agent = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"], start_new_session=True,
                                 env={"PATH": os.environ["PATH"], "SWARM_ATTEMPT": "c000-mini-n1-s000"})
        try:
            time.sleep(0.3)
            rec = self.wait(svc, svc.launch("demo", 1, "c")[1]["id"])
            self.assertEqual(rec["state"], "done")
            self.assertEqual(resets.count(None), 1)
            self.assertTrue(rec["gate"][0]["ok"])
            self.assertIsNotNone(agent.wait(5))
        finally:
            if agent.poll() is None:
                agent.send_signal(signal.SIGKILL)
            agent.wait()


if __name__ == "__main__":
    unittest.main()
