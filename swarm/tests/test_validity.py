"""Every number means what its label says, and nothing the runner did is blamed on a harness.

Covers the 2026-11-12 x256 race: a prepared Hermes home copied 256 times filled the disk and read as Hermes failing;
summed RSS showed 18 GB in use on a 7.7 GB machine; sandboxes were left behind without a trace.
"""
import errno
import json
import os
import shutil
import stat
import sys
import threading
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
from helpers import OK_SCRIPT, make_spec, make_task, tempdir  # noqa: E402
from swarm import harnesses, host, runner  # noqa: E402
from swarm import sandbox as sandbox_module  # noqa: E402
from swarm.bundle import verify  # noqa: E402
from swarm.harnesses.mini import Mini  # noqa: E402
from swarm.live import failure_detail  # noqa: E402
from swarm.plan import freeze  # noqa: E402
from swarm.runner import Admission, infra_reason, run  # noqa: E402
from swarm.sandbox import remove_tree, tree_kb  # noqa: E402
from swarm.serve import Service  # noqa: E402
from swarm.summary import summarize  # noqa: E402


def plan_file(base, **kwargs):
    plan = freeze(make_spec(base, **kwargs))
    path = base / "plan.json"
    path.write_text(json.dumps(plan))
    return plan, path


def records(bundle):
    return [json.loads(line) for line in (bundle / "attempts.jsonl").read_text().splitlines()]


def make_seed(base: Path) -> Path:
    seed = base / "seed"
    (seed / "installs" / "k1" / "lib").mkdir(parents=True)
    (seed / "installs" / "k1" / "lib" / "big.bin").write_bytes(b"x" * 300_000)
    (seed / "installs" / "k1" / "facts.json").write_text(json.dumps({"env": str(seed.resolve() / "installs" / "k1")}))
    (seed / "state.db").write_text("state")
    return seed


class PreparedHomeTests(unittest.TestCase):
    def prepared(self, base):
        base = base.resolve()  # the runner hands adapters resolved homes
        seed = make_seed(base)
        h = harnesses.get("hermes", {"home": "prepared", "seed": str(seed)})
        h.validate_options()
        homes = []
        for name in ("a", "b"):
            home = base / name
            home.mkdir()
            h.prepare(home, "http://127.0.0.1:9/v1", "m", "medium", 16384)
            homes.append(home / ".hermes")
        return seed, h, homes

    def test_the_dependency_tree_is_shared_and_everything_else_is_private(self):
        with tempdir() as d:
            seed, h, (a, b) = self.prepared(Path(d))
            big = seed / "installs" / "k1" / "lib" / "big.bin"
            # One copy of the large tree on the machine, however many attempts there are.
            self.assertEqual(big.stat().st_ino, (a / "installs/k1/lib/big.bin").stat().st_ino)
            self.assertEqual(big.stat().st_ino, (b / "installs/k1/lib/big.bin").stat().st_ino)
            # What Hermes writes, and the file the adapter repoints, belong to one attempt only.
            for name in ("state.db", "installs/k1/facts.json", "config.yaml"):
                self.assertNotEqual((a / name).stat().st_ino, (b / name).stat().st_ino, name)
            self.assertNotEqual((a / "state.db").stat().st_ino, (seed / "state.db").stat().st_ino)
            (a / "state.db").write_text("changed by a")
            self.assertEqual(((b / "state.db").read_text(), (seed / "state.db").read_text()), ("state", "state"))
            self.assertEqual(json.loads((a / "installs/k1/facts.json").read_text())["env"], str(a.resolve() / "installs" / "k1"))
            self.assertIn("k1", json.loads((seed / "installs/k1/facts.json").read_text())["env"])
            self.assertEqual(h.prepared_method(), "hard-link installs/, copy the rest")

    def test_disk_accounting_counts_shared_files_once_and_not_per_attempt(self):
        with tempdir() as d:
            seed, h, (a, b) = self.prepared(Path(d))
            self.assertGreaterEqual(h.shared_kb, 290)
            self.assertLess(h.prepared_kb, 64)  # what each attempt really holds: the small copied part
            self.assertEqual(h.setup_disk_kb(), h.prepared_kb)
            self.assertLess(tree_kb(a, h.shared_inodes), 64)
            self.assertGreaterEqual(tree_kb(a), 290)  # a plain walk would charge the shared tree to every attempt

    def test_a_write_through_a_link_is_found_and_a_replaced_file_is_not_one(self):
        with tempdir() as d:
            seed, h, (a, b) = self.prepared(Path(d))
            self.assertEqual(h.shared_changes(), [])
            # Replacing a file (how Python writes .pyc, how installers write) leaves the shared copy alone.
            (a / "installs/k1/lib/new.tmp").write_bytes(b"mine")
            os.replace(a / "installs/k1/lib/new.tmp", a / "installs/k1/lib/big.bin")
            self.assertEqual(len((b / "installs/k1/lib/big.bin").read_bytes()), 300_000)
            self.assertEqual(h.shared_changes(), [])
            # Writing in place reaches every attempt: that must never pass silently.
            with (b / "installs/k1/lib/big.bin").open("ab") as f:
                f.write(b"leak")
            self.assertEqual(h.shared_changes(), ["installs/k1/lib/big.bin"])

    def test_when_links_are_refused_the_seed_is_copied_and_admission_is_told_the_real_cost(self):
        with tempdir() as d:
            base = Path(d)
            seed = make_seed(base)
            h = harnesses.get("hermes", {"home": "prepared", "seed": str(seed)})
            (base / "a").mkdir()
            with mock.patch.object(os, "link", side_effect=OSError(errno.EXDEV, "cross-device link")):
                h.plan_setup(base)  # the runner asks before the first launch
                h.prepare(base / "a", "http://127.0.0.1:9/v1", "m", "medium", 16384)
            self.assertNotEqual((base / "a/.hermes/installs/k1/lib/big.bin").stat().st_ino,
                                (seed / "installs/k1/lib/big.bin").stat().st_ino)
            self.assertEqual((h.prepared_method(), h.shared_kb), ("copy", 0))
            self.assertGreaterEqual(h.setup_disk_kb(), 290)

    def test_link_support_is_known_before_the_first_launch(self):
        with tempdir() as d:
            base = Path(d).resolve()
            seed = make_seed(base)
            (base / "root").mkdir()
            h = harnesses.get("hermes", {"home": "prepared", "seed": str(seed)})
            h.plan_setup(base / "root")
            self.assertLess(h.setup_disk_kb(), 64)
            self.assertEqual(list((base / "root").iterdir()), [])  # the probe leaves nothing behind
            refused = harnesses.get("hermes", {"home": "prepared", "seed": str(seed)})
            with mock.patch.object(os, "link", side_effect=OSError(errno.EXDEV, "cross-device link")):
                refused.plan_setup(base / "root")
            self.assertGreaterEqual(refused.setup_disk_kb(), 290)  # admission is told the full copy, up front

    def test_seed_safety_checks_still_refuse_links_and_credentials(self):
        with tempdir() as d:
            base = Path(d)
            for plant in (lambda s: (s / "out").symlink_to("/etc"), lambda s: (s / ".env").write_text("K=1"),
                          lambda s: (s / "installs" / "auth.json").write_text("{}")):
                seed = make_seed(base)
                plant(seed)
                with self.assertRaises(ValueError):
                    harnesses.get("hermes", {"home": "prepared", "seed": str(seed)}).validate_options()
                shutil.rmtree(seed)


class BlameTests(unittest.TestCase):
    """Our failures are ours."""

    def test_a_full_disk_during_setup_is_infra_with_a_reason_and_leaves_no_sandbox(self):
        with tempdir() as d:
            base = Path(d)
            make_task(base / "tasks", "one", OK_SCRIPT)
            _, path = plan_file(base, levels=(2,))
            destroyed, torn = [], []

            def full(self, home, *args):
                raise OSError(errno.ENOSPC, "No space left on device", str(home / "big"))

            original = sandbox_module.ProcessSandbox.destroy

            def destroy(self, box):
                out = original(self, box)
                destroyed.append((box["key"], box["base"].exists()))
                return out

            with mock.patch.object(Mini, "prepare", full), mock.patch.object(sandbox_module.ProcessSandbox, "destroy", destroy), \
                    mock.patch.object(Mini, "teardown", lambda self, binary, env: torn.append(1), create=True):
                bundle = run(path, base / "bundle")
            rows = records(bundle)
            self.assertEqual([(r["failure"], r["infra_reason"], r["infra_stage"], r["passed"]) for r in rows],
                             [("infra", "disk_full", "prepare", False)] * 2)
            self.assertEqual(sorted(destroyed), [("c000-mini-n2-s000", False), ("c000-mini-n2-s001", False)])
            self.assertEqual(torn, [])  # no agent was launched, so the harness's own stop command must not be run
            summary = json.loads((bundle / "summary.json").read_text())
            group = summary["groups"]["mini@2"]
            self.assertEqual((group["not_run"], group["infra_reasons"], summary["sandbox_root"]), (2, {"disk_full": 2}, {"removed": True}))
            self.assertEqual(verify(bundle), [])
            detail = failure_detail(rows[0])
            self.assertEqual((detail["infra_reason"], detail["infra_stage"]), ("disk_full", "prepare"))
            self.assertIn("not a failure of the harness", detail["message"])
            self.assertNotIn(str(base), json.dumps(detail))

    def test_infra_reasons(self):
        copy_error = shutil.Error([("a", "b", "[Errno 28] No space left on device: 'b'")])
        self.assertEqual(infra_reason(copy_error, "prepare"), "disk_full")
        self.assertEqual(infra_reason(OSError(errno.EDQUOT, "quota"), "run"), "disk_full")
        self.assertEqual(infra_reason(RuntimeError("can't start new thread"), "sandbox"), "setup_failed")
        self.assertEqual(infra_reason(RuntimeError("x"), "run"), "runner_error")

    def test_an_agent_that_fails_on_a_full_machine_is_not_blamed(self):
        with tempdir() as d:
            base = Path(d)
            wrong = [{"op": "write", "path": "out.txt", "content": "no"}, {"op": "final"}]
            make_task(base / "tasks", "wrong", wrong)
            _, path = plan_file(base, levels=(1,))
            real = host.disk_free_mb
            # Room at admission, none by the time the agent ends.
            with mock.patch.object(host, "disk_free_mb", lambda *paths: 3 if threading.current_thread().name.startswith("Thread") and
                                   getattr(threading.current_thread(), "_swarm_end", False) else real(*paths)):
                original = runner.execute

                def execute(*args, **kwargs):
                    out = original(*args, **kwargs)
                    threading.current_thread()._swarm_end = True
                    return out

                with mock.patch.object(runner, "execute", execute):
                    bundle = run(path, base / "bundle")
            row = records(bundle)[0]
            self.assertEqual((row["failure"], row["infra_reason"], row["harness_failure"]), ("infra", "disk_full", "check_failed"))
            self.assertEqual(failure_detail(row)["harness_failure"], "check_failed")

    def test_a_sandbox_that_cannot_be_removed_is_written_on_the_attempt_and_the_cell(self):
        with tempdir() as d:
            base = Path(d)
            make_task(base / "tasks", "one", OK_SCRIPT)
            _, path = plan_file(base, levels=(1,))
            with mock.patch.object(sandbox_module.ProcessSandbox, "destroy",
                                   lambda self, box: {"removed": False, "errors": ["EACCES"], "leftover_kb": 12}):
                bundle = run(path, base / "bundle")
            row = records(bundle)[0]
            self.assertTrue(row["passed"])  # our cleanup failing is not the agent failing
            self.assertEqual(row["cleanup"], {"sandbox_removed": False, "errors": ["EACCES"], "leftover_kb": 12})
            cell = json.loads((bundle / "cells.jsonl").read_text().splitlines()[0])
            self.assertEqual(cell["winddown"]["sandbox_leftovers"], 1)
            summary = json.loads((bundle / "summary.json").read_text())
            self.assertEqual(summary["groups"]["mini@1"]["sandbox_leftovers"], 1)
            self.assertEqual(summary["sandbox_root"], {"removed": True})  # the run's final sweep still took it
            self.assertEqual(verify(bundle), [])


class CleanupTests(unittest.TestCase):
    def test_remove_tree_takes_read_only_directories_and_reports_what_stays(self):
        with tempdir() as d:
            base = Path(d) / "box"
            (base / "home" / "locked" / "deep").mkdir(parents=True)
            (base / "home" / "locked" / "deep" / "f").write_text("x")
            os.chmod(base / "home" / "locked" / "deep", stat.S_IRUSR | stat.S_IXUSR)
            os.chmod(base / "home" / "locked", stat.S_IRUSR | stat.S_IXUSR)
            self.assertEqual(remove_tree(base), {"removed": True})
            self.assertFalse(base.exists())
            self.assertEqual(remove_tree(base), {"removed": True})  # already gone is clean
            (base / "stuck").mkdir(parents=True)
            with mock.patch.object(shutil, "rmtree", lambda path, **kw: None):
                out = remove_tree(base)
            self.assertEqual((out["removed"], "leftover_kb" in out), (False, True))

    def test_a_sandbox_that_fails_half_made_is_removed(self):
        with tempdir() as d:
            base = Path(d)
            task = mock.Mock(workspace=base / "nope")  # the copy fails after the sandbox directory was made
            box = sandbox_module.ProcessSandbox(base)
            with self.assertRaises(OSError):
                box.create("c000-mini-n1-s000", task)
            self.assertEqual(list(base.iterdir()), [])


class DiskTests(unittest.TestCase):
    def test_disk_per_agent_is_what_the_agent_wrote_not_our_setup_or_the_checkers_copy(self):
        with tempdir() as d:
            base = Path(d)
            root = make_task(base / "tasks", "one", OK_SCRIPT)
            (root / "workspace" / "data.bin").write_bytes(b"d" * 400_000)  # the task's own files are not the agent's
            _, path = plan_file(base, levels=(1,))
            bundle = run(path, base / "bundle")
            agent = records(bundle)[0]["agent"]
            self.assertTrue(records(bundle)[0]["passed"])
            self.assertLessEqual(agent["disk_kb"], 64)  # out.txt
            self.assertGreaterEqual(agent["disk_kb_setup"], 390)
            self.assertIn("between launch and exit", agent["disk_scope"])
            self.assertGreaterEqual(records(bundle)[0]["setup"]["setup_s"], 0)

    def test_launches_are_held_when_the_disk_has_no_room(self):
        state = {"pressure": "normal", "available_mb": 100000, "total_mb": 0, "swap_used_mb": 0, "pressure_source": "test"}
        tasks = {"tasks_free": None, "tasks_limit": None, "tasks_source": None}
        config = {"max_pressure": "normal", "reserve_mb": 0, "reserve_tasks": 0, "allow_unknown": False, "wait_timeout_s": 0.3}
        free = {"mb": 1200}
        with mock.patch.object(host, "memory_state", return_value=state), mock.patch.object(host, "task_headroom", return_value=tasks), \
                mock.patch.object(host, "disk_free_mb", lambda *paths: free["mb"]):
            admission = Admission(dict(config, reserve_disk_mb=1000), None, lambda row: None, disk_paths=("/",))
            first = admission.acquire("hermes", "a0", threading.Event(), disk_hint_mb=150)
            self.assertEqual((first["admitted"], first["disk_free_mb"], first["estimate_disk_mb"]), (True, 1200, 150.0))
            # 1200 free, 150 promised to a0 and not yet written, 150 more wanted: under the 1000 MB reserve.
            second = admission.acquire("hermes", "a1", threading.Event(), disk_hint_mb=150)
            self.assertEqual((second["admitted"], second["held_for"]), (False, ["disk_reserve"]))
            # a0's setup is on disk and shows in the reading: it is not counted twice.
            free["mb"] = 1150
            admission.settled("a0", 150)
            self.assertTrue(admission.acquire("hermes", "a2", threading.Event(), disk_hint_mb=150)["admitted"])
            # What attempts were seen to use outweighs a smaller hint.
            admission.learn_disk("hermes", 900)
            self.assertFalse(admission.acquire("hermes", "a3", threading.Event(), disk_hint_mb=1)["admitted"])
            free["mb"] = None
            self.assertEqual(admission.acquire("x", "a4", threading.Event())["held_for"], ["disk_unknown"])
            # A plan frozen before the gate existed runs as it did.
            old = Admission(config, None, lambda row: None, disk_paths=("/",))
            self.assertTrue(old.acquire("hermes", "b0", threading.Event(), disk_hint_mb=10**9)["admitted"])

    def test_new_plans_carry_a_disk_reserve(self):
        with tempdir() as d:
            base = Path(d)
            make_task(base / "tasks", "one", OK_SCRIPT)
            self.assertEqual(freeze(make_spec(base))["spec"]["admission"]["reserve_disk_mb"], 1024)


class MemoryTests(unittest.TestCase):
    def cgroup(self, base, anon, mapped, kernel=None, parts=0):
        cg = base / "cg"
        cg.mkdir(exist_ok=True)
        lines = [f"anon {anon}", "file 999999999999", f"file_mapped {mapped}", f"kernel_stack {parts}", f"pagetables {parts}",
                 "slab_reclaimable 3145728"]  # 3 MiB of directory-entry cache: never the agents'
        if kernel is not None:
            lines.append(f"kernel {kernel + 3145728}")
        (cg / "memory.stat").write_text("\n".join(lines) + "\n")
        return cg

    def test_cgroup_memory_counts_anon_mapped_and_kernel_but_not_page_cache(self):
        with tempdir() as d:
            base = Path(d)
            mib = 1024 * 1024
            self.assertEqual(host.cgroup_memory_kb(self.cgroup(base, 100 * mib, 20 * mib, kernel=5 * mib, parts=99 * mib)), 125 * 1024)
            # Kernels without the "kernel" total: its parts.
            self.assertEqual(host.cgroup_memory_kb(self.cgroup(base, 100 * mib, 20 * mib, parts=2 * mib)), 124 * 1024)
            self.assertIsNone(host.cgroup_memory_kb(base / "missing"))
            self.assertIsNone(host.cgroup_memory_kb(None))

    def test_pss_is_read_per_process(self):
        with tempdir() as d:
            proc = Path(d)
            (proc / "41").mkdir()
            (proc / "41" / "smaps_rollup").write_text("00400000-7ffd [rollup]\nRss:  90000 kB\nPss:  12345 kB\nPss_Anon: 1 kB\n")
            self.assertEqual(host.pss_kb(41, proc), 12345)
            self.assertIsNone(host.pss_kb(42, proc))

    def test_memory_in_use_never_sums_shared_pages_and_names_its_scope(self):
        with tempdir() as d:
            base = Path(d)
            mib = 1024 * 1024
            sampler = host.Sampler(1, lambda row: None)
            # 256 agents mapping one 70 MB binary: RSS sums to 18 GB; the machine holds the text once.
            rss_kb = 256 * 70 * 1024
            with mock.patch.object(host, "self_anon_kb", return_value=50 * 1024):
                sampler.cgroup = self.cgroup(base, 150 * mib, 10 * mib, kernel=0)
                sampler.mem_baseline_kb = sampler._cgroup_net_kb()  # idle: 150 anon - 50 runner + 10 mapped
                self.assertEqual(sampler.agents_memory(set(), 0), (0.0, host.MEM_CGROUP))
                self.cgroup(base, (150 + 2560) * mib, (10 + 70) * mib, kernel=40 * mib)
                mb, scope = sampler.agents_memory({1, 2}, rss_kb)
                self.assertEqual((mb, scope), (2560 + 70 + 40, host.MEM_CGROUP))
                self.assertLess(mb, rss_kb / 1024)
                # The runner's own growth is not the agents'.
                with mock.patch.object(host, "self_anon_kb", return_value=(50 + 300) * 1024):
                    self.assertEqual(sampler.agents_memory({1}, rss_kb)[0], 2560 + 70 + 40 - 300)
                self.cgroup(base, 1 * mib, 0, kernel=0)
                self.assertEqual(sampler.agents_memory(set(), 0)[0], 0.0)  # never negative
            # No cgroup: PSS where the kernel offers it, else the old sum, marked as overcounting.
            # A read that fails mid-run is an unknown sample in the same scope, never a jump to another measure.
            sampler.cgroup = None
            self.assertEqual(sampler.agents_memory({1}, rss_kb), (None, host.MEM_CGROUP))
            sampler.mem_baseline_kb = None
            sampler.has_pss = True
            with mock.patch.object(host, "pss_kb", lambda pid: {1: 2048, 2: 1024}.get(pid)):
                self.assertEqual(sampler.agents_memory({1, 2, 3}, rss_kb), (3.0, host.MEM_PSS))
            sampler.has_pss = False
            mb, scope = sampler.agents_memory({1, 2}, rss_kb)
            self.assertEqual((mb, scope), (rss_kb / 1024, host.MEM_RSS))
            self.assertIn("more than once", scope)

    def test_samples_carry_the_measure_and_its_scope(self):
        rows = []
        sampler = host.Sampler(0.05, rows.append).start()
        try:
            for _ in range(100):
                if rows:
                    break
                threading.Event().wait(0.05)
        finally:
            sampler.stop()
        self.assertIn("agents_mem_mb", rows[0])
        self.assertIn(rows[0]["agents_mem_scope"], (host.MEM_CGROUP, host.MEM_PSS, host.MEM_RSS))

    def test_the_chart_never_shows_summed_rss(self):
        with tempdir() as d:
            bundle = Path(d)
            old = [{"t": i, "running": 256, "agents_rss_mb": 18667.5, "cpu_pct": 90, "available_mb": 7000 - 500 * i, "total_mb": 7700}
                   for i in range(4)]
            (bundle / "samples.jsonl").write_text("".join(json.dumps(s) + "\n" for s in old))
            series = Service.series(bundle)
            self.assertEqual([row[2] for row in series], [0, 500, 1000, 1500])  # host memory in use above the first sample
            self.assertLess(max(row[2] for row in series), 7700)
            self.assertEqual(Service.mem_scope(bundle), "host")
            new = [{"t": 1, "running": 2, "agents_mem_mb": 300.5, "agents_mem_scope": host.MEM_CGROUP, "agents_rss_mb": 9999,
                    "cpu_pct": 5, "available_mb": 1, "total_mb": 2}]
            (bundle / "samples.jsonl").write_text("".join(json.dumps(s) + "\n" for s in new))
            self.assertEqual((Service.series(bundle)[0][2], Service.mem_scope(bundle)), (300.5, "cgroup"))


class SummaryShapeTests(unittest.TestCase):
    def test_an_old_bundle_with_a_cancelled_attempt_still_verifies_to_the_same_summary(self):
        row = {"harness": "mini", "level": 1, "passed": False, "failure": "cancelled", "admission": {"wait_s": 0},
               "agent": {"wall_s": 1.0, "termination": "cancelled"}, "requests": {"count": 0}}
        group = summarize([row], [{"harness": "mini", "level": 1, "batch_wall_s": 1.0, "max_running": 1}])["groups"]["mini@1"]
        self.assertNotIn("not_run", group)
        self.assertEqual(summarize([dict(row, disk_free_mb_end=900)], [])["groups"]["mini@1"]["not_run"], 1)

    def test_bundles_without_runner_failures_keep_their_summary_shape(self):
        row = {"harness": "mini", "level": 1, "passed": True, "failure": None, "admission": {"wait_s": 0},
               "agent": {"wall_s": 1.0, "cpu_s": 0.1, "max_process_rss_kb": 1024}, "requests": {"count": 2}}
        group = summarize([row], [{"harness": "mini", "level": 1, "batch_wall_s": 1.0, "max_running": 1}])["groups"]["mini@1"]
        for key in ("not_run", "infra_reasons", "sandbox_leftovers", "shared_seed_changed"):
            self.assertNotIn(key, group)


if __name__ == "__main__":
    unittest.main()


class LiveViewTests(unittest.TestCase):
    """What a lane tells the page about each agent while it runs and after."""

    def service(self, base: Path) -> Service:
        make_task(base / "tasks", "one", OK_SCRIPT)
        presets = base / "presets.json"
        presets.write_text(json.dumps({"presets": [{"id": "demo", "title": "Demo", "blurb": "b", "tasks": ["tasks/one"], "sizes": [1, 9]}]}))
        (base / "tmp").mkdir()
        svc = Service(presets, base / "data", harness="mini", reset_tmp=base / "tmp")
        self.addCleanup(svc.close)
        return svc

    def tmp(self) -> Path:
        t = tempdir()
        self.addCleanup(t.cleanup)
        return Path(t.name).resolve()

    def test_each_agent_has_one_named_state_and_setup_is_not_called_alive(self):
        svc = self.service(self.tmp())
        rid = "0123456789ab"
        logs = svc.run_dir(rid) / "bundle" / "logs"
        name = lambda slot: f"c000-mini-n9-s{slot:03d}"  # noqa: E731
        for slot, phase in ((2, "preparing"), (3, "launching"), (4, "launching"), (5, "agent_exited")):
            (logs / name(slot)).mkdir(parents=True)
            (logs / name(slot) / "lifecycle.live.json").write_text(json.dumps({"phase": phase}))
        (logs / name(4) / "requests.live.jsonl").write_text('{"op": "read"}\n')
        attempts = [{"attempt": name(6), "passed": True, "failure": None},
                    {"attempt": name(7), "passed": False, "failure": "timeout"},
                    {"attempt": name(8), "passed": False, "failure": "infra", "infra_reason": "disk_full", "infra_stage": "prepare"}]
        decisions = [{"attempt": name(slot), "admitted": True} for slot in range(1, 9)]
        progress = svc.progress(rid, {"size": 9, "state": "running", "created": "2026-11-12T00:00:00Z"}, attempts, decisions, {})
        self.assertEqual([a["state"] for a in progress["agents"]],
                         ["waiting", "preparing", "preparing", "starting", "working", "checking", "passed", "failed", "notrun"])
        self.assertEqual(progress["agents"][8]["failure_detail"]["infra_reason"], "disk_full")

    def test_a_setup_failure_reaches_the_page_as_not_run_with_its_reason(self):
        svc = self.service(self.tmp())

        def full(self, home, *args):
            raise OSError(errno.ENOSPC, "No space left on device", str(home))

        with mock.patch.object(Mini, "prepare", full):
            status, out = svc.launch("demo", 1, "1.2.3.4")
            self.assertEqual(status, 202)
            for _ in range(600):
                if svc.record(out["id"])["state"] in ("done", "failed"):
                    break
                threading.Event().wait(0.05)
        snap = svc.snapshot(out["id"], 0)
        row = snap["attempts"][0]
        self.assertEqual((row["passed"], row["failure"], row["ran"], row["infra"]),
                         (False, "infra", False, {"reason": "disk_full", "stage": "prepare"}))
        self.assertEqual(snap["progress"]["agents"][0]["state"], "notrun")
        self.assertEqual((snap["run"]["result"]["not_run"], snap["run"]["result"]["infra_reasons"]), (1, {"disk_full": 1}))
        self.assertIn(snap["mem_scope"], ("cgroup", "pss", "rss-sum", None))
