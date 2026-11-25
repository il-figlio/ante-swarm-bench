"""Relay model: real-model requests pass through Swarm's checks, carry no harness credentials, and are metered."""
import json
import threading
import unittest
import urllib.error
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from swarm.relay import RelayModel, SSEUsage, usage_of

SEEN = []


def responses_sse(text="hi"):
    events = [{"type": "response.created", "response": {"id": "r1"}},
              {"type": "response.output_text.delta", "delta": text},
              {"type": "response.completed", "response": {"id": "r1", "usage": {
                  "input_tokens": 120, "output_tokens": 30, "input_tokens_details": {"cached_tokens": 100}}}}]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


def messages_sse(written=None):
    usage = {"input_tokens": 10, "cache_read_input_tokens": 90, "output_tokens": 1}
    if written is not None:
        usage["cache_creation_input_tokens"] = written
    events = [{"type": "message_start", "message": {"id": "msg_up1", "usage": usage}},
              {"type": "content_block_delta", "delta": {"type": "text_delta", "text": "ok"}},
              {"type": "message_delta", "usage": {"output_tokens": 25}},
              {"type": "message_stop"}]
    return "".join(f"event: {e['type']}\ndata: {json.dumps(e)}\n\n" for e in events).encode()


class Upstream(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_POST(self):
        body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
        SEEN.append({"path": self.path, "headers": {k.lower(): v for k, v in self.headers.items()}, "body": body})
        if body.get("model") == "boom":
            payload, ctype, code = json.dumps({"error": {"message": "nope"}}).encode(), "application/json", 400
        elif body.get("model") == "trunc-sse":
            ev = {"type": "response.created", "response": {"id": "r_trunc", "model": "trunc-sse"}}
            payload, ctype, code = f"event: response.created\ndata: {json.dumps(ev)}\n\n".encode(), "text/event-stream", 200
        elif body.get("model") == "wrong-model":
            ev = {"type": "response.completed", "response": {"id": "r_wm", "model": "other-model", "usage": {"input_tokens": 10, "output_tokens": 2}}}
            payload, ctype, code = f"event: response.completed\ndata: {json.dumps(ev)}\n\n".encode(), "text/event-stream", 200
        elif self.path == "/v1/messages/count_tokens":
            payload, ctype, code = json.dumps({"input_tokens": 7}).encode(), "application/json", 200
        elif self.path == "/v1/chat/completions" and body.get("stream"):
            chunks = [{"choices": [{"index": 0, "delta": {"content": "ok"}}]}]
            if (body.get("stream_options") or {}).get("include_usage"):
                chunks.append({"choices": [], "usage": {"prompt_tokens": 40, "completion_tokens": 4,
                                                        "prompt_tokens_details": {"cached_tokens": 8}}})
            payload = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks).encode() + b"data: [DONE]\n\n"
            ctype, code = "text/event-stream", 200
        elif self.path == "/v1/chat/completions":
            payload, ctype, code = json.dumps({"choices": [], "usage": {"prompt_tokens": 9, "completion_tokens": 1}}).encode(), "application/json", 200
        elif body.get("model") == "claude-cw":
            payload, ctype, code = messages_sse(written=40), "text/event-stream", 200
        elif body.get("stream"):
            payload, ctype, code = (messages_sse() if self.path == "/v1/messages" else responses_sse()), "text/event-stream", 200
        else:
            payload, ctype, code = json.dumps({"id": "r", "output": [], "usage": {"input_tokens": 50, "output_tokens": 5}}).encode(), "application/json", 200
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)


class RelayTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.up = ThreadingHTTPServer(("127.0.0.1", 0), Upstream)
        cls.up.daemon_threads = True
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        cls.upstream = f"http://127.0.0.1:{cls.up.server_port}"

    @classmethod
    def tearDownClass(cls):
        cls.up.shutdown()
        cls.up.server_close()

    def setUp(self):
        SEEN.clear()
        self.relay = RelayModel(self.upstream, max_run_tokens=1000)
        self.addCleanup(self.relay.close)

    def post(self, url, body, headers=None):
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                     headers={"Content-Type": "application/json", **(headers or {})})
        try:
            with urllib.request.urlopen(req, timeout=10) as r:
                return r.status, r.headers.get("Content-Type"), r.read()
        except urllib.error.HTTPError as e:
            try:
                return e.code, e.headers.get("Content-Type"), e.read()
            finally:
                e.close()

    def register(self, key="a1", model="gpt-x", cap=16384):
        return self.relay.register(key, None, "/tmp", model, "medium", cap, max_requests=10)

    def test_streamed_responses_pass_through_metered_and_without_credentials(self):
        url = self.register()
        code, ctype, body = self.post(url + "/responses", {"model": "gpt-x", "reasoning": {"effort": "medium"}, "stream": True, "input": "hi"},
                                      headers={"Authorization": "Bearer sk-harness-secret", "X-Api-Key": "k", "Cookie": "c=1"})
        self.assertEqual((code, ctype), (200, "text/event-stream"))
        self.assertEqual(body, responses_sse())
        self.assertEqual(SEEN[0]["path"], "/v1/responses")
        for leaked in ("authorization", "x-api-key", "cookie"):
            self.assertNotIn(leaked, SEEN[0]["headers"])
        rows, _ = self.relay.release("a1")
        self.assertEqual((rows[0]["status"], rows[0]["input_tokens"], rows[0]["cached_input_tokens"], rows[0]["output_tokens"]), (200, 120, 100, 30))
        self.assertEqual(self.relay.budget.used, 150)

    def test_chat_completions_metered_with_usage_requested(self):
        url = self.register()
        code, ctype, _ = self.post(url + "/chat/completions", {"model": "gpt-x", "reasoning_effort": "medium", "stream": True,
                                                               "max_completion_tokens": 16384, "messages": []})
        self.assertEqual((code, ctype), (200, "text/event-stream"))
        self.assertEqual(SEEN[0]["path"], "/v1/chat/completions")
        self.assertEqual(SEEN[0]["body"]["stream_options"], {"include_usage": True})
        code, _, _ = self.post(url + "/chat/completions", {"model": "gpt-x", "reasoning_effort": "low", "messages": []})
        self.assertEqual(code, 422)  # effort checked on the chat wire too
        code, _, _ = self.post(url + "/chat/completions", {"model": "gpt-x", "reasoning_effort": "medium", "messages": []})
        self.assertEqual(code, 200)
        rows, _ = self.relay.release("a1")
        self.assertEqual((rows[0]["api"], rows[0]["usage_requested"], rows[0]["input_tokens"], rows[0]["cached_input_tokens"],
                          rows[0]["output_tokens"]), ("chat", True, 40, 8, 4))
        self.assertEqual(rows[2]["input_tokens"], 9)
        self.assertEqual(len(SEEN), 2)

    def test_upstream_connection_is_kept_alive_across_requests(self):
        url = self.register()
        for _ in range(3):
            code, _, _ = self.post(url + "/responses", {"model": "gpt-x", "reasoning": {"effort": "medium"}, "stream": True, "input": "hi"})
            self.assertEqual(code, 200)
        self.assertEqual(self.relay.pool.opened, 1)

    def test_anthropic_messages_streamed_and_count_tokens(self):
        url = self.register(model="claude-x")
        base = url.removesuffix("/v1")
        code, _, body = self.post(base + "/v1/messages", {"model": "claude-x", "max_tokens": 16384, "stream": True,
                                                           "output_config": {"effort": "medium"}, "messages": []},
                                  headers={"anthropic-version": "2023-06-01"})
        self.assertEqual(code, 200)
        self.assertEqual(body, messages_sse())
        self.assertEqual(SEEN[0]["headers"].get("anthropic-version"), "2023-06-01")
        code, _, body = self.post(base + "/v1/messages/count_tokens", {"model": "claude-x", "messages": []})
        self.assertEqual((code, json.loads(body)), (200, {"input_tokens": 7}))
        rows, _ = self.relay.release("a1")
        self.assertEqual((rows[0]["input_tokens"], rows[0]["output_tokens"]), (100, 25))
        self.assertEqual(rows[1]["kind"], "count_tokens")
        self.assertEqual(self.relay.budget.used, 125)  # count_tokens is not spend

    def test_helper_model_is_admitted_for_sub_agents_only(self):
        url = self.relay.register("h1", None, "/tmp", "claude-x", "medium", 16384, max_requests=10, helper="claude-small")
        base = url.removesuffix("/v1")
        msg = lambda model, **kw: self.post(base + "/v1/messages", {"model": model, "max_tokens": 16384, "messages": [], **kw})[0]
        self.assertEqual(msg("claude-small"), 200)  # effort left out: the harness decides per model
        self.assertEqual(msg("claude-small", output_config={"effort": "medium"}), 200)
        self.assertEqual(msg("claude-small", output_config={"effort": "high"}), 422)  # may not raise it
        self.assertEqual(msg("claude-other", output_config={"effort": "medium"}), 422)
        self.assertEqual(msg("claude-x"), 422)  # the planned model still must send its effort
        models = json.loads(urllib.request.urlopen(url + "/models").read())
        self.assertEqual([m["id"] for m in models["data"]], ["claude-x", "claude-small"])
        rows, _ = self.relay.release("h1")
        self.assertEqual([r.get("helper", False) for r in rows if r.get("api")], [True, True, True, False, False])
        spent = sum((r.get("input_tokens") or 0) + (r.get("output_tokens") or 0) for r in rows if r.get("helper"))
        self.assertTrue(spent > 0)
        self.assertEqual(self.relay.budget.used, spent)  # sub-agent calls count against the run budget

    def test_mismatch_is_refused_before_any_upstream_call(self):
        url = self.register()
        code, _, _ = self.post(url + "/responses", {"model": "gpt-other", "reasoning": {"effort": "medium"}})
        self.assertEqual(code, 422)
        code, _, _ = self.post(url + "/responses", {"model": "gpt-x", "reasoning": {"effort": "high"}})
        self.assertEqual(code, 422)
        code, _, _ = self.post(url + "/responses", {"model": "gpt-x", "reasoning": {"effort": "medium"}, "max_output_tokens": 99999})
        self.assertEqual(code, 422)
        self.assertEqual(SEEN, [])

    def test_run_token_budget_stops_further_calls(self):
        url = self.register()
        ok = {"model": "gpt-x", "reasoning": {"effort": "medium"}, "stream": True}
        for _ in range(6):  # 150 tokens each: the 7th would pass the 1000-token budget
            self.assertEqual(self.post(url + "/responses", ok)[0], 200)
        self.assertEqual(self.post(url + "/responses", ok)[0], 200)  # 900 used: still under
        code, _, _ = self.post(url + "/responses", ok)
        self.assertEqual(code, 429)
        self.assertEqual(len(SEEN), 7)
        rows, _ = self.relay.release("a1")
        self.assertEqual(rows[-1]["error"], "run_token_budget_exhausted")

    def test_upstream_errors_and_unreachable_are_recorded(self):
        url = self.register(model="boom")
        self.assertEqual(self.post(url + "/responses", {"model": "boom", "reasoning": {"effort": "medium"}})[0], 400)
        dead = RelayModel("http://127.0.0.1:9", max_run_tokens=10)
        self.addCleanup(dead.close)
        url2 = dead.register("b1", None, "/tmp", "gpt-x", "medium", 16384)
        self.assertEqual(self.post(url2 + "/responses", {"model": "gpt-x", "reasoning": {"effort": "medium"}})[0], 502)
        rows, _ = self.relay.release("a1")
        self.assertEqual(rows[0]["error"], "upstream_400")
        self.assertEqual(dead.release("b1")[0][0]["error"], "upstream_unreachable")

    def test_model_list_names_only_the_planned_model(self):
        url = self.register()
        with urllib.request.urlopen(url + "/models", timeout=5) as r:
            self.assertEqual([m["id"] for m in json.loads(r.read())["data"]], ["gpt-x"])

    def test_usage_readers(self):
        reader = SSEUsage("responses")
        data = responses_sse()
        for i in range(0, len(data), 7):  # split mid-line
            reader.feed(data[i:i + 7])
        self.assertEqual(reader.usage, {"input_tokens": 120, "cached_input_tokens": 100, "output_tokens": 30,
                                        "cache_write_input_tokens": None})
        self.assertEqual(reader.id, "r1")  # from response.created
        self.assertEqual(usage_of("messages", {"input_tokens": 1, "cache_creation_input_tokens": 2, "output_tokens": 3}),
                         {"input_tokens": 3, "cached_input_tokens": 0, "output_tokens": 3, "cache_write_input_tokens": 2})
        # A Messages block that does not report cache writes: unknown, not zero.
        self.assertIsNone(usage_of("messages", {"input_tokens": 1, "output_tokens": 3})["cache_write_input_tokens"])

    def test_cache_write_is_split_out_without_changing_input_tokens_or_budget(self):
        url = self.register(model="claude-cw")
        base = url.removesuffix("/v1")
        code, _, _ = self.post(base + "/v1/messages", {"model": "claude-cw", "max_tokens": 16384, "stream": True,
                                                        "output_config": {"effort": "medium"}, "messages": []})
        self.assertEqual(code, 200)
        reader = SSEUsage("messages")
        reader.feed(messages_sse(written=40))
        self.assertEqual((reader.usage["input_tokens"], reader.usage["cache_write_input_tokens"], reader.id), (140, 40, "msg_up1"))
        rows, _ = self.relay.release("a1")
        # fresh 10 + read 90 + written 40, as before; the written part is also reported apart
        self.assertEqual((rows[0]["input_tokens"], rows[0]["cached_input_tokens"], rows[0]["cache_write_input_tokens"]), (140, 90, 40))
        self.assertEqual(rows[0]["upstream_id"], "msg_up1")
        self.assertEqual(self.relay.budget.used, 140 + 25)

    def test_other_wires_report_no_cache_write_and_ids_come_from_the_reply(self):
        url = self.register()
        self.post(url + "/responses", {"model": "gpt-x", "reasoning": {"effort": "medium"}, "stream": True})
        self.post(url + "/responses", {"model": "gpt-x", "reasoning": {"effort": "medium"}})  # not streamed
        self.post(url + "/chat/completions", {"model": "gpt-x", "reasoning_effort": "medium", "messages": []})
        self.post(url + "/chat/completions", {"model": "gpt-x", "reasoning_effort": "medium", "stream": True, "messages": []})
        rows, _ = self.relay.release("a1")
        self.assertEqual([r["cache_write_input_tokens"] for r in rows], [None] * 4)
        self.assertEqual([r["upstream_id"] for r in rows], ["r1", "r", None, None])  # chat replies here carry no id
        self.assertEqual([r["request_id"] for r in rows], [f"a1#{i}" for i in range(4)])
        self.assertEqual(self.relay.budget.used, 150 + 55 + 10 + 44)

    def test_probe_rows_carry_a_request_id(self):
        url = self.register()
        urllib.request.urlopen(url + "/models", timeout=5).close()
        rows, _ = self.relay.release("a1")
        self.assertEqual(rows[0]["request_id"], "a1#0")

    def test_truncated_sse_and_response_model_mismatch_are_not_completed(self):
        url = self.register(key="t1", model="trunc-sse")
        code, _, _ = self.post(url + "/responses", {"model": "trunc-sse", "reasoning": {"effort": "medium"}, "stream": True})
        self.assertEqual(code, 200)
        rows, _ = self.relay.release("t1")
        self.assertFalse(rows[0]["completed"])
        self.assertFalse(rows[0]["stream_complete"])
        self.assertEqual(rows[0]["error"], "stream_incomplete")

        url2 = self.register(key="m1", model="wrong-model")
        code2, _, _ = self.post(url2 + "/responses", {"model": "wrong-model", "reasoning": {"effort": "medium"}, "stream": True})
        self.assertEqual(code2, 200)
        rows2, _ = self.relay.release("m1")
        self.assertFalse(rows2[0]["completed"])
        self.assertEqual(rows2[0]["response_model"], "other-model")
        self.assertEqual(rows2[0]["error"], "response_model_mismatch")


if __name__ == "__main__":
    unittest.main()
