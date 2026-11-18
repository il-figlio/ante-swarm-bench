import json
import subprocess
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import tempdir  # noqa: E402
from swarm import cli, export, front, live  # noqa: E402

ATTEMPT = "c000-mini-n1-s000"


def write_bundle(bundle: Path, rows: list[dict], reqs: list[dict]):
    (bundle / "logs" / ATTEMPT).mkdir(parents=True)
    (bundle / "attempts.jsonl").write_text("".join(json.dumps(r) + "\n" for r in rows))
    (bundle / "logs" / ATTEMPT / "requests.jsonl").write_text("".join(json.dumps(r) + "\n" for r in reqs))


def attempt(**extra):
    return {"attempt": ATTEMPT, "task": "one", "passed": True, "failure": None,
            "agent": {"wall_s": 1.5, "cpu_s": 0.4, "max_process_rss_kb": 2048, "disk_kb": 8, "cmd": "/Users/bob/secret --key sk-abcdefabcdefabcdefabcd"},
            "setup": {"setup_s": 0.1}, "timing": {"first_request_s": 0.2, "step_gap_s_median": 0.01, "context_slope": 3.0},
            "requests": {"model_turns": 4, "probes": 0, "input_tokens": 10, "output_tokens": 5, "tools_used": ["read"],
                         "first_request": {"wire": "responses", "canonical_bytes": 700, "tool_count": 2, "request_bytes": 800,
                                           "tools_offered": ["bash", "/tmp/x/leak"], "canary": {"sha256": "ab" * 32}}}} | extra


class ExportTests(unittest.TestCase):
    def build(self):
        t = tempdir()
        self.addCleanup(t.cleanup)
        run = Path(t.name)
        reqs = [{"request_id": ATTEMPT + "#0", "prev_request_id": None, "prev_source": None, "upstream_id": "resp_1", "kind": "responses",
                 "model": "m-1", "request_bytes": 800, "recv_mono": 100.0, "sent_mono": 100.5, "input_tokens": 10, "output_tokens": 5,
                 "body": "prompt text /home/me/x", "user_agent": "host.exe.xyz"},
                {"request_id": ATTEMPT + "#1", "prev_request_id": ATTEMPT + "#0", "prev_source": "sequence", "kind": "responses",
                 "model": "m-1", "helper": True, "recv_mono": 101.25, "sent_mono": None}]
        write_bundle(run / "bundle", [attempt()], reqs)
        write_bundle(run / "control" / "bundle", [attempt(task="ctl")], reqs[:1])
        rec = {"id": "0123456789ab", "preset": "demo", "size": 1, "harness": "mini", "state": "done", "cohort": "c" * 12,
               "result": {"passes": 1, "failures": {"timeout": 1}}, "warnings": ["harness_changed"],
               "title": "visitor title", "client": "1.2.3.4", "error": "boom at /Users/me/x", "spec_path": "/tmp/spec",
               "canary": {"sha256": "ab" * 32, "stable": True, "changed": False, "wire": "responses"},
               "control": {"state": "done", "key": "k" * 12, "median": {"wall_s": 1.0}, "values": {"wall_s": [1.0, None]}, "plan_path": "/tmp/p"},
               "gate": [{"stage": "run", "ok": True, "values": {"swap_used_mb": 0}}], "harness_version": "Python 3.12.1"}
        return run, rec

    def test_only_allow_listed_keys_and_codes_survive(self):
        run, rec = self.build()
        doc = export.run_document(run, rec)
        for gone in ("title", "client", "error", "spec_path"):
            self.assertNotIn(gone, doc)
        self.assertNotIn("plan_path", doc["control"])
        self.assertNotIn("wire", doc["canary"])
        self.assertEqual((doc["result"]["failures"], doc["control"]["values"]["wall_s"]), ({"timeout": 1}, [1.0, None]))
        raw = json.dumps(doc)
        for bad in (front.HOST, front.IPV4, live.HOME, live.KEY, live.TEMP):
            self.assertIsNone(bad.search(raw), bad.pattern)
        for text in ("prompt text", "/Users", "/home", "secret", "leak"):
            self.assertNotIn(text, raw)

    def test_rows_hold_one_line_per_attempt_and_mark_the_control(self):
        run, rec = self.build()
        rows = export.run_document(run, rec)["rows"]
        self.assertEqual([r.get("control") for r in rows], [None, True])
        r = rows[0]
        self.assertEqual((r["wall_s"], r["cpu_s"], r["rss_kb"], r["disk_kb"], r["setup_s"], r["model_turns"]), (1.5, 0.4, 2048, 8, 0.1, 4))
        self.assertEqual(r["tokens"], {"input_tokens": 10, "output_tokens": 5})
        self.assertEqual((r["first_request"]["tool_count"], r["first_request"]["canary_sha256"]), (2, "ab" * 32))
        self.assertEqual(r["first_request"]["tools"], ["bash"])  # a tool name that is not a short code is dropped

    def test_partial_cpu_is_unknown_not_a_number(self):
        run, rec = self.build()
        write_bundle(run / "bundle2", [attempt(agent={"wall_s": 1.0, "cpu_s": 0.3, "cpu_s_partial": True})], [])
        self.assertIsNone(export.rows(run / "bundle2")[0]["cpu_s"])

    def test_requests_carry_ids_tokens_and_time_from_the_attempts_first_request(self):
        run, rec = self.build()
        reqs = export.run_document(run, rec)["requests"]
        self.assertEqual(len(reqs), 3)
        first, second = reqs[0], reqs[1]
        self.assertEqual((first["recv_s"], first["sent_s"], second["recv_s"], second["sent_s"]), (0.0, 0.5, 1.25, None))
        self.assertEqual((second["prev_request_id"], second["prev_source"], second["helper"]), (ATTEMPT + "#0", "sequence", True))
        self.assertEqual((first["tokens"], first["request_bytes"]), ({"input_tokens": 10, "output_tokens": 5}, 800))
        self.assertNotIn("upstream_id", first)  # the provider's id is not published, only its hash
        self.assertEqual(first["upstream_sha12"], export.sha256_bytes(b"resp_1")[:12])
        self.assertNotIn("body", first)
        self.assertTrue(reqs[2]["control"])

    def test_a_run_without_a_bundle_still_exports_its_record(self):
        t = tempdir()
        self.addCleanup(t.cleanup)
        doc = export.run_document(Path(t.name), {"id": "0123456789ab", "state": "failed", "refused": "not_quiet"})
        self.assertEqual((doc["rows"], doc["requests"], doc["canary"], doc["control"]), ([], [], None, None))


NOW = 1_800_000_000.0  # pinned clock for staleness
DAY = 86400
HOSTILE = "build7.exe.xyz 10.1.2.3 /Users/bob/x sk-abcdefabcdefabcdefabcdef"


def result(passes=4, gap=0.5):
    return {"attempts": 4, "passes": passes, "wall_s_median": 2.0, "cpu_s_median": 1.0, "step_gap_s_median": gap,
            "first_request_s_median": 0.3, "peak_rss_mb_median": 100.0, "successes_per_minute": 3.0, "harness": "pi"}


def control(key="k1"):
    return {"state": "done", "key": key, "noisy": False, "median": {"cpu_s": 0.5, "first_request_s": 0.1, "step_gap_s": 0.25, "wall_s": 1.0}}


def entry(cohort, gap=0.5, **extra):
    return {"run": "r" * 12, "harness": "pi", "state": "done", "error": None, "result": result(gap=gap), "control": control(),
            "gate": [{"stage": "run", "ok": True, "values": {"swap_used_mb": 0}}], "cohort": cohort,
            "harness_version": "pi 1.2.3", "harness_sha256": "ab" * 32, "adapter_sha256": "cd" * 32,
            "canary": {"sha256": "ef" * 32, "changed": False}, "export": True} | extra


def race(rid, age_days, reps, state="done", **extra):
    t = NOW - age_days * DAY
    r = {"id": rid, "preset": "demo", "size": 1, "protocol": "naive", "model": "m-1", "effort": "low", "kind": "harness",
         "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(t)), "t": t, "state": state, "repeats": len(reps), "seed": 7,
         "rep": len(reps) - 1, "reps": [{"k": k, "order": "control_first", "started": "2026-01-01T00:00:00Z",
                                         "finished": "2026-01-01T00:01:00Z", "lanes": lanes} for k, lanes in enumerate(reps)]}
    return r | extra


def write_front(root: Path, races: list[dict]):
    (root / "races.jsonl").write_text("".join(json.dumps(r) + "\n" for r in races))
    for r in races:
        for rep in r["reps"]:
            for lane, e in rep["lanes"].items():
                if not e.get("run"):
                    continue
                doc = {"id": e["run"], "preset": r["preset"], "state": e["state"], "cohort": e["cohort"], "harness": e["harness"],
                       "harness_version": HOSTILE, "harness_sha256": e["harness_sha256"], "adapter_sha256": e["adapter_sha256"],
                       "plan_sha256": "12" * 32, "settings": {"gate": "enforce", "gate_limits": {"quiet_s": 5}, "control": True, "path": "/Users/x"},
                       "canary": e["canary"], "control": e["control"], "gate": e["gate"], "error": HOSTILE,
                       "rows": [{"attempt": "a1", "task": "one", "passed": True, "wall_s": 1.0, "host": HOSTILE},
                                {"attempt": "a1", "task": "ctl", "passed": True, "wall_s": 0.5, "control": True}],
                       "requests": [{"attempt": "a1", "request_id": "a1#0", "kind": "responses", "tokens": {"input_tokens": 3}}]}
                d = root / "export" / "v1" / "races" / r["id"] / f"rep-{rep['k']}"
                d.mkdir(parents=True, exist_ok=True)
                (d / f"{lane}.json").write_text(json.dumps(doc))


class TreeTests(unittest.TestCase):
    def tree(self, extra=()):
        t = tempdir()
        self.addCleanup(t.cleanup)
        base = Path(t.name)
        steady = [{"pi": entry("c2")}] * 3
        changed = [{"pi": entry("c1")}, {"pi": entry("c1")}, {"pi": entry("c2", error=HOSTILE, harness_version=HOSTILE)}]
        older = [{"pi": entry("c1")}] * 2
        races = [race("aaaaaaaaaaaa", 90, older), race("bbbbbbbbbbbb", 10, changed), race("cccccccccccc", 5, steady), *extra]
        write_front(base, races)
        out = base / "out"
        got = export.build(base, out, now=NOW)
        return base, out, got

    def index(self, out):
        races = json.loads((out / "v1" / "index.json").read_text())["races"]
        return {r["id"]: r for r in races}

    def test_supersede_follows_policy_rule_3_and_rule_5(self):
        _, out, got = self.tree()
        idx = self.index(out)
        self.assertEqual(idx["aaaaaaaaaaaa"]["superseded_by"], {"pi": "c2"})  # older cohort c1, newest c2
        self.assertEqual(idx["bbbbbbbbbbbb"]["superseded_by"], {"pi": "c2"})  # its c1 reps are older rows too
        self.assertEqual(idx["cccccccccccc"]["superseded_by"], {"pi": None})
        self.assertEqual(idx["bbbbbbbbbbbb"]["cohorts"], {"pi": ["c1", "c2"]})
        summary = json.loads((out / "v1" / "races" / "bbbbbbbbbbbb" / "summary.json").read_text())
        wall = summary["stats"]["metrics"]["gap"]["lanes"]["pi"]
        self.assertEqual((wall["excluded"], wall["n"]), ("cohort_changed", 0))  # rule 5: no statistics, rows stay
        rows = [json.loads(l) for l in (out / "v1" / "races" / "bbbbbbbbbbbb" / "rows.jsonl").read_text().splitlines()]
        self.assertEqual(len(rows), 6)
        self.assertEqual(sum(1 for r in rows if r.get("control")), 3)
        self.assertEqual({(r["race"], r["lane"]) for r in rows}, {("bbbbbbbbbbbb", "pi")})
        self.assertEqual(got["races"], 3)

    def test_a_race_older_than_sixty_days_is_stale(self):
        _, out, _ = self.tree()
        idx = self.index(out)
        self.assertEqual([idx[k]["stale"] for k in ("aaaaaaaaaaaa", "bbbbbbbbbbbb", "cccccccccccc")], [True, False, False])

    def test_an_interrupted_series_exports_what_it_has(self):
        part = race("dddddddddddd", 1, [{"pi": entry("c2")}, {"pi": {"run": None, "harness": "pi", "state": "failed", "error": "offline"}}],
                    state="interrupted", repeats=6)
        part["reps"][1]["finished"] = None
        base, out, _ = self.tree([part])
        self.assertTrue(self.index(out)["dddddddddddd"]["interrupted"])
        manifest = json.loads((out / "v1" / "races" / "dddddddddddd" / "manifest.json").read_text())
        self.assertTrue(manifest["interrupted"])
        self.assertEqual(manifest["repeats"], 6)
        rows = (out / "v1" / "races" / "dddddddddddd" / "rows.jsonl").read_text().splitlines()
        self.assertEqual(len(rows), 2)  # the repetition that never ran has no document
        self.assertEqual(json.loads((out / "v1" / "races" / "dddddddddddd" / "summary.json").read_text())["stats"]["n"], 1)

    def test_nothing_hostile_reaches_the_tree(self):
        _, out, _ = self.tree()
        files = [p for p in out.rglob("*") if p.is_file()]
        self.assertGreater(len(files), 10)
        for p in files:
            raw = p.read_text()
            for bad in (front.HOST, front.IPV4, live.HOME, live.KEY):
                self.assertIsNone(bad.search(raw), (p.name, bad.pattern))
            self.assertNotIn("/Users", raw)
        manifest = json.loads((out / "v1" / "races" / "bbbbbbbbbbbb" / "manifest.json").read_text())
        lane = manifest["lanes"]["pi"]
        self.assertEqual((lane["harness_sha256"], lane["adapter_sha256"], lane["canary_sha256"], lane["control_key"]),
                         ("ab" * 6, "cd" * 32, "ef" * 32, "k1"))
        self.assertEqual((lane["gate_limits"], lane["plan_sha256"], lane["harness_version"]), ({"quiet_s": 5}, "12" * 32, None))
        self.assertEqual(lane["gate_mode"], "enforce")
        reps = [json.loads(l) for l in (out / "v1" / "races" / "bbbbbbbbbbbb" / "reps.jsonl").read_text().splitlines()]
        self.assertEqual(reps[2]["lanes"]["pi"]["error"], "redacted")  # the error stays an error without its text
        gates = (out / "v1" / "races" / "bbbbbbbbbbbb" / "gates.jsonl").read_text().splitlines()
        self.assertEqual(len(gates), 3)

    def test_a_rebuild_is_byte_identical(self):
        base, out, _ = self.tree()
        again = base / "again"
        export.build(base, again, now=NOW)
        for p in out.rglob("*"):
            if p.is_file():
                self.assertEqual(p.read_bytes(), (again / p.relative_to(out)).read_bytes(), p.name)

    def run_tool(self, out):
        tool = Path(__file__).resolve().parents[1] / "tools" / "rebuild_export.py"
        return subprocess.run([sys.executable, str(tool), str(out)], capture_output=True, text=True)

    def test_rebuild_tool_matches_and_catches_a_tampered_summary(self):
        _, out, _ = self.tree()
        done = self.run_tool(out)
        self.assertEqual((done.returncode, done.stderr), (0, ""))
        summary = out / "v1" / "races" / "cccccccccccc" / "summary.json"
        summary.write_text(summary.read_text().replace('"passes":4', '"passes":3', 1))
        bad = self.run_tool(out)
        self.assertEqual(bad.returncode, 1)
        self.assertIn("MISMATCH cccccccccccc", bad.stdout)

    def test_cli_export_prints_one_line(self):
        t = tempdir()
        self.addCleanup(t.cleanup)
        base = Path(t.name)
        write_front(base, [race("eeeeeeeeeeee", 1, [{"pi": entry("c1")}])])
        import contextlib
        import io
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            code = cli.main(["export", "--front-data", str(base), "-o", str(base / "o")])
        self.assertEqual(code, 0)
        self.assertEqual(len(buf.getvalue().strip().splitlines()), 1)
        self.assertTrue((base / "o" / "v1" / "index.json").exists())


if __name__ == "__main__":
    unittest.main()
