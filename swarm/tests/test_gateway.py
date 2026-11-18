"""swarm gateway: subscription adapters behind per-lane tokens, never leaking a lane token or the owner's secret."""
import json
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from swarm.gateway import Gateway, handler_for, token_hash
from swarm.relay import RelayModel

SEEN = []


class Provider(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        if self.path.endswith("/chat/completions") and body.get("stream"):
            chunks = [{"choices": [{"delta": {"content": "hi"}}]}, {"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 9,
                                                                                         "prompt_tokens_details": {"cached_tokens": 30}}}]
            payload = ("".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n").encode()
            ctype = "text/event-stream"
        elif self.path.endswith("/messages"):
            payload, ctype = json.dumps({"id": "m", "content": [], "usage": {"input_tokens": 11, "output_tokens": 4}}).encode(), "application/json"
        else:
            payload, ctype = json.dumps({"id": "r", "output": [], "usage": {"input_tokens": 20, "output_tokens": 2}}).encode(), "application/json"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class GatewayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.prov = ThreadingHTTPServer(("127.0.0.1", 0), Provider)
        cls.prov.daemon_threads = True
        threading.Thread(target=cls.prov.serve_forever, daemon=True).start()
        up = f"http://127.0.0.1:{cls.prov.server_port}"
        cls.tmp = tempfile.TemporaryDirectory()
        root = Path(cls.tmp.name)
        config = {"routes": {
            "claude-sub": {"upstream": up, "auth": "claude-oauth", "secret_env": "T_CLAUDE", "apis": ["messages", "count_tokens"], "models": ["claude-x"]},
            "oc-messages": {"upstream": up + "/anthropic", "auth": "anthropic-key", "secret_env": "T_OC", "apis": ["messages"], "models": ["qwen-x"]},
            "oc-chat": {"upstream": up + "/openai", "auth": "bearer", "secret_env": "T_OC", "apis": ["chat"], "models": ["glm-x"]},
            "oc-responses": {"upstream": up + "/openai", "auth": "bearer", "secret_env": "T_OC", "apis": ["responses"], "models": ["grok-x"]}},
            "clients": [{"name": "swarm-claude", "token_sha256": token_hash("lane-claude"), "routes": ["claude-sub", "oc-messages"]},
                        {"name": "swarm-pi", "token_sha256": token_hash("lane-pi"), "routes": ["oc-chat", "oc-responses"]},
                        {"name": "swarm-capped", "token_sha256": token_hash("lane-capped"), "routes": ["oc-responses"], "max_tokens_per_day": 60}]}
        (root / "gateway.json").write_text(json.dumps(config))
        cls.gw = Gateway(root / "gateway.json", root / "usage.jsonl", env={"T_CLAUDE": "sk-ant-oat01-owner", "T_OC": "oc-owner-key"})
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), handler_for(cls.gw))
        cls.srv.daemon_threads = True
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.base = f"http://127.0.0.1:{cls.srv.server_port}"

    @classmethod
    def tearDownClass(cls):
        for s in (cls.srv, cls.prov):
            s.shutdown()
            s.server_close()
        cls.tmp.cleanup()

    def setUp(self):
        SEEN.clear()

    def post(self, path, body, token, headers=None):
        req = urllib.request.Request(self.base + path, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", "Authorization": f"Bearer {token}", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.read()
            finally:
                e.close()

    def test_claude_subscription_uses_oauth_headers_and_never_the_lane_token(self):
        code, _ = self.post("/v1/messages", {"model": "claude-x", "max_tokens": 10}, "lane-claude",
                            headers={"anthropic-beta": "fine-grained-tool-streaming-2025-05-14", "anthropic-version": "2023-06-01"})
        self.assertEqual(code, 200)
        h = SEEN[0]["headers"]
        self.assertEqual(SEEN[0]["path"], "/v1/messages")
        self.assertEqual(h["authorization"], "Bearer sk-ant-oat01-owner")
        self.assertEqual(h["anthropic-beta"], "fine-grained-tool-streaming-2025-05-14,oauth-2025-04-20")
        self.assertNotIn("x-api-key", h)
        self.assertNotIn("lane-claude", json.dumps(h))

    def test_only_the_claude_code_lane_reaches_the_subscription(self):
        code, body = self.post("/v1/messages", {"model": "claude-x"}, "lane-pi")
        self.assertEqual(code, 403)
        self.assertEqual(SEEN, [])
        self.assertEqual(self.post("/v1/messages", {"model": "claude-x"}, "nobody")[0], 401)
        self.assertEqual(self.post("/v1/responses", {"model": "claude-x"}, "lane-claude")[0], 403)  # wrong API for the route

    def test_opencode_key_styles_and_chat_usage(self):
        self.assertEqual(self.post("/v1/messages", {"model": "qwen-x"}, "lane-claude")[0], 200)
        self.assertEqual(SEEN[-1]["path"], "/anthropic/v1/messages")
        self.assertEqual(SEEN[-1]["headers"]["x-api-key"], "oc-owner-key")
        code, body = self.post("/v1/chat/completions", {"model": "glm-x", "stream": True}, "lane-pi")
        self.assertEqual(code, 200)
        self.assertTrue(body.endswith(b"data: [DONE]\n\n"))
        self.assertEqual(SEEN[-1]["headers"]["authorization"], "Bearer oc-owner-key")
        rows = [json.loads(l) for l in (Path(self.tmp.name) / "usage.jsonl").read_text().splitlines()]
        chat = [r for r in rows if r["api"] == "chat"][-1]
        self.assertEqual((chat["client"], chat["input_tokens"], chat["cached_input_tokens"], chat["output_tokens"]), ("swarm-pi", 40, 30, 9))

    def test_daily_cap_and_model_list(self):
        with urllib.request.urlopen(urllib.request.Request(self.base + "/v1/models", headers={"Authorization": "Bearer lane-pi"}), timeout=5) as r:
            self.assertEqual([m["id"] for m in json.loads(r.read())["data"]], ["glm-x", "grok-x"])
        codes = [self.post("/v1/responses", {"model": "grok-x"}, "lane-capped")[0] for _ in range(4)]
        self.assertEqual(codes, [200, 200, 200, 429])  # 22 tokens a call, 60 a day for this client

    def test_relay_presents_its_lane_token_to_the_gateway(self):
        relay = RelayModel(self.base, max_run_tokens=1000, upstream_key="lane-claude")
        self.addCleanup(relay.close)
        url = relay.register("k1", None, "/tmp", "claude-x", "medium", 16384)
        req = urllib.request.Request(url.removesuffix("/v1") + "/v1/messages", method="POST",
                                     data=json.dumps({"model": "claude-x", "max_tokens": 16384, "output_config": {"effort": "medium"}}).encode(),
                                     headers={"Content-Type": "application/json", "x-api-key": "sk-ant-harness-dummy"})
        with urllib.request.urlopen(req, timeout=10) as r:
            self.assertEqual(r.status, 200)
        self.assertEqual(SEEN[-1]["headers"]["authorization"], "Bearer sk-ant-oat01-owner")
        rows, _ = relay.release("k1")
        self.assertEqual((rows[0]["input_tokens"], rows[0]["output_tokens"]), (11, 4))

    def test_source_vm_identity_when_behind_the_vm_to_vm_integration(self):
        root = Path(self.tmp.name)
        (root / "vm.json").write_text(json.dumps({"trust_source_vm": True, "routes": {
            "claude-sub": {"upstream": "https://api.anthropic.com", "auth": "claude-oauth", "secret_env": "T", "apis": ["messages"], "models": ["claude-x"]}},
            "clients": [{"name": "swarm-claude", "source_vm": "swarm-claude", "routes": ["claude-sub"]}]}))
        gw = Gateway(root / "vm.json", root / "vm.jsonl", env={"T": "s"})
        self.assertEqual(gw.client({"X-Exedev-Source-Vm": "swarm-claude"})["name"], "swarm-claude")
        self.assertIsNone(gw.client({"X-Exedev-Source-Vm": "swarm-pi"}))
        self.assertIsNone(gw.client({"Authorization": "Bearer anything"}))  # tokens mean nothing in this mode

    def test_a_route_without_its_secret_is_off_not_fatal(self):
        root = Path(self.tmp.name)
        (root / "half.json").write_text(json.dumps({"routes": {
            "a": {"upstream": "https://a.example", "auth": "bearer", "secret_env": "SET", "apis": ["chat"], "models": ["m1"]},
            "b": {"upstream": "https://b.example", "auth": "bearer", "secret_env": "UNSET", "apis": ["chat"], "models": ["m2"]}},
            "clients": [{"name": "c", "token_sha256": token_hash("t"), "routes": ["a", "b"]}]}))
        gw = Gateway(root / "half.json", root / "half.jsonl", env={"SET": "s"})
        self.assertEqual((sorted(gw.routes), gw.disabled), (["a"], ["b"]))
        self.assertEqual(gw.models(gw.client({"Authorization": "Bearer t"})), ["m1"])

    def test_integration_route_needs_no_secret_and_adds_no_auth(self):
        root = Path(self.tmp.name)
        (root / "int.json").write_text(json.dumps({"routes": {
            "free": {"upstream": "https://proxy.example/zen", "auth": "integration", "apis": ["chat"], "models": ["glm-free"]}},
            "clients": [{"name": "c", "token_sha256": token_hash("t"), "routes": ["free"]}]}))
        gw = Gateway(root / "int.json", root / "int.jsonl", env={})
        self.assertEqual((sorted(gw.routes), gw.disabled), (["free"], []))
        headers = Gateway.upstream_headers(gw.routes["free"], {"Authorization": "Bearer harness", "Content-Type": "application/json"})
        self.assertEqual(headers, {"content-type": "application/json"})

    def test_bad_config_is_refused(self):
        root = Path(self.tmp.name)
        (root / "bad.json").write_text(json.dumps({"routes": {"r": {"upstream": "http://evil", "auth": "bearer", "secret_env": "X", "apis": ["chat"], "models": ["m"]}}, "clients": []}))
        with self.assertRaises(ValueError):
            Gateway(root / "bad.json", root / "u.jsonl", env={"X": "s"})


if __name__ == "__main__":
    unittest.main()
