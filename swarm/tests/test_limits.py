"""cgroup limits: parsing and formatting everywhere, a fake cgroup tree for the group's life cycle, plan and run
behaviour when limits cannot be applied, and a real cgroup only on a Linux host that delegates one."""
import json
import os
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import OK_SCRIPT, make_spec, make_task, tempdir  # noqa: E402
from swarm import limits  # noqa: E402
from swarm.plan import freeze, load_spec  # noqa: E402
from swarm.runner import run  # noqa: E402

FULL = limits.Limits(cpu=1.5, memory_mb=2048, pids=4096)


def fake_own(base: Path, controllers="cpu memory pids io", enabled="") -> Path:
    """A directory shaped like a delegated cgroup: plain files stand in for the control files."""
    own = base / "own"
    own.mkdir()
    (own / "cgroup.controllers").write_text(controllers + "\n")
    (own / "cgroup.procs").write_text("")
    (own / "cgroup.subtree_control").write_text(enabled)
    return own


def clear_like_cgroupfs(path: Path):
    """cgroupfs lets a group's directory go while its control files are still there; a plain tree does not."""
    for f in path.iterdir():
        f.unlink()


class FormatTests(unittest.TestCase):
    def test_cpu_max_is_quota_over_100ms_period(self):
        self.assertEqual(limits.cpu_max(1.0), "100000 100000")
        self.assertEqual(limits.cpu_max(0.5), "50000 100000")
        self.assertEqual(limits.cpu_max(2), "200000 100000")
        self.assertEqual(limits.cpu_max(1.5), "150000 100000")

    def test_limit_files_hold_the_exact_text_written(self):
        self.assertEqual(FULL.files(), {"cpu.max": "150000 100000", "memory.max": str(2048 * 1024 * 1024), "pids.max": "4096"})
        self.assertEqual(limits.Limits(memory_mb=512).files(), {"memory.max": str(512 * 1024 * 1024)})
        self.assertEqual(limits.Limits().files(), {})

    def test_requested_and_needed_controllers(self):
        self.assertEqual(FULL.requested(), {"cpu": 1.5, "memory_mb": 2048, "pids": 4096})
        self.assertEqual(limits.Limits(pids=8).requested(), {"pids": 8})
        self.assertEqual(FULL.needed(), ["cpu", "memory", "pids"])
        self.assertEqual(limits.Limits.from_spec({"cpu": 1.0, "required": True}), limits.Limits(cpu=1.0))
        self.assertEqual(limits.Limits.from_spec(None).requested(), {})

    def test_flat_keyed_files_parse(self):
        self.assertEqual(limits.parse_flat("usage_usec 1234\nuser_usec 1000\nsystem_usec 234\n"),
                         {"usage_usec": 1234, "user_usec": 1000, "system_usec": 234})
        events = limits.parse_flat("low 0\nhigh 0\nmax 7\noom 2\noom_kill 1\noom_group_kill 0\n")
        self.assertEqual((events["max"], events["oom"], events["oom_kill"]), (7, 2, 1))
        self.assertEqual(limits.parse_flat("garbage\nkey notanumber\n"), {})


class FakeTreeTests(unittest.TestCase):
    def tearDown(self):
        limits.release()

    def test_probe_names_what_is_missing(self):
        with tempdir() as d:
            own = fake_own(Path(d), controllers="cpu io")
            reason = limits.probe(FULL, own)
            self.assertIn("memory", reason)
            self.assertIn("Delegate=yes", reason)
            self.assertIsNone(limits.probe(limits.Limits(cpu=1.0), own))
            self.assertIn("cgroup.controllers", limits.probe(FULL, Path(d) / "nowhere"))
            self.assertIsNone(limits.probe(limits.Limits(), own))  # nothing requested, nothing to apply

    def test_prepare_moves_the_process_enables_controllers_and_is_idempotent(self):
        with tempdir() as d:
            own = fake_own(Path(d))
            self.assertEqual(limits.prepare(FULL, own, pid=4242), own)
            self.assertEqual((own / "runner" / "cgroup.procs").read_text(), "4242")
            self.assertEqual((own / "cgroup.subtree_control").read_text(), "+cpu +memory +pids")
            (own / "runner" / "cgroup.procs").write_text("")
            (own / "cgroup.subtree_control").write_text("")
            self.assertEqual(limits.prepare(FULL, own, pid=4242), own)  # second call changes nothing
            self.assertEqual((own / "runner" / "cgroup.procs").read_text(), "")
            self.assertEqual((own / "cgroup.subtree_control").read_text(), "")

    def test_prepare_enables_only_what_is_missing(self):
        with tempdir() as d:
            own = fake_own(Path(d), enabled="memory")
            limits.prepare(FULL, own, pid=1)
            self.assertEqual((own / "cgroup.subtree_control").read_text(), "+cpu +pids")

    def test_release_moves_back_and_disables_what_it_enabled(self):
        with tempdir() as d:
            own = fake_own(Path(d), enabled="memory")
            limits.prepare(FULL, own, pid=77)
            self.assertEqual(limits.release(), {})
            self.assertEqual((own / "cgroup.procs").read_text(), "77")
            self.assertEqual(limits.release(), {})  # nothing prepared any more

    def test_prepare_failure_is_a_reason_not_a_crash(self):
        with tempdir() as d:
            own = fake_own(Path(d), controllers="cpu")
            with self.assertRaises(limits.LimitsUnavailable) as caught:
                limits.prepare(FULL, own)
            self.assertIn("memory", str(caught.exception))
            self.assertIsNone(limits.CellGroup.create("x", FULL, own))
            self.assertIn("memory", limits.CellGroup.last_reason)

    def test_cell_group_life_cycle(self):
        with tempdir() as d:
            own = fake_own(Path(d))
            group = limits.CellGroup.create("c000-1", FULL, own, pid=9)
            self.assertIsNotNone(group)
            for name, text in (("cpu.max", "150000 100000"), ("memory.max", str(2048 * 1024 * 1024)), ("pids.max", "4096")):
                self.assertEqual((group.path / name).read_text(), text)
            group.add(31337)
            self.assertEqual((group.path / "cgroup.procs").read_text(), "31337")
            (group.path / "cpu.stat").write_text("usage_usec 2500000\nuser_usec 2000000\nsystem_usec 500000\n")
            (group.path / "memory.peak").write_text("268435456\n")
            (group.path / "memory.current").write_text("134217728\n")
            (group.path / "memory.events").write_text("low 0\nhigh 0\nmax 12\noom 3\noom_kill 2\n")
            (group.path / "pids.events").write_text("max 5\n")
            self.assertEqual(group.stats(), {"cpu_usec": 2500000, "memory_peak_kb": 262144, "memory_peak_source": "memory.peak",
                                             "memory_current_kb": 131072, "oom_kill": 2, "oom": 3, "max_events": 12,
                                             "pids_max_events": 5})
            (group.path / "cgroup.procs").write_text("")
            clear_like_cgroupfs(group.path)
            self.assertTrue(group.close())
            self.assertFalse(group.path.exists())
            self.assertTrue(group.close())  # closing twice is fine

    def test_without_memory_peak_the_sampled_maximum_is_used_and_named(self):
        with tempdir() as d:
            own = fake_own(Path(d))
            group = limits.CellGroup.create("c001-1", limits.Limits(memory_mb=64), own, pid=9)
            (group.path / "memory.current").write_text("2097152\n")  # 2048 KiB
            group._peak_sampled_kb = 8192
            stats = group.stats()
            self.assertEqual((stats["memory_peak_kb"], stats["memory_peak_source"], stats["memory_current_kb"]), (8192, "sampled memory.current", 2048))
            self.assertIsNone(stats["cpu_usec"])  # unreadable stays null, never 0
            self.assertIsNone(stats["oom_kill"])
            clear_like_cgroupfs(group.path)
            group.close()

    def test_a_leftover_group_with_processes_is_refused(self):
        with tempdir() as d:
            own = fake_own(Path(d))
            (own / "c002-1").mkdir()
            (own / "c002-1" / "cgroup.procs").write_text("555\n")
            self.assertIsNone(limits.CellGroup.create("c002-1", FULL, own, pid=9))
            self.assertIn("holds processes", limits.CellGroup.last_reason)


class PlanTests(unittest.TestCase):
    def spec(self, base, limits_text):
        return make_spec(base, extra=limits_text)

    def test_limits_are_validated_and_required_defaults_to_true(self):
        with tempdir() as d:
            base = Path(d)
            spec = load_spec(make_spec(base, extra="[limits]\ncpu = 1.0\nmemory_mb = 2048\npids = 4096\n"))
            self.assertEqual(spec["limits"], {"cpu": 1.0, "memory_mb": 2048, "pids": 4096, "required": True})
            spec = load_spec(make_spec(base, extra="[limits]\ncpu = 0.5\nrequired = false\n"))
            self.assertFalse(spec["limits"]["required"])
            self.assertNotIn("limits", load_spec(make_spec(base)))
            for bad in ("cpu = 0", "cpu = -1", "memory_mb = 4", "memory_mb = 1.5", "pids = 0", "cpu = 1\nrequired = \"yes\"",
                        "required = true", "cpu = 1\nwat = 2", "cpu = true"):
                with self.assertRaises(ValueError, msg=bad):
                    load_spec(make_spec(base, extra=f"[limits]\n{bad}\n"))

    def test_limits_are_frozen_into_the_plan_hash(self):
        with tempdir() as d:
            base = Path(d)
            make_task(base / "tasks", "one", OK_SCRIPT)
            plan = freeze(make_spec(base, extra="[limits]\ncpu = 1.0\n"))
            self.assertEqual(plan["spec"]["limits"]["cpu"], 1.0)
            plain = freeze(make_spec(base))
            self.assertNotEqual(plan["plan_sha256"], plain["plan_sha256"])


class RunTests(unittest.TestCase):
    """Where limits cannot be applied (this test forces it), the run refuses unless the plan says required = false."""

    def plan_path(self, base, limits_text):
        make_task(base / "tasks", "one", OK_SCRIPT)
        path = base / "plan.json"
        path.write_text(json.dumps(freeze(make_spec(base, levels=(1,), extra=limits_text))))
        return path

    def test_required_limits_that_cannot_apply_refuse_the_run_before_it_starts(self):
        with tempdir() as d:
            base = Path(d)
            path = self.plan_path(base, "[limits]\ncpu = 1.0\nmemory_mb = 512\n")
            with mock.patch.object(limits, "probe", return_value="no cgroup v2 hierarchy for this process"):
                with self.assertRaises(RuntimeError) as caught:
                    run(path, base / "bundle")
            self.assertIn("no cgroup v2", str(caught.exception))
            self.assertIn("limits.required = false", str(caught.exception))
            self.assertFalse((base / "bundle").exists())

    def test_optional_limits_run_unlimited_and_say_so_on_the_run_and_every_cell(self):
        with tempdir() as d:
            base = Path(d)
            path = self.plan_path(base, "[limits]\ncpu = 1.0\nmemory_mb = 512\nrequired = false\n")
            with mock.patch.object(limits, "probe", return_value="no cgroup v2 hierarchy for this process"):
                bundle = run(path, base / "bundle")
            summary = json.loads((bundle / "summary.json").read_text())
            self.assertTrue(summary["complete"])
            self.assertEqual(summary["limits"]["applied"], False)
            self.assertEqual(summary["limits"]["requested"], {"cpu": 1.0, "memory_mb": 512})
            cell = json.loads((bundle / "cells.jsonl").read_text().splitlines()[0])
            self.assertEqual(cell["limits"], {"requested": {"cpu": 1.0, "memory_mb": 512}, "applied": False, "required": False,
                                              "reason": "no cgroup v2 hierarchy for this process"})
            host = json.loads((bundle / "host.json").read_text())
            self.assertEqual(host["limits"]["reason"], "no cgroup v2 hierarchy for this process")

    def test_a_run_without_limits_records_none(self):
        with tempdir() as d:
            base = Path(d)
            path = self.plan_path(base, "")
            bundle = run(path, base / "bundle")
            self.assertNotIn("limits", json.loads((bundle / "summary.json").read_text()))
            self.assertNotIn("limits", json.loads((bundle / "cells.jsonl").read_text().splitlines()[0]))


class JoinBeforeExecTests(unittest.TestCase):
    """The agent's first process puts itself in the cell's group and then execs the harness: nothing it starts can
    be outside the group. The no-group path runs argv untouched."""

    def test_wrapper_argv_shape(self):
        argv = limits.wrap_argv(Path("/sys/fs/cgroup/x/c000-1/cgroup.procs"), ["/opt/bin/ante", "-p", "a b"])
        self.assertEqual(argv[:3], ["/bin/sh", "-c", limits.JOIN_SCRIPT])
        self.assertEqual(argv[3:], ["swarm-join", "/sys/fs/cgroup/x/c000-1/cgroup.procs", "/opt/bin/ante", "-p", "a b"])
        self.assertIn('exec "$@"', limits.JOIN_SCRIPT)
        group = limits.CellGroup(Path("/g/c001"), FULL)
        self.assertEqual(group.wrap(["x"]), limits.wrap_argv(Path("/g/c001/cgroup.procs"), ["x"]))

    def test_the_process_writes_its_own_pid_then_execs_with_the_same_pid(self):
        with tempdir() as d:
            procs = Path(d) / "cgroup.procs"
            done = subprocess.run(limits.wrap_argv(procs, ["/bin/sh", "-c", 'echo "$$"; echo "$1"', "inner", "arg with space"]),
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            pid, arg = done.stdout.split("\n")[:2]
            self.assertEqual(procs.read_text().strip(), pid)   # the pid that ran the command joined before it ran
            self.assertEqual(arg, "arg with space")

    def test_a_failed_join_never_runs_the_command(self):
        with tempdir() as d:
            procs = Path(d) / "missing" / "cgroup.procs"
            marker = Path(d) / "ran"
            log = Path(d) / "agent.log"
            with log.open("wb") as out:
                done = subprocess.run(limits.wrap_argv(procs, ["/usr/bin/touch", str(marker)]), stdout=out,
                                      stderr=subprocess.STDOUT, timeout=30)
            self.assertEqual(done.returncode, limits.JOIN_FAILED_EXIT)
            self.assertFalse(marker.exists())
            self.assertTrue(limits.join_failed(done.returncode, log))
            self.assertFalse(limits.join_failed(0, log))
            log.write_text("exit 125 from the agent itself\n")
            self.assertFalse(limits.join_failed(limits.JOIN_FAILED_EXIT, log))

    def run_capturing(self, base, limits_text, group_dir=None):
        """Run a one-attempt plan; returns (bundle, the argv each execute() got)."""
        from swarm import runner as runner_module
        seen = []
        real = runner_module.execute

        def spy(argv, *a, **k):
            seen.append(list(argv))
            return real(argv, *a, **k)

        make_task(base / "tasks", "one", OK_SCRIPT)
        path = base / "plan.json"
        path.write_text(json.dumps(freeze(make_spec(base, levels=(1,), extra=limits_text))))
        patches = [mock.patch.object(runner_module, "execute", spy)]
        if group_dir is not None:
            group_dir.mkdir(exist_ok=True)
            patches += [mock.patch.object(limits, "probe", return_value=None),
                        mock.patch.object(limits, "prepare", return_value=group_dir),
                        mock.patch.object(limits, "release", return_value={}),
                        mock.patch.object(limits.CellGroup, "close", lambda self, timeout_s=3.0: True)]
        for patcher in patches:
            patcher.start()
        try:
            bundle = run(path, base / "bundle")
        finally:
            for patcher in reversed(patches):
                patcher.stop()
        return bundle, seen

    def test_without_a_group_the_argv_is_unchanged(self):
        with tempdir() as d:
            base = Path(d)
            bundle, seen = self.run_capturing(base, "")
            self.assertEqual(len(seen), 1)
            self.assertNotIn(limits.JOIN_SCRIPT, seen[0])
            optional = base / "optional"
            optional.mkdir()
            with mock.patch.object(limits, "probe", return_value="no cgroup v2 hierarchy for this process"):
                bundle, seen = self.run_capturing(optional, "[limits]\ncpu = 1.0\nrequired = false\n")
            self.assertEqual(len(seen), 1)
            self.assertNotIn(limits.JOIN_SCRIPT, seen[0])
            attempt = json.loads((bundle / "attempts.jsonl").read_text().splitlines()[0])
            self.assertTrue(attempt["passed"], attempt.get("infra_error"))

    def test_with_a_group_the_agent_joins_it_before_the_harness_starts(self):
        with tempdir() as d:
            base = Path(d)
            bundle, seen = self.run_capturing(base, "[limits]\ncpu = 1.0\n", group_dir=base / "own")
            agent_argv = seen[0]
            self.assertEqual(agent_argv[agent_argv.index(limits.JOIN_SCRIPT) - 2:agent_argv.index(limits.JOIN_SCRIPT)],
                             ["/bin/sh", "-c"])
            procs = Path(agent_argv[agent_argv.index(limits.JOIN_SCRIPT) + 2])
            self.assertEqual(procs.name, "cgroup.procs")
            self.assertTrue(procs.read_text().strip().isdigit())   # the agent's own pid, written by the agent
            attempt = json.loads((bundle / "attempts.jsonl").read_text().splitlines()[0])
            self.assertTrue(attempt["passed"], attempt.get("infra_error"))

    def test_an_agent_that_cannot_join_fails_as_infra_and_never_runs(self):
        with tempdir() as d:
            base = Path(d)
            own = base / "own"
            original = limits.CellGroup.try_create.__func__

            def try_create(cls, name, wanted, own_=None, pid=None):
                group = original(cls, name, wanted, own_, pid)
                (group.path / "cgroup.procs").mkdir()   # writing a pid into it fails
                return group

            with mock.patch.object(limits.CellGroup, "try_create", classmethod(try_create)):
                bundle, seen = self.run_capturing(base, "[limits]\ncpu = 1.0\n", group_dir=own)
            attempt = json.loads((bundle / "attempts.jsonl").read_text().splitlines()[0])
            self.assertEqual((attempt["failure"], attempt["infra_reason"]), ("infra", "limits_unavailable"))
            self.assertIn("could not join", attempt["infra_error"])
            self.assertEqual(attempt["requests"]["count"], 0)   # the harness never started

def real_cgroup_reason():
    if not sys.platform.startswith("linux"):
        return "not Linux"
    reason = limits.probe(limits.Limits(cpu=1.0, memory_mb=256, pids=64))
    return reason


@unittest.skipIf(real_cgroup_reason(), f"no writable delegated cgroup v2 here: {real_cgroup_reason()}")
class RealCgroupTests(unittest.TestCase):
    def tearDown(self):
        limits.release()

    def test_a_child_is_limited_counted_and_the_group_removed(self):
        wanted = limits.Limits(cpu=0.5, memory_mb=256, pids=64)
        group = limits.CellGroup.create(f"swarm-test-{os.getpid()}", wanted)
        self.assertIsNotNone(group, limits.CellGroup.last_reason)
        proc = subprocess.Popen([sys.executable, "-c", "import time; t=time.time()\nwhile time.time()-t<0.5: pass"])
        group.add(proc.pid)
        self.assertEqual((group.path / "cpu.max").read_text().strip(), "50000 100000")
        self.assertEqual((group.path / "pids.max").read_text().strip(), "64")
        proc.wait()
        stats = group.stats()
        self.assertGreater(stats["cpu_usec"], 0)
        self.assertEqual(stats["oom_kill"], 0)
        self.assertIsNotNone(stats["memory_peak_kb"])
        self.assertTrue(group.close())
        self.assertFalse(group.path.exists())

    def test_a_wrapped_command_and_its_first_child_start_inside_the_group(self):
        group = limits.CellGroup.create(f"swarm-join-{os.getpid()}", limits.Limits(pids=64))
        self.assertIsNotNone(group, limits.CellGroup.last_reason)
        try:
            done = subprocess.run(group.wrap(["/bin/sh", "-c", "cat /proc/self/cgroup; sh -c 'cat /proc/self/cgroup'"]),
                                  capture_output=True, text=True, timeout=30)
            self.assertEqual(done.returncode, 0, done.stderr)
            lines = [l for l in done.stdout.splitlines() if l.startswith("0::")]
            self.assertEqual(len(lines), 2)
            self.assertTrue(all(l.endswith("/" + group.name) for l in lines), lines)
        finally:
            self.assertTrue(group.close())


if __name__ == "__main__":
    unittest.main()
