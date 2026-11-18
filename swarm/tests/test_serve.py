import json
import os
import shutil
import stat
import subprocess
import sys
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import OK_SCRIPT, make_task, tempdir  # noqa: E402
from swarm.bundle import verify  # noqa: E402
from swarm.serve import Service, canary_of, handler_for, lane_peaks  # noqa: E402
from swarm.util import read_jsonl  # noqa: E402

FRONT = Path(__file__).resolve().parents[1] / "front"
NODE = shutil.which("node")
PEAKS = "global.window = {}; const R = require(process.argv[process.argv.length - 1] + '/rank.js'); " \
        "const i = JSON.parse(require('fs').readFileSync(0, 'utf8')); console.log(JSON.stringify(R.peaks(i.series, i.rss.map((v) => ({ rss_mb: v })))));"


def first(sha, wire="responses"):
    return {"requests": {"first_request": {"wire": wire, "canary": {"sha256": sha, "tools_sha256": "t", "system_sha256": "s", "injected_sha256": "i", "params_sha256": "p"}}}}


class ServeTests(unittest.TestCase):
    def start(self, base: Path, secret=None, **kw):
        make_task(base / "tasks", "one", OK_SCRIPT)
        presets = base / "presets.json"
        presets.write_text(json.dumps({"presets": [{"id": "demo", "title": "Demo", "blurb": "b", "tasks": ["tasks/one"], "sizes": [1, 3]}]}))
        svc = Service(presets, base / "data", harness="mini", **kw)
        server = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(svc, secret))
        server.daemon_threads = True
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.addCleanup(svc.close)
        return svc, f"http://127.0.0.1:{server.server_address[1]}"

    def call(self, url, body=None, headers=None):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def tmp(self) -> str:
        t = tempdir()
        self.addCleanup(t.cleanup)  # registered first, so it runs after the service and server stop
        return t.name

    def test_run_streams_and_seals_a_verified_bundle(self):
        d = self.tmp()
        svc, url = self.start(Path(d))
        status, body = self.call(url + "/api/runs", {"preset": "demo", "size": 3})
        self.assertEqual(status, 202)
        rid = json.loads(body)["id"]
        status, stream = self.call(f"{url}/api/runs/{rid}/events")
        events = [json.loads(line[6:]) for line in stream.decode().splitlines() if line.startswith("data: ")]
        self.assertEqual(events[-1]["run"]["state"], "done", events[-1])
        self.assertEqual(sum(len(e["attempts"]) for e in events), 3)
        final = events[-1]["run"]
        self.assertEqual((final["verify"], final["result"]["passes"], final["result"]["attempts"]), ("verified", 3, 3))
        self.assertEqual(verify(svc.run_dir(rid) / "bundle"), [])

    def test_protocol_is_checked_size_one_repeats_and_meter_can_skip(self):
        base = Path(self.tmp())
        make_task(base / "tasks", "one", OK_SCRIPT)
        presets = base / "presets.json"
        presets.write_text(json.dumps({"presets": [
            {"id": "demo", "title": "Demo", "blurb": "b", "tasks": ["tasks/one"], "sizes": [1], "kind": "work"},
            {"id": "cold", "title": "Cold", "blurb": "b", "tasks": ["tasks/one"], "sizes": [1], "kind": "harness",
             "protocols": ["naive"], "primary_outcome": "first_request_s"},
            {"id": "lane-baseline", "title": "Base", "blurb": "b", "tasks": ["tasks/one"], "sizes": [1],
             "kind": "baseline", "harnesses": ["mini"]},
        ]}))
        svc = Service(presets, base / "data", harness="mini", per_client_hour=1)
        self.addCleanup(svc.close)
        self.assertEqual(svc.launch("cold", 1, "c", protocol="collapsed")[0], 400)
        self.assertEqual(svc.launch("demo", 1, "c", meter=False)[0], 202)
        self.assertEqual(svc.launch("demo", 1, "c", meter=False)[0], 202)
        code, body = svc.launch("cold", 1, "c")
        self.assertEqual(code, 202, body)
        self.assertEqual(svc.launch("demo", 1, "c")[0], 429)
        rid = body["id"]
        spec = svc.run_dir(rid) / "spec.toml"
        for _ in range(200):
            if spec.exists() and spec.stat().st_size:
                break
            time.sleep(0.05)
        text = spec.read_text()
        self.assertIn("repeats = 5", text)
        self.assertIn('protocol = "naive"', text)
        self.assertIn('primary_outcome = "first_request_s"', text)
        names = [p["id"] for p in svc.public_presets()]
        self.assertNotIn("lane-baseline", names)
        self.assertIn("cold", names)

    def test_only_presets_and_listed_sizes_are_accepted(self):
        d = self.tmp()
        _, url = self.start(Path(d))
        for body in ({"preset": "nope", "size": 1}, {"preset": "demo", "size": 2}, {"preset": "demo", "size": "x"},
                     {"preset": "../tasks/one", "size": 1}):
            self.assertEqual(self.call(url + "/api/runs", body)[0], 400, body)
        self.assertEqual(self.call(url + "/api/runs/../../etc/passwd")[0], 404)
        self.assertEqual(self.call(url + "/api/runs/abcdefabcdef")[0], 404)

    def test_real_models_are_allowlisted_capped_and_planned_through_the_relay(self):
        d = self.tmp()
        live = {"upstream": "http://127.0.0.1:9/gw", "models": ["gpt-x"], "max_size": 1, "max_tokens": 5000, "effort": "medium"}
        svc, url = self.start(Path(d), per_client_hour=100, live=live)
        self.assertEqual(self.call(url + "/api/runs", {"preset": "demo", "size": 1, "model": "gpt-other"})[0], 400)
        self.assertEqual(self.call(url + "/api/runs", {"preset": "demo", "size": 3, "model": "gpt-x"})[0], 400)
        code, body = self.call(url + "/api/runs", {"preset": "demo", "size": 1, "model": "gpt-x"})
        self.assertEqual(code, 202, body)
        rid = json.loads(body)["id"]
        spec = Path(d) / "data" / "runs" / rid / "spec.toml"
        for _ in range(200):
            if spec.exists() and spec.stat().st_size:  # the file exists, empty, a moment before it is written
                break
            time.sleep(0.05)
        text = spec.read_text()
        self.assertIn('source = "relay", id = "gpt-x"', text)
        self.assertIn('upstream = "http://127.0.0.1:9/gw", max_run_tokens = 5000', text)
        self.assertEqual(json.loads(self.call(url + f"/api/runs/{rid}")[1])["run"].get("model"), "gpt-x")
        # A per-agent budget scales the run's budget with its size.
        svc.live = dict(svc.live, agent_tokens=1000)
        self.assertEqual((svc.run_tokens(1), svc.run_tokens(256)), (1000, 256000))
        svc.live["agent_tokens"] = 0
        self.assertEqual(svc.run_tokens(256), 5000)
        # Without a live config, a lane is scripted only.
        _, url2 = self.start(Path(self.tmp()), per_client_hour=100)
        self.assertEqual(self.call(url2 + "/api/runs", {"preset": "demo", "size": 1, "model": "gpt-x"})[0], 400)

    def test_live_effort_is_chosen_per_launch_and_wire_must_fit_the_harness(self):
        d = self.tmp()
        live = {"upstream": "http://127.0.0.1:9/gw", "models": ["gpt-6.1-sol", "glm-5.3-flash-free"], "max_size": 1,
                "max_tokens": 5000, "effort": "medium"}
        _, url = self.start(Path(d), per_client_hour=100, live=live)
        # mini speaks Responses only: a Chat-Completions-only model is refused for it.
        self.assertEqual(self.call(url + "/api/runs", {"preset": "demo", "size": 1, "model": "glm-5.3-flash-free"})[0], 400)
        self.assertEqual(self.call(url + "/api/runs", {"preset": "demo", "size": 1, "model": "gpt-6.1-sol", "effort": "max"})[0], 400)
        # GPT 6.1 is offered at low or medium only.
        self.assertEqual(self.call(url + "/api/runs", {"preset": "demo", "size": 1, "model": "gpt-6.1-sol", "effort": "high"})[0], 400)
        code, body = self.call(url + "/api/runs", {"preset": "demo", "size": 1, "model": "gpt-6.1-sol", "effort": "low"})
        self.assertEqual(code, 202, body)
        spec = Path(d) / "data" / "runs" / json.loads(body)["id"] / "spec.toml"
        for _ in range(200):
            if spec.exists() and spec.stat().st_size:
                break
            time.sleep(0.05)
        self.assertIn('id = "gpt-6.1-sol", effort = "low"', spec.read_text())
        self.assertIn('wire = "responses"', spec.read_text())

    def test_rate_limit_and_queue_cap(self):
        d = self.tmp()
        _, url = self.start(Path(d), per_client_hour=2, max_queue=10)
        codes = [self.call(url + "/api/runs", {"preset": "demo", "size": 1})[0] for _ in range(3)]
        self.assertEqual(codes, [202, 202, 429])
        d = self.tmp()
        svc, url = self.start(Path(d), per_client_hour=100, max_queue=1)
        with svc.lock:  # a queued run the worker has not picked up yet
            svc.order.append("000000000000")
        self.assertEqual(svc.launch("demo", 1, "c")[0], 503)

    def test_passkey_adds_launches_and_limits_guesses(self):
        d = self.tmp()
        svc, url = self.start(Path(d), per_client_hour=1, max_queue=50, passkey="pk", passkey_bonus=2, passkey_tries_hour=2)
        post = lambda **extra: self.call(url + "/api/runs", {"preset": "demo", "size": 1, **extra})
        self.assertEqual(post()[0], 202)
        status, body = post()
        self.assertEqual((status, json.loads(body)["passkey"]), (429, True))
        self.assertEqual([post(passkey="pk")[0] for _ in range(3)], [202, 202, 429])
        self.assertEqual([post(passkey="nope")[0] for _ in range(3)], [403, 403, 429])
        self.assertEqual(post(passkey="pk")[0], 429)  # guess cap applies to the right key too, for that hour

    def test_ingress_secret_is_required_when_set(self):
        d = self.tmp()
        _, url = self.start(Path(d), secret="s3cret")
        self.assertEqual(self.call(url + "/api/healthz")[0], 200)
        self.assertEqual(self.call(url + "/api/presets")[0], 403)
        self.assertEqual(self.call(url + "/api/presets", headers={"X-Swarm-Ingress": "wrong"})[0], 403)
        status, body = self.call(url + "/api/presets", headers={"X-Swarm-Ingress": "s3cret"})
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(body)["presets"][0]["tasks"], ["one"])
        self.assertNotIn(str(Path(d)), body.decode())

    # ---- maintenance gate ----
    def test_maintenance_blocks_launches_reports_status_and_lifts(self):
        d = self.tmp()
        svc, url = self.start(Path(d))
        self.assertIsNone(self.get(url + "/api/status")["maintenance"])
        flag = svc.data / "MAINTENANCE"
        until = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() + 25 * 60))
        flag.write_text(until + "\n")
        status, body = self.call(url + "/api/runs", {"preset": "demo", "size": 1})
        out = json.loads(body)
        self.assertEqual((status, out["error"], out["until"]), (503, "benchmark in progress", until))
        self.assertIn(out["minutes"], (25, 26))
        m = self.get(url + "/api/status")["maintenance"]
        self.assertEqual((m["until"], m["minutes"] in (25, 26)), (until, True))
        # a bad request is still a bad request, but nothing is queued while the file exists
        self.assertEqual(svc.order, [])
        flag.write_text(json.dumps({"until": until}))
        self.assertEqual(self.get(url + "/api/status")["maintenance"]["until"], until)
        flag.write_text("")  # no end time known: still maintenance
        self.assertEqual(self.get(url + "/api/status")["maintenance"], {"until": None, "minutes": None})
        flag.write_text("not a time")
        self.assertEqual(self.call(url + "/api/runs", {"preset": "demo", "size": 1})[0], 503)
        flag.write_text("2020-01-01T00:00:00Z")  # in the past: still blocks until the file is removed, minutes 0
        self.assertEqual(self.get(url + "/api/status")["maintenance"]["minutes"], 0)
        flag.unlink()
        self.assertEqual(self.call(url + "/api/runs", {"preset": "demo", "size": 1})[0], 202)

    def test_run_in_progress_finishes_first_and_queued_runs_wait(self):
        d = self.tmp()
        svc, url = self.start(Path(d), per_client_hour=100, max_queue=10)
        started, release, ran = threading.Event(), threading.Event(), []
        real = svc._execute

        def slow(rid):
            ran.append(rid)
            started.set()
            release.wait(20)
            real(rid)

        svc._execute = slow
        svc.maintenance_poll_s = 0.05
        a = json.loads(self.call(url + "/api/runs", {"preset": "demo", "size": 1})[1])["id"]
        self.assertTrue(started.wait(10))
        b = json.loads(self.call(url + "/api/runs", {"preset": "demo", "size": 1})[1])["id"]
        (svc.data / "MAINTENANCE").write_text("")
        self.assertEqual(self.get(url + "/api/status")["running"]["id"], a)  # still the one in progress
        release.set()
        time.sleep(1.5)
        self.assertEqual(ran, [a])  # b has not started while the file exists
        self.assertEqual(svc.record(b)["state"], "queued")
        (svc.data / "MAINTENANCE").unlink()
        deadline = time.time() + 20
        while svc.record(b)["state"] != "done" and time.time() < deadline:
            time.sleep(0.2)
        self.assertEqual((ran, svc.record(a)["state"], svc.record(b)["state"]), ([a, b], "done", "done"))

    # ---- harness picker ----
    def test_harness_picker_offers_only_listed_available_harnesses(self):
        import swarm.harnesses as registry
        from swarm.harnesses.mini import Mini

        class Mini2(Mini):
            name = "mini2"

        registry.REGISTRY["mini2"] = Mini2
        self.addCleanup(registry.REGISTRY.pop, "mini2", None)
        d = Path(self.tmp())
        make_task(d / "tasks", "one", OK_SCRIPT)
        presets = d / "presets.json"
        presets.write_text(json.dumps({"presets": [
            {"id": "demo", "title": "Demo", "blurb": "b", "tasks": ["tasks/one"], "sizes": [1], "harnesses": ["mini", "mini2", "codex"]},
            {"id": "solo", "title": "Solo", "blurb": "b", "tasks": ["tasks/one"], "sizes": [1]}]}))
        svc = Service(presets, d / "data", harness="mini", per_client_hour=100, binaries={"codex": "/nonexistent/codex"})
        self.addCleanup(svc.close)
        listed = {p["id"]: p["harnesses"] for p in svc.public_presets()}
        # codex has no binary on this machine, so it is not offered; the default always is
        self.assertEqual(listed, {"demo": ["mini", "mini2"], "solo": ["mini"]})
        self.assertEqual(svc.launch("demo", 1, "c", harness="codex")[0], 400)
        self.assertEqual(svc.launch("demo", 1, "c", harness="../x")[0], 400)
        self.assertEqual(svc.launch("demo", 1, "c", harness=7)[0], 400)
        self.assertEqual(svc.launch("solo", 1, "c", harness="mini2")[0], 400)
        status, out = svc.launch("demo", 1, "c", harness="mini2")
        self.assertEqual(status, 202)
        status, default = svc.launch("demo", 1, "c")
        self.assertEqual(status, 202)
        for rid, harness in ((out["id"], "mini2"), (default["id"], "mini")):
            deadline = time.time() + 30
            while (svc.record(rid) or {}).get("state") != "done" and time.time() < deadline:
                time.sleep(0.2)
            rec = svc.record(rid)
            self.assertEqual((rec["state"], rec["harness"], rec["result"]["passes"]), ("done", harness, 1))
            self.assertIn(f'harnesses = ["{harness}"]', (svc.run_dir(rid) / "spec.toml").read_text())
        entries = {e["id"]: e["harness"] for e in svc.ledger(10)}
        self.assertEqual(entries, {out["id"]: "mini2", default["id"]: "mini"})

    def test_legacy_ledger_entries_read_as_ante(self):
        from swarm.serve import Service as S
        self.assertEqual(S.ledger_entry({"id": "x"})["harness"], "ante")
        self.assertEqual(S.ledger_entry({"id": "x", "harness": "codex"})["harness"], "codex")

    def run_to_done(self, url, n=1, size=1):
        ids = []
        for _ in range(n):
            status, body = self.call(url + "/api/runs", {"preset": "demo", "size": size})
            self.assertEqual(status, 202)
            ids.append(json.loads(body)["id"])
        for rid in ids:
            self.call(f"{url}/api/runs/{rid}/events")
        return ids

    def get(self, url):
        status, body = self.call(url)
        self.assertEqual(status, 200)
        return json.loads(body)

    def test_ledger_appends_survives_prune_and_holds_no_client_data(self):
        d = self.tmp()
        svc, url = self.start(Path(d), keep=2, per_client_hour=100, max_queue=10)
        ids = self.run_to_done(url, n=3, size=3)
        deadline = time.time() + 30
        while len(svc.ledger(10)) < 3 and time.time() < deadline:
            time.sleep(0.2)
        time.sleep(0.5)  # prune runs right after the last append
        runs = self.get(url + "/api/ledger")["runs"]
        self.assertEqual([r["id"] for r in runs], ids[::-1])  # newest first
        e = runs[0]
        self.assertEqual((e["state"], e["verify"], e["passes"], e["attempts"], e["size"], e["preset"]), ("done", "verified", 3, 3, 3, "demo"))
        for k in ("created", "started", "finished", "chain_head", "plan_sha256", "harness_version", "max_running",
                  "successes_per_minute", "wall_s_median", "peak_rss_mb_median", "cpu_s_per_success", "admission_wait_s_total"):
            self.assertIn(k, e)
        # keep=2 pruned the oldest bundle; its ledger line stays, flagged as not kept
        self.assertEqual([r["bundle_kept"] for r in runs], [True, True, False])
        self.assertFalse(svc.run_dir(ids[0]).exists())
        text = svc.ledger_path.read_text()
        self.assertEqual(len(text.splitlines()), 3)
        for word in ("127.0.0.1", "client", "passkey", "ip", "Real-IP"):
            self.assertNotIn(f'"{word}', text)
        self.assertNotIn("127.0.0.1", text)
        self.assertNotIn(str(Path(d)), text)

    def test_ledger_limit_bounds_and_failed_runs(self):
        d = self.tmp()
        svc, url = self.start(Path(d))
        for i in range(3):
            svc._ledger_append({"id": f"{i:012x}", "preset": "demo", "title": "Demo", "size": 1, "state": "failed", "error": "x"})
        self.assertEqual(len(self.get(url + "/api/ledger?limit=2")["runs"]), 2)
        self.assertEqual(len(self.get(url + "/api/ledger?limit=0")["runs"]), 3)   # bad value: default
        self.assertEqual(len(self.get(url + "/api/ledger?limit=99999")["runs"]), 3)  # capped at 500
        self.assertEqual(svc.ledger(10**9)[0]["id"], "000000000002")
        self.assertEqual(len(svc.ledger(-5)), 1)
        e = self.get(url + "/api/ledger?limit=1")["runs"][0]
        self.assertEqual((e["state"], e["bundle_kept"], e["passes"]), ("failed", False, None))
        self.assertNotIn("error", e)

    def test_ledger_backfills_from_existing_runs_when_missing(self):
        d = self.tmp()
        svc, url = self.start(Path(d))
        (rid,) = self.run_to_done(url)
        deadline = time.time() + 30
        while not svc.ledger(1) and time.time() < deadline:
            time.sleep(0.2)
        svc.close()
        other = svc.run_dir("aaaaaaaaaaaa")
        other.mkdir()
        (other / "run.json").write_text(json.dumps({"id": "aaaaaaaaaaaa", "state": "running", "preset": "demo"}))
        svc.ledger_path.unlink()
        svc2 = Service(svc.presets_path, svc.data, harness="mini")
        self.addCleanup(svc2.close)
        self.assertEqual([e["id"] for e in svc2.ledger()], [rid])  # unfinished runs are not seeded
        self.assertEqual(svc2.ledger()[0]["verify"], "verified")
        before = svc2.ledger_path.read_text()
        svc3 = Service(svc.presets_path, svc.data, harness="mini")  # an existing ledger is left alone
        self.addCleanup(svc3.close)
        self.assertEqual(svc3.ledger_path.read_text(), before)

    def test_tasks_endpoint_lists_definitions_cleaned_without_the_script(self):
        d = self.tmp()
        svc, url = self.start(Path(d))
        task_dir = Path(d) / "tasks" / "one"
        (task_dir / "instruction.md").write_text("Write ok to out.txt. See /home/someone/x and /Users/bob/y.\n")
        out = self.get(url + "/api/tasks")["tasks"]
        self.assertEqual(len(out), 1)
        t = out[0]
        self.assertEqual((t["name"], t["title"], t["presets"], t["allowed_changes"]), ("one", "Demo", ["demo"], ["out.txt"]))
        self.assertEqual(t["workspace"], [{"name": "seed.txt", "bytes": 5}])
        self.assertIn("out.txt", t["test"])
        self.assertIn("~/x", t["instruction"])
        raw = json.dumps(out)
        for bad in ("/home/", "/Users/", str(d), "script", '"op"', "swarm-test-"):
            self.assertNotIn(bad, raw)
        self.assertEqual(self.call(url + "/api/tasks")[0], 200)

    def test_new_endpoints_need_the_ingress_secret(self):
        d = self.tmp()
        _, url = self.start(Path(d), secret="s3cret")
        for path in ("/api/ledger", "/api/tasks"):
            self.assertEqual(self.call(url + path)[0], 403)
            self.assertEqual(self.call(url + path, headers={"X-Swarm-Ingress": "s3cret"})[0], 200)

    def finish(self, svc, preset="demo", size=3):
        code, out = svc.launch(preset, size, "c", meter=False)
        self.assertEqual(code, 202, out)
        for _ in range(600):
            if (svc.record(out["id"]) or {}).get("state") in ("done", "failed"):
                break
            time.sleep(0.1)
        return svc.record(out["id"])

    def test_result_carries_resource_fields_and_the_page_peak_rule(self):
        svc, _ = self.start(Path(self.tmp()))
        rec = self.finish(svc)
        r = rec["result"]
        for key in ("cpu_s_median", "disk_kb_median", "batch_wall_s_total", "agents_mem_mb_peak", "first_request_bytes_median",
                    "first_request_tools_bytes_median", "first_request_tool_count_median", "canary_sha256", "canary_distinct"):
            self.assertIsNotNone(r[key], key)
        self.assertIn("mem_per_agent_mb", r)
        self.assertEqual((rec["adapter_sha256"] and len(rec["adapter_sha256"]), len(rec["cohort"])), (12, 12))
        self.assertEqual((rec["canary"]["stable"], rec["canary"]["changed"], rec["canary"]["distinct"]), (True, False, 1))
        bundle = svc.run_dir(rec["id"]) / "bundle"
        rss = [round(a["agent"]["max_process_rss_kb"] / 1024, 1) for a in read_jsonl(bundle / "attempts.jsonl")]
        self.assertEqual(lane_peaks(svc.series(bundle), rss)[0], r["agents_mem_mb_peak"])
        # The snapshot's attempts carry the first request's sizes and canary, and no tool names or text.
        snap = svc.snapshot(rec["id"], 0)
        fr = snap["attempts"][0]["first_request"]
        self.assertEqual((fr["tool_count"], fr["canary_sha256"]), (3, rec["canary"]["sha256"]))
        self.assertNotIn("tools_offered", fr)

    @unittest.skipUnless(NODE, "node is needed to run the page's rules")
    def test_peaks_match_rank_js_on_fixtures(self):
        fixtures = [
            ([[0, 1, 10, 5, 900], [1, 3, 30, 50, 800], [2, 3, 36, 60, 700], [3, 2, 40, 10, 700]], [20, 31.5]),
            ([[0, 0, 5, 0, 900], [1, 2, None, 3, 900]], [12.5, None]),
            ([[0, 2, 8, 1, 1], [1, None, 9, 1, 1]], []),
            ([], [4.0]), ([], []), ([[0, 0, None, None, None]], [None]),
        ]
        for series, rss in fixtures:
            done = subprocess.run([NODE, "-e", PEAKS, str(FRONT)], input=json.dumps({"series": series, "rss": rss}),
                                  capture_output=True, text=True, timeout=60)
            self.assertEqual(done.returncode, 0, done.stderr)
            js = json.loads(done.stdout)
            mem, per = lane_peaks(series, rss)
            self.assertEqual(mem, js["mem"], (series, rss))
            self.assertEqual(per, js["per"], (series, rss))

    def test_swapping_the_harness_binary_writes_one_canary_line_and_moves_the_cohort(self):
        base = Path(self.tmp())
        svc, _ = self.start(base)
        wrapper = base / "py"
        wrapper.write_text(f'#!/bin/sh\nexec {sys.executable} "$@"\n')
        wrapper.chmod(wrapper.stat().st_mode | stat.S_IEXEC)
        a, b, c = self.finish(svc, size=1), None, None
        self.assertFalse((svc.data / "canary.jsonl").exists())
        svc.binaries["mini"] = str(wrapper)
        b = self.finish(svc, size=1)
        lines = (svc.data / "canary.jsonl").read_text().splitlines()
        self.assertEqual(len(lines), 1)
        self.assertTrue(b["canary"]["changed"] and b["canary"]["binary_changed"], b["canary"])
        self.assertEqual(b["canary"]["parts_changed"], [])
        self.assertIn("harness_changed", b["warnings"])
        self.assertNotEqual(a["cohort"], b["cohort"])
        c = self.finish(svc, size=1)  # the same binary again: no new change, same cohort as the second run
        self.assertFalse(c["canary"]["changed"])
        self.assertEqual((len((svc.data / "canary.jsonl").read_text().splitlines()), c["cohort"]), (1, b["cohort"]))
        self.assertEqual(list(svc.canary_status()), ["mini", "since"])
        self.assertEqual(svc.canary_status()["mini"], b["canary"]["sha256"][:12])

    def test_an_unstable_canary_has_no_verdict_and_leaves_the_store_alone(self):
        svc, _ = self.start(Path(self.tmp()))
        self.assertIsNone(canary_of([{"requests": {}}]))
        canary = canary_of([first("a"), first("b"), first("a")])
        self.assertEqual((canary["stable"], canary["distinct"], canary["sha256"]), (False, 2, None))
        out = svc._canary("r", {"harness": "mini"}, canary, {"sha256": "0" * 64, "version": "v"}, [])
        self.assertIsNone(out["changed"])
        self.assertFalse((svc.data / "canary.json").exists())

    def test_the_export_route_needs_the_ingress_secret_and_holds_no_private_text(self):
        from swarm import front, live
        svc, url = self.start(Path(self.tmp()), secret="s3cret")
        rec = self.finish(svc)
        path = f"{url}/api/runs/{rec['id']}/export"
        self.assertEqual(self.call(path)[0], 403)
        code, body = self.call(path, headers={"X-Swarm-Ingress": "s3cret"})
        self.assertEqual(code, 200)
        doc = json.loads(body)
        self.assertEqual((doc["id"], len(doc["rows"])), (rec["id"], 3))
        for bad in (front.HOST, front.IPV4, live.HOME, live.KEY, live.TEMP):
            self.assertIsNone(bad.search(body.decode()), bad.pattern)


if __name__ == "__main__":
    unittest.main()
