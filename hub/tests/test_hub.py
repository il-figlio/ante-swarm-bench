import json, os, sys, tempfile, threading, unittest, urllib.request, urllib.error
from pathlib import Path
from http.server import ThreadingHTTPServer
tmp = Path(tempfile.mkdtemp()); (tmp / "static").mkdir(); (tmp / "static" / "index.html").write_text("<title>t</title>"); (tmp / "runs").mkdir(); (tmp / "packs" / "toolbox").mkdir(parents=True); (tmp / "packs" / "toolbox" / "tasks.json").write_text("[]")
os.environ.update(SWARM_RUNS_DIR=str(tmp / "runs"), SWARM_STATIC_DIR=str(tmp / "static"), SWARM_PACKS_DIR=str(tmp / "packs"), SWARM_ALLOWED_EMAILS="owner@example.com", SWARM_RUNNER="/bin/true", SWARM_INGRESS_TOKEN="test-token", SWARM_ANNOTATIONS_DIR=str(tmp / "annotations"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "hub"))
import server as hub

class HubTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), hub.H); cls.port = cls.srv.server_address[1]
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
    @classmethod
    def tearDownClass(cls): cls.srv.shutdown(); cls.srv.server_close()
    def req(self, path, method="GET", headers=None, body=None):
        headers = dict(headers or {})
        if "X-ExeDev-Email" in headers: headers.setdefault("X-Swarm-Ingress", "test-token")  # production shape: proxy injects it
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}{path}", method=method, data=body, headers=headers)
        try:
            with urllib.request.urlopen(r) as resp: return resp.status, resp.read(), resp.headers
        except urllib.error.HTTPError as e:
            try: return e.code, e.read(), e.headers
            finally: e.close()
    def test_healthz_needs_no_identity(self): self.assertEqual(self.req("/swarm/healthz")[0], 200)
    def test_anonymous_redirects_to_login(self):
        opener = urllib.request.build_opener(urllib.request.HTTPRedirectHandler); 
        r = urllib.request.Request(f"http://127.0.0.1:{self.port}/swarm/", headers={"X-Swarm-Ingress": "test-token"})  # nginx injects the token on every proxied request, anonymous included
        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *a, **k): return None
        try: urllib.request.build_opener(NoRedirect).open(r); self.fail("expected 303")
        except urllib.error.HTTPError as e:
            try:
                self.assertEqual(e.code, 303); self.assertIn("/__exe.dev/login", e.headers["Location"])
            finally: e.close()
    def test_wrong_identity_forbidden(self): self.assertEqual(self.req("/swarm/api/runs", headers={"X-ExeDev-Email": "x@example.com"})[0], 403)
    def test_owner_lists_runs(self):
        code, body, _h = self.req("/swarm/api/runs", headers={"X-ExeDev-Email": "owner@example.com"}); self.assertEqual(code, 200); self.assertEqual(json.loads(body), [])
    def test_post_requires_action_header(self):
        self.assertEqual(self.req("/swarm/api/swarms", "POST", {"X-ExeDev-Email": "owner@example.com", "Content-Type": "application/json"}, b"{}")[0], 403)
    def test_bad_spec_rejected(self):
        code, body, _h = self.req("/swarm/api/swarms", "POST", {"X-ExeDev-Email": "owner@example.com", "X-Swarm-Action": "1", "Content-Type": "application/json"}, json.dumps({"mode": "vm", "cells": 99}).encode())
        self.assertEqual(code, 409); self.assertIn("vm mode allows", json.loads(body)["error"])
    def test_run_id_regex_and_liveness(self):
        d = tmp / "runs" / "r42"; d.mkdir(); (d / "meta.json").write_text(json.dumps({"pid": 999999})); (d / "log.jsonl").write_text("")
        self.assertFalse(hub.run_is_live(d))  # dead pid => not live => aborted, not stuck
        code, body, _h = self.req("/swarm/api/runs", headers={"X-ExeDev-Email": "owner@example.com"}); self.assertTrue(json.loads(body)[0]["aborted"])
        self.assertEqual(hub.next_run_id(), "r43")

    def test_ingress_token_blocks_local_forgery(self):
        # Loopback clients can forge X-ExeDev-Email; only nginx can supply the shared ingress secret.
        self.assertEqual(self.req("/swarm/api/runs", headers={"X-ExeDev-Email": "owner@example.com", "X-Swarm-Ingress": ""})[0], 403)
        code, _b, _h = self.req("/swarm/api/runs", headers={"X-ExeDev-Email": "owner@example.com", "X-Swarm-Ingress": "test-token"})
        self.assertEqual(code, 200)
        self.assertEqual(self.req("/swarm/api/runs", headers={"X-ExeDev-Email": "owner@example.com", "X-Swarm-Ingress": "wrong"})[0], 403)

    def test_authed_routes_fail_closed_without_token(self):
        saved = hub.INGRESS; hub.INGRESS = ""
        try:  # a hub started without the shared secret must not fall back to forgeable email auth
            self.assertEqual(self.req("/swarm/api/runs", headers={"X-ExeDev-Email": "owner@example.com"})[0], 403)
        finally: hub.INGRESS = saved

    def test_csp_has_no_script_unsafe_inline(self):
        _c, _b, hdrs = self.req("/swarm/api/runs", headers={"X-ExeDev-Email": "owner@example.com"})
        csp = hdrs.get("Content-Security-Policy", "")
        script = next(d for d in csp.split(";") if "script-src" in d)
        self.assertNotIn("unsafe-inline", script)

    def test_post_body_guards(self):
        h = {"X-ExeDev-Email": "owner@example.com", "X-Swarm-Action": "1", "Content-Type": "application/json"}
        code, _b, _h = self.req("/swarm/api/swarms", "POST", h, b"[1,2]")
        self.assertEqual(code, 409)  # non-dict body -> empty spec -> fails validation, no crash
        code, body, _h = self.req("/swarm/api/swarms", "POST", h, b"{bad json")
        self.assertEqual(code, 400)

    def test_custom_tasks_validation(self):
        # custom runs: prompts list required, check must name an existing pack checker
        t, e = hub.custom_tasks({}, "toolbox"); self.assertIsNone(t); self.assertIn("prompts", e)
        t, e = hub.custom_tasks({"prompts": ["  "]}, "toolbox"); self.assertIsNone(t)
        t, e = hub.custom_tasks({"prompts": ["x" * 4001]}, "toolbox"); self.assertIsNone(t); self.assertIn("4000", e)
        t, e = hub.custom_tasks({"prompts": ["do thing"]}, "toolbox")
        self.assertEqual(len(t), 1); self.assertEqual(t[0]["id"], "x01"); self.assertEqual(t[0]["prompt"], "do thing"); self.assertEqual(t[0]["check"], "")
        t, e = hub.custom_tasks({"prompts": ["a", "b"], "check": "tests.check_d01"}, "toolbox")
        self.assertIsNone(t); self.assertIn("check", e)  # toolbox has no check_d01
        t, e = hub.custom_tasks({"prompts": ["a"], "check": "os.system"}, "toolbox")
        self.assertIsNone(t)  # non-check_* module names are rejected: no arbitrary exec
        t, e = hub.custom_tasks({"prompts": ["a"], "budget_s": "lots"}, "toolbox")
        self.assertIsNone(t); self.assertIn("budget_s", e)

    def test_annotations_auth_and_storage(self):
        # Sink for Agentation feedback: same gates as other mutations (ingress + identity + action header).
        h = {"X-ExeDev-Email": "owner@example.com", "X-Swarm-Action": "1", "Content-Type": "application/json"}
        self.assertEqual(self.req("/swarm/api/annotations", "POST", {"X-ExeDev-Email": "owner@example.com", "X-Swarm-Ingress": "wrong", "X-Swarm-Action": "1"}, b"{}")[0], 403)
        self.assertEqual(self.req("/swarm/api/annotations", "POST", {"X-ExeDev-Email": "owner@example.com", "Content-Type": "application/json"}, b"{}")[0], 403)  # no X-Swarm-Action
        self.assertEqual(self.req("/swarm/api/annotations", "POST", h, b"[1,2]")[0], 400)  # must be a JSON object
        code, body, _h = self.req("/swarm/api/annotations", "POST", h, json.dumps({"event": "submit", "output": "## fix", "annotations": [{"comment": "x"}]}).encode())
        self.assertEqual(code, 201, body)
        name = json.loads(body)["stored"]; stored = json.loads((tmp / "annotations" / name).read_text())
        self.assertEqual(stored["payload"]["event"], "submit"); self.assertTrue(stored["received"])
        (tmp / "annotations" / name).unlink()
    def test_annotations_caps(self):
        h = {"X-ExeDev-Email": "owner@example.com", "X-Swarm-Action": "1", "Content-Type": "application/json"}
        code, _b, _h = self.req("/swarm/api/annotations", "POST", h, b'{"pad":"' + b"x" * 262144 + b'"}')
        self.assertEqual(code, 413)  # 256 KB ceiling, above the 64 KB cap of the other endpoints
        code, body, _h = self.req("/swarm/api/annotations", "POST", h, json.dumps({"event": "submit", "annotations": [{"i": i} for i in range(300)]}).encode())
        self.assertEqual(code, 201)
        name = json.loads(body)["stored"]; stored = json.loads((tmp / "annotations" / name).read_text())
        self.assertEqual(len(stored["payload"]["annotations"]), 256)
        (tmp / "annotations" / name).unlink()

    def test_custom_run_writes_tasks_file(self):
        (tmp / "packs" / "toolbox" / "tests").mkdir(exist_ok=True)
        (tmp / "packs" / "toolbox" / "tests" / "check_t01.py").write_text("import unittest")
        h = {"X-ExeDev-Email": "owner@example.com", "X-Swarm-Action": "1", "Content-Type": "application/json"}
        code, body, _h = self.req("/swarm/api/swarms", "POST", h, json.dumps({"mode": "local", "cells": 2, "pack": "toolbox", "custom": True, "check": "tests.check_t01", "prompts": ["fix a", "fix b"]}).encode())
        self.assertEqual(code, 201, body)
        rid = json.loads(body)["id"]
        gen = json.loads((tmp / "runs" / rid / "tasks.json").read_text())
        self.assertEqual([t["prompt"] for t in gen], ["fix a", "fix b"])
        self.assertEqual(gen[0]["check"], "tests.check_t01")
        pr = hub.ACTIVE.pop(rid)  # runner is /bin/true; reap it and clean the registry for later tests
        pr.wait()
        import shutil; shutil.rmtree(tmp / "runs" / rid)  # test_owner_lists_runs asserts the list is empty

    def test_public_runs_keeps_missing_samples_null_and_handles_non_numeric_dirs(self):
        import shutil
        fixture = Path(__file__).resolve().parent / "fixtures" / "mini-run"
        dst = tmp / "runs" / "mini-run"
        shutil.copytree(fixture, dst)
        try:
            code, body, _h = self.req("/swarm/public/runs")
            self.assertEqual(code, 200)
            row = next(r for r in json.loads(body)["runs"] if r["id"] == "mini-run")
            self.assertIsNone(row["peak_vm_used_kb"])
            self.assertIsNone(row["peak_load1"])
            self.assertIsNone(row["peak_vm_ante_rss_kb"])
            code2, body2, _h2 = self.req("/swarm/public/runs/mini-run")
            self.assertEqual(code2, 200)
            detail = json.loads(body2)
            self.assertEqual(len(detail["receipts"]["results"]), 2)
            self.assertEqual(detail["meta"]["pack"], "web-app")
        finally:
            shutil.rmtree(dst)

if __name__ == "__main__": unittest.main()
