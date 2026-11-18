"""swarm front: lanes stay private, payloads are scrubbed, races fan out, visitors are capped."""
import json
import os
import re
import secrets
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from swarm import front as front_mod
from swarm.front import Front, handler_for

PRESETS = [{"id": "fleet-fix", "title": "Fix a fleet", "sizes": [1, 8, 32]},
           {"id": "cold-start", "title": "Cold", "sizes": [1, 8], "kind": "harness"}]
# A fake lane's runs finish DONE_AFTER seconds after their launch, unless held running.
RUN_STATE = {"hold": False}
DONE_AFTER = 0.6
CONTROL = {"state": "done", "key": "c0ffee000001", "order": "control_first", "noisy": False,
           "median": {"first_request_s": 0.02, "step_gap_s": 0.01, "cpu_s": 0.1, "wall_s": 0.5}}


REFUSE: dict[str, tuple] = {}  # lane -> repetitions whose run the lane refuses (not quiet)


def fake_lane(name, seen):
    """A stand-in for `swarm serve`: requires the ingress secret, leaks its own host into payloads."""
    runs = {}

    class H(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, *a):
            pass

        def _json(self, code, body):
            b = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(b)))
            self.end_headers()
            self.wfile.write(b)

        def _ok(self):
            seen.append((name, self.command, self.path, self.headers.get("X-Swarm-Ingress"), self.headers.get("X-Real-IP")))
            return self.headers.get("X-Swarm-Ingress") == f"secret-{name}"

        def do_GET(self):
            if not self._ok():
                return self._json(403, {"error": "forbidden"})
            if self.path == "/api/status":
                return self._json(200, {"running": None, "queued": 0, "maintenance": None})
            if self.path == "/api/presets":
                return self._json(200, {"presets": PRESETS})
            if self.path.endswith("/events"):
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Connection", "close")
                self.end_headers()
                host = f"swarm-{name}.exe.xyz"
                for state in ("running", "done"):
                    snap = {"run": {"state": state, "harness": name}, "console": [
                        {"text": f"agent 1 ran on {host} at 10.0.0.7 in /home/exedev/x key sk-ant-abcdef123456"}]}
                    self.wfile.write(f"data: {json.dumps(snap)}\n\n".encode())
                    self.wfile.flush()
                    time.sleep(0.05)
                self.close_connection = True
                return
            m = re.match(r"^/api/runs/([0-9a-f]{12})(/export)?$", self.path)
            if m and m.group(1) in runs:
                launched, body = runs[m.group(1)]
                if m.group(2):
                    return self._json(200, {"run": {"id": m.group(1), "harness": name, "host": f"swarm-{name}.exe.xyz"}, "rows": []})
                done = not RUN_STATE["hold"] and time.time() - launched > DONE_AFTER
                series = body.get("series") or {}
                refuse = done and series.get("rep") in REFUSE.get(name, ())
                run = {"state": "failed" if refuse else "done" if done else "running", "harness": name, "series": series,
                       "control": dict(CONTROL, order=body.get("control_order")), "cohort": f"cohort-{name}",
                       "result": {"attempts": 1, "passes": 1, "first_request_s_median": 0.1 + 0.01 * series.get("rep", 1)}}
                if refuse:
                    run.update(refused="not_quiet", result=None, error="This lane was not quiet enough to measure: other CPU busy (0.6 cores).")
                return self._json(200, {"run": run})
            if "/agents/" in self.path:
                return self._json(200, {"reply": f"done on swarm-{name}.exe.xyz"})
            return self._json(404, {})

        def do_POST(self):
            if not self._ok():
                return self._json(403, {"error": "forbidden"})
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            seen.append((name, "BODY", json.dumps(body), None, None))
            rid = secrets.token_hex(6)
            runs[rid] = (time.time(), body)
            return self._json(202, {"id": rid, "position": 0})

    srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
    srv.daemon_threads = True
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv


class FrontTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.seen = []
        cls.lanes = {n: fake_lane(n, cls.seen) for n in ("ante", "codex")}
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        lanes = {"lanes": [{"id": n, "label": n.title(), "harness": n, "url": f"http://127.0.0.1:{s.server_port}",
                            "secret_env": f"T_SECRET_{n.upper()}"} for n, s in cls.lanes.items()]}
        for n in cls.lanes:
            os.environ[f"T_SECRET_{n.upper()}"] = f"secret-{n}"
        (root / "lanes.json").write_text(json.dumps(lanes))
        (root / "static").mkdir()
        (root / "static" / "index.html").write_text("<!doctype html><title>t</title>")
        (root / "secret.txt").write_text("nope")
        cls.front = Front(root / "lanes.json", root / "data", per_client_hour=2)
        cls.front.poll_s = 0.1
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(cls.front, root / "static"))
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"
        cls.lane_ports = [str(s.server_port) for s in cls.lanes.values()]

    @classmethod
    def tearDownClass(cls):
        for s in [cls.srv, *cls.lanes.values()]:
            s.shutdown()
            s.server_close()
        cls.tmp.cleanup()

    def get(self, path):
        with urllib.request.urlopen(self.base + path, timeout=10) as r:
            return r.status, r.read().decode()

    def post(self, path, body, ip="198.51.100.1"):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "X-Forwarded-For": ip})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            try:
                return e.code, json.loads(e.read())
            finally:
                e.close()

    def assertPrivate(self, text):
        for port in self.lane_ports:
            self.assertNotIn(port, text)
        for leak in ("exe.xyz", "10.0.0.7", "/home/exedev", "sk-ant-", "secret-"):
            self.assertNotIn(leak, text)

    def test_lanes_hide_addresses(self):
        code, text = self.get("/api/lanes")
        self.assertEqual(code, 200)
        lanes = json.loads(text)["lanes"]
        self.assertEqual([l["id"] for l in lanes], ["ante", "codex"])
        self.assertTrue(all(l["state"] == "idle" for l in lanes))
        self.assertEqual(set(lanes[0]), {"id", "label", "harness", "state"})
        self.assertPrivate(text)

    def settle(self, rid, timeout=20):
        deadline = time.time() + timeout
        while time.time() < deadline:
            race = self.front.races[rid]
            if race["state"] != "running":
                return race
            time.sleep(0.05)
        self.fail("race did not settle")

    def test_the_front_launches_no_baseline_answers_at_once_and_relays_the_lane_control(self):
        self.front.races.clear()
        self.seen.clear()
        t = time.monotonic()
        code, race = self.front.start_race("cold-start", 1, "198.51.100.80")
        self.assertLess(time.monotonic() - t, 1.0)
        self.assertEqual((code, race["state"], race["repeats"]), (202, "running", 1))
        self.assertTrue(all(v["started"] is False and v["state"] == "queued" for v in race["lanes"].values()))
        done = self.settle(race["id"])
        self.assertEqual(done["state"], "done")
        bodies = [json.loads(b) for n, kind, b, *_ in self.seen if kind == "BODY"]
        self.assertFalse([b for b in bodies if b.get("preset") == "lane-baseline"])
        self.assertTrue(all(b["series"] == {"race": race["id"], "rep": 1, "of": 1} and b["control_order"] in ("control_first", "control_last")
                            for b in bodies))
        public = json.loads(self.get(f"/api/races/{race['id']}")[1])
        for lane in public["lanes"].values():
            self.assertEqual((lane["state"], lane["control"]["key"], lane["cohort"][:7]), ("done", "c0ffee000001", "cohort-"))
            self.assertNotIn("baseline_s", lane)
        self.assertEqual(public["reps"][0]["lanes"]["ante"]["result"]["passes"], 1)
        archived = self.front.export_dir(race["id"]) / "rep-1" / "ante.json"
        self.assertTrue(archived.exists())
        self.assertPrivate(archived.read_text())
        stats = json.loads(self.get(f"/api/races/{race['id']}/stats")[1])
        self.assertEqual((stats["n"], stats["rule"]), (1, "gap10"))
        self.assertTrue((self.front.export_dir(race["id"]) / "stats.json").exists())
        # A race recorded before the lane control keeps its baseline.
        old = {"id": "0123456789ab", "preset": "cold-start", "size": 1, "created": "x", "state": "done", "t": 0,
               "lanes": {"ante": {"run": "abcdef012345", "error": None, "baseline_s": 0.04}}}
        self.assertEqual(self.front.public_race(old)["lanes"]["ante"]["baseline_s"], 0.04)

    def test_a_series_runs_every_repetition_on_every_lane_and_keeps_a_refusal(self):
        self.front.races.clear()
        self.seen.clear()
        REFUSE["codex"] = (3,)
        self.front.passkey = "owner-key"
        try:
            code, race = self.front.start_race("cold-start", 1, "198.51.100.90", repeats=6)
            self.assertEqual(code, 202, race)
            self.assertEqual(self.front.start_race("cold-start", 1, "198.51.100.91")[0], 409)  # the lanes are the series'
            done = self.settle(race["id"], timeout=60)
        finally:
            REFUSE.clear()
            self.front.passkey = None
        self.assertEqual((done["state"], len(done["reps"])), ("done", 6))
        for rep in done["reps"]:
            self.assertIn(rep["order"], ("control_first", "control_last"))
            for lid, v in rep["lanes"].items():
                self.assertTrue(v["run"])
                if lid == "codex" and rep["k"] == 3:
                    self.assertEqual((v["state"], v["refused"]), ("failed", "not_quiet"))
                    self.assertIn("not quiet", v["error"])
                else:
                    self.assertEqual((v["state"], v["error"]), ("done", None))
        bodies = [json.loads(b) for n, kind, b, *_ in self.seen if kind == "BODY"]
        self.assertEqual(sorted(b["series"]["rep"] for b in bodies if b["harness"] == "ante"), [1, 2, 3, 4, 5, 6])
        self.assertTrue(all(b.get("meter") is False for b in bodies if b["series"]["rep"] > 1))
        self.assertTrue(all("meter" not in b for b in bodies if b["series"]["rep"] == 1))
        # Orders come from the race id, so the same race always gets the same orders.
        rng = __import__("random").Random(race["id"])
        self.assertEqual([r["order"] for r in done["reps"]], [rng.choice(front_mod.ORDERS) for _ in range(6)])
        stats = json.loads(self.get(f"/api/races/{race['id']}/stats")[1])
        self.assertEqual(stats["n"], 6)
        pair = stats["metrics"]["start"]["pairs"][0]
        self.assertEqual(pair["n"], 5)  # codex's refused repetition 3 drops that pair
        saved = (self.front.export_dir(race["id"]) / "stats.json").read_bytes()
        self.assertEqual(json.dumps(front_mod.race_stats_mod.race_stats(json.loads(json.dumps(done))), sort_keys=True,
                                    separators=(",", ":")).encode(), saved)
        # races.jsonl: the last line for the race holds all six repetitions with run ids, orders and results
        last = [json.loads(l) for l in (self.front.data / "races.jsonl").read_text().splitlines() if race["id"] in l][-1]
        self.assertEqual([len(r["lanes"]) for r in last["reps"]], [2] * 6)
        for k in (1, 6):
            self.assertTrue((self.front.export_dir(race["id"]) / f"rep-{k}" / "codex.json").exists())
        code, text = self.get(f"/api/races/{race['id']}/lanes/ante/agents/1?rep=2")
        self.assertEqual(code, 200)
        self.assertPrivate(json.dumps(stats) + text + self.get(f"/api/races/{race['id']}")[1])

    def test_repetitions_visitors_may_ask_for(self):
        self.front.races.clear()
        self.assertEqual(self.post("/api/races", {"preset": "cold-start", "size": 1, "repeats": 10}, ip="192.0.2.200")[0], 403)
        self.assertEqual(self.post("/api/races", {"preset": "fleet-fix", "size": 1, "repeats": 6}, ip="192.0.2.201")[0], 403)
        self.assertEqual(self.post("/api/races", {"preset": "cold-start", "size": 8, "repeats": 6}, ip="192.0.2.202")[0], 403)
        self.assertEqual(self.post("/api/races", {"preset": "cold-start", "size": 1, "repeats": 3}, ip="192.0.2.203")[0], 400)
        code, race = self.post("/api/races", {"preset": "cold-start", "size": 1, "repeats": 6}, ip="192.0.2.204")
        self.assertEqual((code, race["repeats"]), (202, 6))
        self.front.races[race["id"]]["state"] = "done"  # stops its driver

    def test_a_restarted_front_marks_a_running_series_interrupted_and_launches_nothing(self):
        root = Path(self.tmp.name) / "restart"
        root.mkdir()
        race = {"id": "00000000abcd", "preset": "cold-start", "size": 1, "created": "x", "t": time.time(), "state": "running",
                "repeats": 6, "rep": 2, "reps": [{"k": 1, "lanes": {"ante": {"run": "abcdef012345", "state": "done"}}},
                                                 {"k": 2, "lanes": {"ante": {"run": "abcdef012346", "state": "queued"}}}],
                "lanes": {"ante": {"run": "abcdef012346", "state": "queued"}}}
        (root / "races.jsonl").write_text(json.dumps(race) + "\n")
        self.seen.clear()
        front = Front(Path(self.tmp.name) / "lanes.json", root)
        self.assertEqual(front.races[race["id"]]["state"], "interrupted")
        time.sleep(0.3)
        self.assertFalse([x for x in self.seen if x[1] in ("POST", "BODY")])
        self.assertEqual(front.public_race(front.races[race["id"]])["state"], "interrupted")
        self.assertEqual(json.loads((root / "races.jsonl").read_text().splitlines()[-1])["state"], "interrupted")

    def test_race_fans_out_streams_and_scrubs(self):
        self.front.races.clear()
        code, race = self.post("/api/races", {"preset": "fleet-fix", "size": 8})
        self.assertEqual(code, 202, race)
        self.assertEqual(set(race["lanes"]), {"ante", "codex"})
        time.sleep(0.3)
        self.assertTrue(all(v["started"] for v in self.front.public_race(self.front.races[race["id"]])["lanes"].values()))
        bodies = [{k: v for k, v in json.loads(b).items() if k not in ("series", "control_order")} for n, kind, b, *_ in self.seen if kind == "BODY"]
        self.assertIn({"preset": "fleet-fix", "size": 8, "harness": "codex"}, bodies)
        self.assertTrue(all(sec == f"secret-{n}" for n, _, _, sec, _ in self.seen if sec is not None))
        code, text = self.get(f"/api/races/{race['id']}/events")
        self.assertEqual(code, 200)
        msgs = [json.loads(l[6:]) for l in text.splitlines() if l.startswith("data: ")]
        self.assertEqual({m["lane"] for m in msgs if "snap" in m}, {"ante", "codex"})
        self.assertIn({"rep": 1, "start": True}, msgs)
        self.assertTrue(all(m["rep"] == 1 for m in msgs if "lane" in m))
        self.assertTrue(msgs[-1].get("end"))
        self.assertEqual(msgs[-1]["race"]["state"], "done")
        self.assertIn("[host]", text)
        self.assertPrivate(text)
        code, text = self.get(f"/api/races/{race['id']}/lanes/ante/agents/1")
        self.assertEqual(code, 200)
        self.assertPrivate(text)

    def test_rejects_bad_input_and_caps_visitors(self):
        self.front.races.clear()
        self.assertEqual(self.post("/api/races", {"preset": "nope", "size": 8}, ip="203.0.113.9")[0], 400)
        self.assertEqual(self.post("/api/races", {"preset": "fleet-fix", "size": 9}, ip="203.0.113.9")[0], 400)
        self.assertEqual(self.post("/api/races", {"preset": "fleet-fix", "size": "8"}, ip="203.0.113.9")[0], 400)
        RUN_STATE["hold"] = True
        self.addCleanup(RUN_STATE.update, hold=False)
        ok = self.post("/api/races", {"preset": "fleet-fix", "size": 1}, ip="203.0.113.9")
        self.assertEqual(ok[0], 202)
        self.assertEqual(self.post("/api/races", {"preset": "fleet-fix", "size": 1}, ip="203.0.113.10")[0], 409)
        for r in self.front.races.values():
            r["state"] = "done"
        self.assertEqual(self.post("/api/races", {"preset": "fleet-fix", "size": 1}, ip="203.0.113.9")[0], 202)
        for r in self.front.races.values():
            r["state"] = "done"
        self.assertEqual(self.post("/api/races", {"preset": "fleet-fix", "size": 1}, ip="203.0.113.9")[0], 429)

    def test_lanes_can_be_picked_and_disjoint_races_run_side_by_side(self):
        self.front.races.clear()
        self.seen.clear()
        for bad in (["nope"], [], ["ante", "ante"], "ante", [1]):
            self.assertEqual(self.post("/api/races", {"preset": "fleet-fix", "size": 1, "lanes": bad}, ip="192.0.2.70")[0], 400, bad)
        RUN_STATE["hold"] = True
        self.addCleanup(RUN_STATE.update, hold=False)
        code, solo = self.post("/api/races", {"preset": "fleet-fix", "size": 1, "lanes": ["ante"]}, ip="192.0.2.71")
        self.assertEqual(code, 202, solo)
        self.assertEqual(set(solo["lanes"]), {"ante"})
        time.sleep(0.3)
        self.assertEqual({n for n, kind, b, *_ in self.seen if kind == "BODY" and json.loads(b)["series"]["race"] == solo["id"]}, {"ante"})
        # ante is busy; codex is free, so a race on codex alone starts beside it, and one wanting ante waits.
        self.assertEqual(self.post("/api/races", {"preset": "fleet-fix", "size": 1, "lanes": ["codex", "ante"]}, ip="192.0.2.72")[0], 409)
        code, other = self.post("/api/races", {"preset": "fleet-fix", "size": 1, "lanes": ["codex"]}, ip="192.0.2.73")
        self.assertEqual(code, 202, other)
        self.assertEqual(set(other["lanes"]), {"codex"})

    def test_lane_size_cap_is_public_and_advisory(self):
        self.front.races.clear()
        lane = self.front.lane("codex")
        lane.max_size = 1
        try:
            lanes = json.loads(self.get("/api/lanes")[1])["lanes"]
            self.assertEqual({l["id"]: l.get("max_size") for l in lanes}, {"ante": None, "codex": 1})
            # Over a lane's known size the race still runs (to be seen failing); the race records who was over.
            code, body = self.post("/api/races", {"preset": "fleet-fix", "size": 8, "lanes": ["ante", "codex"]}, ip="192.0.2.80")
            self.assertEqual(code, 202, body)
            self.assertEqual(self.front.races[body["race"]["id"] if "race" in body else body["id"]]["over"], ["codex"])
        finally:
            lane.max_size = None

    def test_real_models_need_the_owner_passkey_and_a_lane_that_offers_them(self):
        self.front.races.clear()
        self.seen.clear()
        ante, codex = self.front.lane("ante"), self.front.lane("codex")
        self.front.passkey, ante.models = "owner-key", ["gpt-x"]
        try:
            body = {"preset": "fleet-fix", "size": 1, "lanes": ["ante"], "model": "gpt-x"}
            self.assertEqual(self.post("/api/races", body, ip="192.0.2.90")[0], 403)
            self.assertEqual(self.post("/api/races", body | {"passkey": "wrong"}, ip="192.0.2.90")[0], 403)
            code, out = self.post("/api/races", body | {"lanes": ["ante", "codex"], "passkey": "owner-key"}, ip="192.0.2.90")
            self.assertEqual(code, 400)
            self.assertIn("Codex cannot run gpt-x", out["error"])
            code, race = self.post("/api/races", body | {"passkey": "owner-key"}, ip="192.0.2.91")
            self.assertEqual(code, 202, race)
            self.assertEqual(race["model"], "gpt-x")
            time.sleep(0.3)
            sent = [{k: v for k, v in json.loads(b).items() if k not in ("series", "control_order")} for n, kind, b, *_ in self.seen
                    if kind == "BODY" and json.loads(b)["series"]["race"] == race["id"]]
            self.assertEqual(sent, [{"preset": "fleet-fix", "size": 1, "model": "gpt-x", "harness": "ante"}])
            self.front.status_cache = (0.0, [])  # lane rows are cached for 2 s
            lanes = {l["id"]: l for l in json.loads(self.get("/api/lanes")[1])["lanes"]}
            self.assertEqual(lanes["ante"].get("models"), ["gpt-x"])
            self.assertNotIn("passkey", self.get("/api/lanes")[1])
        finally:
            self.front.passkey, ante.models, codex.models = None, [], []
            front_mod.write_lock(self.front.data, 0, False)

    def test_three_wrong_passkeys_lock_real_models_silently_until_unlocked_on_the_vm(self):
        self.front.races.clear()
        ante = self.front.lane("ante")
        self.front.passkey, ante.models = "owner-key", ["gpt-x"]
        front_mod.write_lock(self.front.data, 0, False)
        try:
            body = {"preset": "fleet-fix", "size": 1, "lanes": ["ante"], "model": "gpt-x"}
            refusals = set()
            for i in range(2):  # the first try and one retry, from different visitors
                code, out = self.post("/api/races", body | {"passkey": f"guess{i}"}, ip=f"192.0.2.{120 + i}")
                refusals.add((code, json.dumps(out)))
            self.assertEqual(front_mod.lock_state(self.front.data), (2, False))
            code, out = self.post("/api/races", body | {"passkey": "guess2"}, ip="192.0.2.122")  # the second retry
            refusals.add((code, json.dumps(out)))
            self.assertEqual(front_mod.lock_state(self.front.data), (3, True))
            # Locked: even the right passkey gets the very same reply as a wrong one, and nothing public says why.
            code, out = self.post("/api/races", body | {"passkey": "owner-key"}, ip="192.0.2.123")
            refusals.add((code, json.dumps(out)))
            self.assertEqual(len(refusals), 1)
            self.assertEqual(refusals.pop()[0], 403)
            for path in ("/api/lanes", "/api/presets", "/api/races"):
                text = self.get(path)[1]
                for word in ("lock", "owner-key", "failures", "PIN"):
                    self.assertNotIn(word, text)
            # Unlocked on the VM: the owner launches, and the count is clear.
            front_mod.write_lock(self.front.data, 0, False)
            self.assertEqual(self.post("/api/races", body | {"passkey": "owner-key"}, ip="192.0.2.124")[0], 202)
            # A scripted launch never looks at the passkey: no count, and no extra launches for a right one, so the
            # hourly cap cannot test a guess.
            scripted = {"preset": "fleet-fix", "size": 1, "lanes": ["codex"]}
            replies = []
            for _ in range(self.front.per_client_hour + 1):
                self.front.races.clear()  # one lane, one race at a time: clear so only the hourly cap answers
                replies.append(self.post("/api/races", scripted | {"passkey": "owner-key"}, ip="192.0.2.126")[0])
            self.assertEqual(replies[-1], 429)
            self.assertEqual(front_mod.lock_state(self.front.data), (0, False))
        finally:
            self.front.passkey, ante.models = None, []
            front_mod.write_lock(self.front.data, 0, False)

    def test_race_settles_without_a_viewer(self):
        self.front.races.clear()
        RUN_STATE["hold"] = True
        try:
            code, race = self.post("/api/races", {"preset": "fleet-fix", "size": 8}, ip="192.0.2.50")
            self.assertEqual(code, 202)
            time.sleep(DONE_AFTER + 0.3)
            self.assertEqual(json.loads(self.get(f"/api/races/{race['id']}")[1])["state"], "running")
        finally:
            RUN_STATE["hold"] = False
        self.settle(race["id"])
        self.assertEqual(json.loads(self.get(f"/api/races/{race['id']}")[1])["state"], "done")

    def test_static_is_confined(self):
        self.assertEqual(self.get("/")[0], 200)
        for path in ("/../secret.txt", "/%2e%2e/secret.txt", "/lanes.json", "/api/nope"):
            try:
                code = self.get(path)[0]
            except urllib.error.HTTPError as e:
                code = e.code
                e.close()
            self.assertEqual(code, 404, path)

    def test_a_lane_that_is_not_clean_is_shown_as_such_and_its_refusal_is_explained(self):
        """A lane whose post-run reset failed takes no race; the page must say why, not show a bare 503."""
        class Dirty(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _json(self, code, body):
                b = json.dumps(body).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(b)))
                self.end_headers()
                self.wfile.write(b)

            def do_GET(self):
                if self.path == "/api/presets":
                    return self._json(200, {"presets": PRESETS})
                return self._json(200, {"running": None, "queued": 0, "maintenance": None, "lane": "reset_failed"})

            def do_POST(self):
                self.rfile.read(int(self.headers["Content-Length"]))
                return self._json(503, {"error": "lane is not clean", "lane": "reset_failed", "problems": ["disk_low"]})

        srv = ThreadingHTTPServer(("127.0.0.1", 0), Dirty)
        srv.daemon_threads = True
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        self.addCleanup(srv.server_close)
        self.addCleanup(srv.shutdown)
        root = Path(self.tmp.name)
        (root / "dirty.json").write_text(json.dumps({"lanes": [{"id": "hermes", "harness": "hermes", "url": f"http://127.0.0.1:{srv.server_port}"}]}))
        front = Front(root / "dirty.json", root / "d-dirty", per_client_hour=5)
        self.assertEqual(front.lane_status()[0]["state"], "unclean")
        code, out = front.start_race("fleet-fix", 8, "198.51.100.9")
        self.assertEqual(code, 202)  # answered at once; the driver finds out
        front.poll_s = 0.05
        for _ in range(100):
            if front.races[out["id"]]["state"] != "running":
                break
            time.sleep(0.05)
        self.assertEqual(front.races[out["id"]]["state"], "failed")
        self.assertEqual(front.races[out["id"]]["lanes"]["hermes"]["error"], "not clean after its last run")
        self.assertEqual(front.public_race(front.races[out["id"]])["lanes"]["hermes"]["error"], "not clean after its last run")

    def test_missing_secret_refuses_to_start(self):
        root = Path(self.tmp.name)
        (root / "bad.json").write_text(json.dumps({"lanes": [{"id": "x", "harness": "x", "url": "http://h", "secret_env": "T_UNSET_XYZ"}]}))
        with self.assertRaises(ValueError):
            Front(root / "bad.json", root / "d2")


if __name__ == "__main__":
    unittest.main()
