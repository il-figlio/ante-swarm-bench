"""Codex CLI and Claude Code support: tool mappings, the Messages wire format, and harness configuration."""
import json
import subprocess
import sys
import tomllib
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import OK_SCRIPT, tempdir  # noqa: E402
from swarm import harnesses  # noqa: E402
from swarm.scripted import ScriptedModel, anthropic_events, map_step, sh_quote  # noqa: E402

CODEX_TOOLS = ["exec_command", "write_stdin", "request_user_input", "view_image"]
CLAUDE_TOOLS = ["Agent", "Bash", "Edit", "Read", "Skill", "ToolSearch", "Write"]


def http(url, body=None, method="POST"):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.headers.get("Content-Type"), response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.headers.get("Content-Type"), error.read()


def sse(raw: bytes):
    out = []
    for block in raw.decode().strip().split("\n\n"):
        event, data = block.split("\n", 1)
        out.append((event.removeprefix("event: "), json.loads(data.removeprefix("data: "))))
    return out


class CodexMappingTests(unittest.TestCase):
    def test_codex_offers_only_a_shell_so_read_and_write_go_through_it(self):
        name, args, _ = map_step("/w", {"op": "read", "path": "a b/it's.txt"}, CODEX_TOOLS)
        self.assertEqual((name, args["cmd"]), ("exec_command", "cat -- '/w/a b/it'\\''s.txt'"))
        name, args, _ = map_step("/w", {"op": "write", "path": "x.txt", "content": "hi"}, CODEX_TOOLS)
        self.assertEqual(name, "exec_command")
        self.assertEqual(args["cmd"], "printf %s 'hi' > '/w/x.txt'")
        name, args, _ = map_step("/w", {"op": "bash", "command": "sleep 8"}, CODEX_TOOLS)
        self.assertEqual((name, args), ("exec_command", {"cmd": "sleep 8", "yield_time_ms": 30000}))

    def test_shell_write_is_byte_exact(self):
        content = "line1\nit's \"quoted\" $HOME `x` \\n 100% %s\n\ttab\nno trailing newline"
        with tempdir() as d:
            _, args, _ = map_step(d, {"op": "write", "path": "out.txt", "content": content}, CODEX_TOOLS)
            subprocess.run(["/bin/sh", "-c", args["cmd"]], check=True)
            self.assertEqual((Path(d) / "out.txt").read_text(), content)
            _, args, _ = map_step(d, {"op": "read", "path": "out.txt"}, CODEX_TOOLS)
            self.assertEqual(subprocess.run(["/bin/sh", "-c", args["cmd"]], capture_output=True, text=True).stdout, content)
        self.assertEqual(sh_quote("a'b"), "'a'\\''b'")

    def test_native_tools_win_over_the_shell_and_claude_code_tools_map(self):
        offered = CLAUDE_TOOLS + ["exec_command"]
        self.assertEqual(map_step("/w", {"op": "read", "path": "a"}, offered)[:2], ("Read", {"file_path": "/w/a"}))
        self.assertEqual(map_step("/w", {"op": "write", "path": "a", "content": "c"}, offered)[:2],
                         ("Write", {"file_path": "/w/a", "content": "c"}))
        name, args, _ = map_step("/w", {"op": "bash", "command": "ls"}, CLAUDE_TOOLS)
        self.assertEqual((name, args["command"]), ("Bash", "ls"))
        self.assertEqual(map_step("/w", {"op": "edit", "path": "a", "old": "x", "new": "y"}, CLAUDE_TOOLS)[:2],
                         ("Edit", {"file_path": "/w/a", "old_string": "x", "new_string": "y"}))

    def test_missing_tools_are_named(self):
        for op, step in (("read", {"op": "read", "path": "a"}), ("write", {"op": "write", "path": "a", "content": ""}),
                         ("bash", {"op": "bash", "command": "x"}), ("edit", {"op": "edit", "path": "a", "old": "", "new": ""})):
            with self.assertRaises(LookupError) as raised:
                map_step("/w", step, ["Skill"])
            self.assertEqual(str(raised.exception), op)
        with self.assertRaises(LookupError):
            map_step("/w", {"op": "edit", "path": "a", "old": "", "new": ""}, CODEX_TOOLS)  # no shell fallback for edit


class ResponsesServerForCodexTests(unittest.TestCase):
    def setUp(self):
        self.model = ScriptedModel()
        self.addCleanup(self.model.close)

    def test_codex_style_request_without_an_output_cap_is_served(self):
        url = self.model.register("cx", OK_SCRIPT, "/w", "m", "medium", 16384)
        body = {"model": "m", "stream": True, "reasoning": {"effort": "medium", "summary": "auto"},
                "input": [{"type": "message", "role": "user", "content": []}],
                "tools": [{"type": "function", "name": n} for n in CODEX_TOOLS]}
        status, ctype, raw = http(url + "/responses", body)
        self.assertEqual((status, ctype), (200, "text/event-stream"))
        events = dict(sse(raw))
        self.assertEqual(events["response.output_item.done"]["item"]["name"], "exec_command")
        self.assertIn("cat --", json.loads(events["response.output_item.done"]["item"]["arguments"])["cmd"])
        row = self.model.release("cx")[0][0]
        self.assertEqual((row["tool"], row["op"], row["arg"], row["max_output_tokens"]), ("exec_command", "read", "seed.txt", None))


class MessagesServerTests(unittest.TestCase):
    def setUp(self):
        self.model = ScriptedModel()
        self.addCleanup(self.model.close)
        self.url = self.model.register("cc", OK_SCRIPT, "/w", "m", "medium", 16384)
        self.root = self.url.removesuffix("/v1")

    def body(self, results=0, **over):
        messages = [{"role": "user", "content": [{"type": "text", "text": "go"}]}]
        for i in range(results):
            messages += [{"role": "assistant", "content": [{"type": "tool_use", "id": f"t{i}", "name": "Bash", "input": {}}]},
                         {"role": "user", "content": [{"type": "tool_result", "tool_use_id": f"t{i}", "content": "ok"}]}]
        return dict({"model": "m", "max_tokens": 16384, "stream": True, "output_config": {"effort": "medium"},
                     "messages": messages, "tools": [{"name": n, "input_schema": {}} for n in CLAUDE_TOOLS]}, **over)

    def test_streaming_tool_use_event_sequence(self):
        status, ctype, raw = http(self.root + "/v1/messages", self.body(0))
        self.assertEqual((status, ctype), (200, "text/event-stream"))
        events = sse(raw)
        self.assertEqual([e for e, _ in events], ["message_start", "ping", "content_block_start", "content_block_delta",
                                                  "content_block_stop", "message_delta", "message_stop"])
        for name, data in events:
            self.assertEqual(data["type"], name)
        start = events[0][1]["message"]
        self.assertEqual((start["role"], start["content"], start["stop_reason"], start["model"]), ("assistant", [], None, "m"))
        block = events[2][1]
        self.assertEqual((block["index"], block["content_block"]["type"], block["content_block"]["name"], block["content_block"]["input"]),
                         (0, "tool_use", "Read", {}))
        delta = events[3][1]["delta"]
        self.assertEqual(delta["type"], "input_json_delta")
        self.assertEqual(json.loads(delta["partial_json"]), {"file_path": "/w/seed.txt"})
        self.assertEqual(events[5][1]["delta"], {"stop_reason": "tool_use", "stop_sequence": None})
        self.assertGreater(events[5][1]["usage"]["output_tokens"], 0)

    def test_steps_follow_tool_results_and_final_ends_the_turn(self):
        tools = []
        for i in range(3):
            _, _, raw = http(self.root + "/v1/messages", self.body(i, stream=False))
            message = json.loads(raw)
            self.assertEqual((message["type"], message["role"], message["stop_reason"]), ("message", "assistant", "tool_use"))
            tools.append(message["content"][0]["name"])
        self.assertEqual(tools, ["Read", "Write", "Bash"])
        events = sse(http(self.root + "/v1/messages", self.body(3))[2])
        self.assertEqual([e for e, _ in events][2:5], ["content_block_start", "content_block_delta", "content_block_stop"])
        self.assertEqual(events[2][1]["content_block"], {"type": "text", "text": ""})
        self.assertEqual(events[3][1]["delta"], {"type": "text_delta", "text": "done"})
        self.assertEqual(events[5][1]["delta"]["stop_reason"], "end_turn")
        rows, final_served = self.model.release("cc")
        self.assertTrue(final_served)
        self.assertEqual([r["tool"] for r in rows], ["Read", "Write", "Bash", None])
        self.assertEqual([r["api"] for r in rows], ["messages"] * 4)

    def test_probes_and_count_tokens_are_harmless(self):
        self.assertEqual(http(self.root + "/", method="HEAD")[0], 200)
        self.assertEqual(http(self.root + "/v1/messages", method="GET")[0], 200)
        status, _, raw = http(self.root + "/v1/messages/count_tokens", {"model": "m", "messages": []})
        self.assertEqual(status, 200)
        self.assertGreater(json.loads(raw)["input_tokens"], 0)
        self.assertEqual(http(self.root.replace("/s/", "/s/0"), method="HEAD")[0], 404)  # unknown token
        rows, _ = self.model.release("cc")
        self.assertEqual([r.get("kind") for r in rows], ["probe", "probe", "count_tokens"])
        self.assertTrue(all(r["status"] == 200 and not r["error"] for r in rows))

    def test_effort_and_output_cap_are_checked_on_messages(self):
        self.assertEqual(http(self.root + "/v1/messages", self.body(output_config={"effort": "high"}))[0], 422)
        self.assertEqual(http(self.root + "/v1/messages", self.body(max_tokens=32000))[0], 422)
        self.assertEqual(http(self.root + "/v1/messages", self.body(model="other"))[0], 422)
        self.assertEqual(http(self.root + "/v1/messages", self.body(tools=[]))[0], 400)
        rows, _ = self.model.release("cc")
        self.assertEqual([r["error"] for r in rows], ["settings_mismatch"] * 3 + ["tool_unavailable"])
        self.assertEqual(rows[0]["settings_mismatch"], ["effort"])

    def test_event_builder_matches_the_non_streaming_message(self):
        message = {"id": "m1", "type": "message", "role": "assistant", "model": "m", "stop_reason": "tool_use", "stop_sequence": None,
                   "content": [{"type": "text", "text": "a"}, {"type": "tool_use", "id": "t", "name": "Bash", "input": {"command": "ls"}}],
                   "usage": {"input_tokens": 5, "output_tokens": 7}}
        names = [n for n, _ in anthropic_events(message)]
        self.assertEqual(names.count("content_block_start"), 2)
        self.assertEqual(names[-2:], ["message_delta", "message_stop"])

    def test_trace_file_gets_each_completed_row(self):
        with tempdir() as d:
            trace = Path(d) / "requests.live.jsonl"
            url = self.model.register("tr", OK_SCRIPT, "/w", "m", "medium", 16384, trace=trace)
            http(url.removesuffix("/v1") + "/v1/messages", self.body(0))
            rows = [json.loads(line) for line in trace.read_text().splitlines()]
            self.assertEqual([(r["tool"], r["arg"]) for r in rows], [("Read", "seed.txt")])


class HarnessConfigTests(unittest.TestCase):
    def test_registered(self):
        self.assertEqual({"codex", "claude-code"} <= set(harnesses.REGISTRY), True)

    def test_codex_config_points_at_the_scripted_url_and_turns_network_features_off(self):
        with tempdir() as d:
            home = Path(d)
            h = harnesses.get("codex")
            h.validate_options()
            env = h.prepare(home, "http://127.0.0.1:1234/s/abc/v1", "swarm-scripted-1", "medium", 16384)
            cfg = tomllib.loads((home / ".codex" / "config.toml").read_text())
            self.assertEqual(env["CODEX_HOME"], str(home / ".codex"))
            self.assertEqual(cfg["model_provider"], "swarm")
            provider = cfg["model_providers"]["swarm"]
            self.assertEqual((provider["base_url"], provider["wire_api"], provider["env_key"]),
                             ("http://127.0.0.1:1234/s/abc/v1", "responses", "SWARM_API_KEY"))
            self.assertIn("SWARM_API_KEY", env)
            self.assertEqual((cfg["analytics"]["enabled"], cfg["feedback"]["enabled"], cfg["check_for_update_on_startup"]),
                             (False, False, False))
            self.assertTrue(all(v is False for v in cfg["features"].values()))
            self.assertEqual(cfg["web_search"], "disabled")
            self.assertEqual(cfg["model_reasoning_effort"], "medium")
            argv = h.argv(Path("/x/codex"), "do it", home, "swarm-scripted-1", "medium")
            self.assertEqual(argv[:2], ["/x/codex", "exec"])
            for flag in ("--skip-git-repo-check", "--dangerously-bypass-approvals-and-sandbox", "--ephemeral"):
                self.assertIn(flag, argv)
            self.assertEqual(argv[-1], "do it")

    def test_claude_code_env_and_argv(self):
        with tempdir() as d:
            home = Path(d)
            h = harnesses.get("claude-code")
            env = h.prepare(home, "http://127.0.0.1:1234/s/abc/v1", "swarm-scripted-1", "medium", 16384)
            self.assertEqual(env["ANTHROPIC_BASE_URL"], "http://127.0.0.1:1234/s/abc")
            for key in ("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "DISABLE_TELEMETRY", "DISABLE_AUTOUPDATER", "DISABLE_ERROR_REPORTING"):
                self.assertEqual(env[key], "1")
            self.assertTrue(env["ANTHROPIC_API_KEY"].startswith("sk-ant-"))
            self.assertEqual(env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"], "16384")
            self.assertTrue(json.loads((home / ".claude.json").read_text())["hasCompletedOnboarding"])
            argv = h.argv(Path("/x/claude"), "do it", home, "swarm-scripted-1", "medium")
            self.assertEqual(argv[:3], ["/x/claude", "-p", "do it"])
            self.assertEqual(argv[argv.index("--model") + 1], "swarm-scripted-1")
            self.assertEqual(argv[argv.index("--effort") + 1], "medium")
            for flag in ("--dangerously-skip-permissions", "--no-session-persistence"):
                self.assertIn(flag, argv)
            self.assertNotIn("ANTHROPIC_DEFAULT_HAIKU_MODEL", env)
            h.helper = "claude-small"  # a race with a sub-agent model: Claude Code's Haiku tier points at it
            env = h.prepare(home, "http://127.0.0.1:1234/s/abc/v1", "claude-x", "medium", 16384)
            self.assertEqual((env["ANTHROPIC_DEFAULT_HAIKU_MODEL"], env["ANTHROPIC_SMALL_FAST_MODEL"]), ("claude-small", "claude-small"))

    def test_hermes_turn_cap_follows_the_budget_and_gateway_stop_is_teardown(self):
        h = harnesses.get("hermes", {"home": "cold"})
        argv = h.argv(Path("/x/hermes"), "do it", Path("/w"), "m", "medium")
        self.assertEqual(argv[0], "/x/hermes")
        self.assertNotIn("gateway", argv)
        self.assertNotIn("/bin/sh", argv)
        self.assertEqual(argv[argv.index("--max-turns") + 1], "300")
        h.turn_cap = 260
        argv = h.argv(Path("/x/hermes"), "do it", Path("/w"), "m", "medium")
        self.assertEqual(argv[argv.index("--max-turns") + 1], "260")
        with tempdir() as d:
            home = Path(d)
            env = h.prepare(home, "http://127.0.0.1:9/v1", "m", "medium", 16384)
            settings = json.loads((home / ".hermes" / "config.yaml").read_text())
            self.assertEqual(settings["agent"]["max_turns"], 260)
            self.assertIn("HERMES_HOME", env)
        self.assertTrue(callable(h.teardown))

    def test_clean_environment_carries_no_operator_credentials(self):
        import os
        with tempdir() as d:
            os.environ["OPENAI_API_KEY"] = os.environ["ANTHROPIC_API_KEY"] = "operator-secret"
            self.addCleanup(lambda: [os.environ.pop(k, None) for k in ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")])
            for name in ("codex", "claude-code"):
                env = harnesses.get(name).environment(Path("/x/bin"), Path(d))
                self.assertNotIn("operator-secret", json.dumps(env))


def _real(env_name):
    """A real CLI binary named by an environment variable (the CLIs are not vendored); tests skip without it."""
    import os
    path = os.environ.get(env_name)
    return path if path and Path(path).exists() else None


class RealCliTests(unittest.TestCase):
    """End to end with the real binaries: SWARM_TEST_CODEX=/path/codex SWARM_TEST_CLAUDE=/path/claude."""

    def run_one(self, name, binary, tools):
        from helpers import make_task
        from swarm.bundle import verify
        from swarm.plan import freeze
        from swarm.runner import run
        with tempdir() as d:
            base = Path(d)
            make_task(base / "tasks", "one", OK_SCRIPT)
            from helpers import make_spec
            spec = make_spec(base, harnesses=(name,), levels=(1,))
            plan = freeze(spec, {name: binary})
            (base / "plan.json").write_text(json.dumps(plan))
            bundle = run(base / "plan.json", base / "bundle")
            record = json.loads((bundle / "attempts.jsonl").read_text().splitlines()[0])
            self.assertTrue(record["passed"], record)
            self.assertEqual(record["requests"]["tools_used"], tools)
            self.assertEqual(verify(bundle), [])

    @unittest.skipUnless(_real("SWARM_TEST_CODEX"), "set SWARM_TEST_CODEX to a codex binary")
    def test_real_codex(self):
        self.run_one("codex", _real("SWARM_TEST_CODEX"), ["exec_command"])

    @unittest.skipUnless(_real("SWARM_TEST_CLAUDE"), "set SWARM_TEST_CLAUDE to a claude binary")
    def test_real_claude_code(self):
        self.run_one("claude-code", _real("SWARM_TEST_CLAUDE"), ["Bash", "Read", "Write"])


if __name__ == "__main__":
    unittest.main()
