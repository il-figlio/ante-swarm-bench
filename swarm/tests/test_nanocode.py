"""nanocode support: harness configuration, the Code Mode mapping in the scripted model, and one live attempt
when a nanocode binary is installed (SWARM_NANOCODE_BIN, or `nanocode` on PATH / in ~/.nanocode/bin)."""
import json
import os
import subprocess
import sys
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import OK_SCRIPT, tempdir  # noqa: E402
from swarm import harnesses  # noqa: E402
from swarm.harnesses.nanocode import Nanocode  # noqa: E402
from swarm.scripted import ScriptedModel, code_mode_program, map_step  # noqa: E402

OPTIONS = {"code_mode": "on", "subagents": "off"}
# What nanocode 0.7.0 sends: no `tools`, an `additional_tools` input item with Code Mode's two entry points.
ADDITIONAL_TOOLS = {"type": "additional_tools", "role": "developer",
                    "tools": [{"type": "custom", "name": "exec", "format": {"type": "grammar"}},
                              {"type": "function", "name": "wait", "parameters": {}}]}


def http(url, body=None, method="POST"):
    data = json.dumps(body).encode() if body is not None else None
    request = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=10) as response:
            return response.status, response.read()
    except urllib.error.HTTPError as error:
        return error.code, error.read()


class NanocodeConfigTests(unittest.TestCase):
    def test_registered(self):
        self.assertIs(harnesses.REGISTRY["nanocode"], Nanocode)

    def test_options_are_required_and_closed(self):
        for options in ({}, {"code_mode": "on"}, {"subagents": "off"}, {"code_mode": "off", "subagents": "off"},
                        dict(OPTIONS, telemetry="off")):
            with self.assertRaises(ValueError):
                Nanocode(options).validate_options()
        Nanocode(OPTIONS).validate_options()
        Nanocode({"code_mode": "on", "subagents": "on"}).validate_options()

    def test_prepare_points_at_the_scripted_url_with_telemetry_and_tempo_off(self):
        with tempdir() as d:
            home = Path(d)
            env = Nanocode(OPTIONS).prepare(home, "http://127.0.0.1:1234/s/abc/v1", "swarm-scripted-1", "medium", 16384)
            self.assertEqual(env["NANOCODE_OPENAI_BASE_URL"], "http://127.0.0.1:1234/s/abc/v1")
            self.assertEqual(env["NANOCODE_HOME"], str(home / ".nanocode"))
            self.assertTrue((home / ".nanocode").is_dir())
            self.assertEqual((env["NANOCODE_TELEMETRY"], env["NANOCODE_NO_TEMPO_TOOLS"]), ("off", "true"))
            self.assertNotIn("OPENAI_API_KEY", env)
            self.assertFalse(any(k.startswith(("OTEL_", "CODEX_")) for k in env))

    def test_argv_states_model_effort_and_off_switches(self):
        argv = Nanocode(OPTIONS).argv(Path("/opt/nc/nanocode"), "-do it", Path("/w"), "swarm-scripted-1", "medium")
        self.assertEqual(argv[:2], ["/opt/nc/nanocode", "exec"])
        self.assertEqual(argv[-2:], ["--", "-do it"])  # a prompt starting with '-' stays the prompt
        pairs = dict(zip(argv, argv[1:]))
        self.assertEqual((pairs["--model"], pairs["--thinking"], pairs["--telemetry"], pairs["--cd"]),
                         ("swarm-scripted-1", "medium", "off", "/w"))
        for flag in ("--ephemeral", "--no-tempo-tools", "--no-subagents"):
            self.assertIn(flag, argv)
        self.assertNotIn("--no-subagents", Nanocode({"code_mode": "on", "subagents": "on"}).argv(
            Path("/nc"), "p", Path("/w"), "m", "low"))


class CodeModeMappingTests(unittest.TestCase):
    def test_every_op_becomes_one_exec_program_running_the_codex_shell_command(self):
        offered = ["exec", "wait"]
        name, program, text = map_step("/w", {"op": "write", "path": "x.txt", "content": "it's\n"}, offered)
        self.assertEqual((name, text), ("exec", None))
        self.assertIsInstance(program, str)
        self.assertTrue(program.startswith('// @exec: {"yield_time_ms": 30000}\n'))
        call = program.splitlines()[1]
        args = json.loads(call.removeprefix("const result = await tools.exec_command(").removesuffix(");"))
        self.assertEqual(args, {"cmd": "printf %s 'it'\\''s\n' > '/w/x.txt'", "yield_time_ms": 30000})
        self.assertIn("cat -- '/w/a'", map_step("/w", {"op": "read", "path": "a"}, offered)[1])
        self.assertEqual(map_step("/w", {"op": "bash", "command": "sleep 8"}, offered)[1], code_mode_program("sleep 8"))
        with self.assertRaises(LookupError):
            map_step("/w", {"op": "edit", "path": "a", "old": "", "new": ""}, offered)

    def test_a_shell_tool_wins_over_exec(self):
        self.assertEqual(map_step("/w", {"op": "bash", "command": "ls"}, ["exec", "exec_command"])[0], "exec_command")


class ScriptedServerForNanocodeTests(unittest.TestCase):
    def setUp(self):
        self.model = ScriptedModel()
        self.addCleanup(self.model.close)
        self.url = self.model.register("nc", OK_SCRIPT, "/w", "m", "medium", 16384)

    def body(self, results=0):
        items = [ADDITIONAL_TOOLS, {"type": "message", "role": "user", "content": []}]
        for i in range(results):
            items += [{"type": "custom_tool_call", "call_id": f"c{i}", "name": "exec", "input": "..."},
                      {"type": "custom_tool_call_output", "call_id": f"c{i}", "output": "ok"}]
        return {"model": "m", "stream": True, "reasoning": {"effort": "medium", "summary": "auto"}, "input": items}

    def test_models_list_names_the_attempts_model(self):
        status, raw = http(self.url + "/models", method="GET")
        self.assertEqual(status, 200)
        self.assertEqual([m["id"] for m in json.loads(raw)["data"]], ["m"])
        rows, _ = self.model.release("nc")
        self.assertEqual([(r["kind"], r["path"]) for r in rows], [("probe", "/s/<token>/v1/models")])

    def test_custom_tool_call_stream_and_step_counting(self):
        status, raw = http(self.url + "/responses", self.body(0))
        self.assertEqual(status, 200)
        events = [(block.split("\n", 1)[0].removeprefix("event: "), json.loads(block.split("\n", 1)[1].removeprefix("data: ")))
                  for block in raw.decode().strip().split("\n\n")]
        names = [e for e, _ in events]
        self.assertIn("response.custom_tool_call_input.delta", names)
        done = dict(events)["response.output_item.done"]["item"]
        self.assertEqual((done["type"], done["name"]), ("custom_tool_call", "exec"))
        self.assertIn("cat --", done["input"])
        delta = dict(events)["response.custom_tool_call_input.delta"]
        self.assertEqual(delta["delta"], done["input"])
        for i in (1, 2):
            http(self.url + "/responses", dict(self.body(i), stream=False))
        _, raw = http(self.url + "/responses", dict(self.body(3), stream=False))
        self.assertEqual(json.loads(raw)["output"][0]["type"], "message")
        rows, final = self.model.release("nc")
        self.assertTrue(final)
        self.assertEqual([(r["op"], r["tool"]) for r in rows], [("read", "exec"), ("write", "exec"), ("bash", "exec"), ("final", None)])
        self.assertEqual(rows[0]["tools_offered"], ["exec", "wait"])


def nanocode_binary():
    explicit = os.environ.get("SWARM_NANOCODE_BIN")
    return Nanocode(OPTIONS).locate(explicit) if explicit else Nanocode(OPTIONS).locate()


@unittest.skipUnless(nanocode_binary(), "nanocode not installed (set SWARM_NANOCODE_BIN)")
class NanocodeLiveTests(unittest.TestCase):
    def test_one_attempt_against_the_scripted_model(self):
        binary = nanocode_binary()
        model = ScriptedModel()
        self.addCleanup(model.close)
        with tempdir() as d:
            home, work = Path(d) / "home", Path(d) / "work"
            home.mkdir()
            work.mkdir()
            (work / "seed.txt").write_text("seed\n")
            url = model.register("live", OK_SCRIPT, work, "swarm-scripted-1", "medium", 16384, 20)
            harness = Nanocode(OPTIONS)
            env = harness.environment(binary, home)
            env.update(harness.prepare(home, url, "swarm-scripted-1", "medium", 16384))
            out = subprocess.run(harness.argv(binary, "Write ok to out.txt.", work, "swarm-scripted-1", "medium"),
                                 cwd=work, env=env, capture_output=True, text=True, timeout=120, stdin=subprocess.DEVNULL)
            rows, final = model.release("live")
            self.assertEqual(out.returncode, 0, out.stderr[-2000:])
            self.assertTrue(final)
            self.assertEqual((work / "out.txt").read_text(), "ok")
            model_rows = [r for r in rows if r.get("kind") != "probe"]
            self.assertEqual([r["status"] for r in model_rows], [200] * 4)
            self.assertEqual([r["tool"] for r in model_rows], ["exec", "exec", "exec", None])
            self.assertTrue(all(r["settings_mismatch"] == [] for r in model_rows))


if __name__ == "__main__":
    unittest.main()
