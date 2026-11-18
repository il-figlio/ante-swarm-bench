"""Quiet gate: each check alone, unknowns, waiting, leftovers, record shape. No test sleeps for a quiet window."""
import json
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
from swarm import gate, host  # noqa: E402

TOP = ["stage", "at", "mode", "ok", "held_for", "reasons", "wait_s", "quiet_s", "values", "limits", "unknown"]
VALUES = ["swap_used_mb", "cpu_cores_other", "cpu_cores_self", "steal_cores", "psi_cpu_some_avg10", "pressure",
          "available_mb", "baseline_available_mb", "disk_free_mb", "agent_processes", "sandbox_roots"]
LIMS = ["max_swap_mb", "max_cpu_cores", "tolerance_mb", "min_free_mb", "wait_s"]
LIM = gate.limits(tolerance_mb=512, min_free_mb=1024)


class Fake:
    """Injected readings on a fake clock; sleeping advances the clock and the CPU counters."""

    def __init__(self, other=0.0, own=0.0, steal=0.0, **kw):
        self.t, self.other, self.own, self.steal = 0.0, other, own, steal
        self.busy_s = self.own_s = self.steal_s = 0.0
        self.mem = {"swap_used_mb": 0, "available_mb": 4000, "pressure": "normal", **kw.get("mem", {})}
        self.disk, self.procs, self.roots = kw.get("disk", 5000), kw.get("procs", 0), kw.get("roots", 0)
        self.cpu_none = kw.get("cpu_none", False)
        self.sleeps = 0

    def sleep(self, s):
        self.sleeps += 1
        self.t += s
        self.busy_s += (self.other + self.own) * s
        self.own_s += self.own * s
        self.steal_s += self.steal * s

    def readers(self):
        return {"memory_state": lambda: dict(self.mem), "clock": lambda: self.t, "sleep": self.sleep,
                "cpu_breakdown": lambda: None if self.cpu_none else {"busy": self.busy_s, "steal": self.steal_s, "total": self.t * 4},
                "own_cpu": lambda: self.own_s, "psi": lambda: 1.5, "disk_free": lambda p: self.disk,
                "agent_processes": lambda: self.procs, "runner_roots": lambda tmp: self.roots}


def run(fake, lim=LIM, baseline=4000, **kw):
    return gate.check(lim, baseline, "/t", "/d", fake.readers(), **kw)


class Checks(unittest.TestCase):
    def test_all_clear_passes(self):
        fake = Fake()
        rec = run(fake)
        self.assertTrue(rec["ok"], rec)
        self.assertEqual(rec["reasons"], [])
        self.assertEqual(fake.sleeps, 5)  # a five second window, on the fake clock

    def test_each_alone(self):
        cases = [("swap_in_use", Fake(mem={"swap_used_mb": 8})), ("cpu_busy", Fake(other=0.6)),
                 ("memory_below_baseline", Fake(mem={"available_mb": 3000})), ("disk_low", Fake(disk=100)),
                 ("agent_processes_left", Fake(procs=2)), ("sandbox_roots_left", Fake(roots=1)),
                 ("pressure_warn", Fake(mem={"pressure": "warn"})), ("pressure_critical", Fake(mem={"pressure": "critical"}))]
        for reason, fake in cases:
            with self.subTest(reason):
                rec = run(fake)
                self.assertEqual(rec["reasons"], [reason])
                self.assertFalse(rec["ok"])

    def test_own_cpu_is_not_other(self):
        rec = run(Fake(other=0.0, own=1.5))
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["values"]["cpu_cores_self"], 1.5)
        self.assertEqual(rec["values"]["cpu_cores_other"], 0.0)

    def test_steal_only_reports_cpu_steal(self):
        rec = run(Fake(steal=0.5))
        self.assertEqual(rec["reasons"], ["cpu_steal"])
        self.assertEqual(rec["values"]["steal_cores"], 0.5)
        self.assertEqual(run(Fake(other=0.5, steal=0.5))["reasons"], ["cpu_busy"])

    def test_unknown_holds_unless_allowed(self):
        fake = Fake(mem={"swap_used_mb": None, "available_mb": None, "pressure": "unknown"}, cpu_none=True)
        fake.procs = None
        rec = run(fake)
        for reason in ("swap_unknown", "cpu_unknown", "memory_unknown", "pressure_unknown", "agent_processes_unknown"):
            self.assertIn(reason, rec["reasons"])
        self.assertIsNone(rec["values"]["cpu_cores_other"])  # never 0
        self.assertIn("pressure", rec["unknown"])
        allowed = run(fake, gate.limits(512, 1024, allow_unknown=True))
        self.assertTrue(allowed["ok"], allowed)
        self.assertEqual(allowed["unknown"], rec["unknown"])

    def test_no_baseline_is_memory_unknown(self):
        self.assertEqual(run(Fake(), baseline=None)["reasons"], ["memory_unknown"])

    def test_record_keys_match_prd(self):
        rec = run(Fake(), stage="control", mode="enforce")
        self.assertEqual(list(rec), TOP)
        self.assertEqual(list(rec["values"]), VALUES)
        self.assertEqual(list(rec["limits"]), LIMS)
        self.assertEqual((rec["stage"], rec["mode"], rec["quiet_s"]), ("control", "enforce", 5))
        self.assertEqual(rec["limits"], {"max_swap_mb": 0, "max_cpu_cores": 0.10, "tolerance_mb": 512, "min_free_mb": 1024, "wait_s": 120})

    def test_public_has_no_paths(self):
        rec = run(Fake(roots=2, procs=1, other=0.7), stage="run")
        rec["note"] = "/tmp/swarm-abcd1234"
        text = json.dumps(gate.public(rec))
        self.assertNotIn("/", text)
        self.assertEqual(gate.public(rec)["values"]["sandbox_roots"], 2)
        self.assertIsInstance(gate.public(rec)["values"]["agent_processes"], int)

    def test_words_and_sentence(self):
        self.assertEqual(gate.words("cpu_busy"), "other CPU busy")
        text = gate.sentence(run(Fake(other=0.6, mem={"swap_used_mb": 120})))
        self.assertIn("other CPU busy (0.6 cores)", text)
        self.assertIn("swap in use (120 MB)", text)
        for reason in ("pressure_warn", "agent_processes_left", "cpu_unknown"):
            self.assertNotIn("_", gate.words(reason))


class Wait(unittest.TestCase):
    def test_stops_at_wait_s_and_accumulates(self):
        fake = Fake(other=0.6)
        lim = gate.limits(512, 1024, wait_s=20)
        rec = gate.wait(lim, 4000, "/t", "/d", fake.readers())
        self.assertFalse(rec["ok"])
        self.assertEqual(rec["held_for"], ["cpu_busy"])
        self.assertGreaterEqual(rec["wait_s"], 20)
        self.assertLess(rec["wait_s"], 40)
        self.assertEqual(rec["limits"]["wait_s"], 20)

    def test_held_for_keeps_earlier_reasons_after_pass(self):
        fake = Fake(disk=100)
        base = fake.sleep

        def sleep(s):
            base(s)
            if fake.t >= 7:
                fake.disk = 5000
        readers = dict(fake.readers(), sleep=sleep)
        rec = gate.wait(LIM, 4000, "/t", "/d", readers)
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["held_for"], ["disk_low"])
        self.assertEqual(rec["reasons"], [])

    def test_on_leftovers_once_then_pass(self):
        fake, calls = Fake(procs=3, roots=1), []

        def clean():
            calls.append(1)
            fake.procs = fake.roots = 0
        rec = gate.wait(LIM, 4000, "/t", "/d", fake.readers(), on_leftovers=clean, stage="run")
        self.assertEqual(len(calls), 1)
        self.assertTrue(rec["ok"])
        self.assertEqual(rec["held_for"], ["agent_processes_left", "sandbox_roots_left"])

    def test_on_leftovers_not_repeated_when_it_does_not_help(self):
        fake, calls = Fake(procs=3), []
        rec = gate.wait(gate.limits(512, 1024, wait_s=10), 4000, "/t", "/d", fake.readers(), on_leftovers=lambda: calls.append(1))
        self.assertEqual(len(calls), 1)
        self.assertFalse(rec["ok"])

    def test_on_leftovers_not_called_for_other_reasons(self):
        calls = []
        gate.wait(gate.limits(512, 1024, wait_s=6), 4000, "/t", "/d", Fake(other=1).readers(), on_leftovers=lambda: calls.append(1))
        self.assertEqual(calls, [])


@unittest.skipUnless(sys.platform.startswith("linux"), "needs /proc")
class RealProc(unittest.TestCase):
    def test_cpu_breakdown_and_psi(self):
        a = host.cpu_breakdown()
        self.assertEqual(set(a), {"busy", "steal", "total"})
        b = host.cpu_breakdown()
        self.assertGreaterEqual(b["total"], a["total"])
        self.assertGreaterEqual(b["busy"], a["busy"])
        psi = host.psi_cpu_some_avg10()
        self.assertTrue(psi is None or psi >= 0)  # PSI can be off in a kernel; then it is unknown, not 0


if __name__ == "__main__":
    unittest.main()
