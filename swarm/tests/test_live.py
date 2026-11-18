import json
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from swarm.live import Tailer, clean, transcript  # noqa: E402

WORK = "/private/var/folders/2y/abc/T/swarm-x1/c001-ante-n4-s002/work"
URL = "http://127.0.0.1:59271/s/feedfacefeedfacefeedfacefeedface/v1"


def event(kind, body, stamp="2026-10-30T12:28:22.205887Z"):
    return json.dumps({"timestamp": stamp, "id": "evt", "event": {kind: body}}) + "\n"


def fake_bundle(root: Path) -> Path:
    b = root / "bundle"
    d = b / "logs" / "c001-ante-n4-s002"
    d.mkdir(parents=True)
    (d / "agent.log").write_text(
        event("SessionStart", {"provider": {"base_url": URL}, "cwd": WORK})
        + event("UserInput", f"Fix it.\n\n<folder-structure>\n- {WORK}/\n</folder-structure>")
        + event("ToolStart", {"id": "c0", "name": "Read", "args": {"file_path": f"{WORK}/app.log"}})
        + event("ToolEnd", {"tool_use_id": "c0", "tool_name": "Read", "status": "Completed",
                            "result_json": {"file_type": {"Text": {"lines": ["a ERROR api\n"]}}}}, "2026-10-30T12:28:22.305887Z")
        + event("ToolStart", {"id": "c1", "name": "Bash", "args": {"command": f"cd {WORK} && python3 triage.py", "description": "x"}})
        + event("ToolEnd", {"tool_use_id": "c1", "tool_name": "Bash", "status": "Completed",
                            "result_json": {"Completed": {"exit_code": 0, "stdout": f"wrote {WORK}/errors.json via {URL}\n"}}})
        + event("AgentMessage", "Done.")
        + event("TurnEnd", {"status": "Completed", "steps": 3}))
    (d / "check.log").write_text("PASS\n")
    (b / "attempts.jsonl").write_text(json.dumps({"attempt": "c001-ante-n4-s002", "task": "log-triage", "passed": True,
                                                  "agent": {"wall_s": 0.2, "max_process_rss_kb": 40960}, "check": {"changed": ["errors.json"]}}) + "\n")
    (b / "admission.jsonl").write_text(json.dumps({"attempt": "c001-ante-n4-s002", "admitted": True, "available_mb": 5000,
                                                   "pressure": "normal", "wait_s": 0.01, "t": time.time()}) + "\n")
    return b


class LiveTests(unittest.TestCase):
    def test_clean_removes_sandbox_paths_tokens_and_homes(self):
        s = clean(f"{WORK}/x.py /home/exedev/swarm-live-data/runs {URL} /tmp/abc/def /Users/someone/file")
        self.assertEqual(s, "x.py ~/swarm-live-data/runs <model> <tmp> ~/file")

    def test_transcript_is_complete_and_clean(self):
        with tempfile.TemporaryDirectory() as d:
            t = transcript(fake_bundle(Path(d)), 2)
            blob = json.dumps(t)
            for leak in ("/private/var", "feedfacefeed", "127.0.0.1", "folder-structure", "swarm-x1"):
                self.assertNotIn(leak, blob)
            self.assertEqual(t["prompt"], "Fix it.")
            self.assertEqual([(s["tool"], s["arg"]) for s in t["steps"]], [("Read", "app.log"), ("Bash", "cd . && python3 triage.py")])
            self.assertEqual(t["steps"][0]["result"], "a ERROR api")
            self.assertEqual(t["steps"][0]["ms"], 100)
            self.assertTrue(t["steps"][1]["result"].startswith("exit 0\nwrote errors.json via <model>"))
            self.assertEqual((t["messages"], t["passed"], t["check"]["log"], t["agent"]["rss_mb"]), (["Done."], True, "PASS", 40.0))
            self.assertIsNone(transcript(Path(d) / "bundle", 7))

    def test_tailer_streams_admissions_tools_and_messages(self):
        with tempfile.TemporaryDirectory() as d:
            tailer = Tailer(fake_bundle(Path(d)))
            tailer.finish()
            events, nxt = tailer.since(0)
            self.assertEqual([e["k"] for e in events].count("tool"), 2)
            self.assertIn("admit", [e["k"] for e in events])
            self.assertIn({"slot": 2, "k": "msg", "text": "Done."}, [{k: e[k] for k in ("slot", "k", "text")} for e in events if e["k"] == "msg"])
            self.assertNotIn("/private/var", json.dumps(events))
            self.assertEqual(tailer.since(nxt)[0], [])
            kinds = [e["k"] for e in events if e["slot"] == 2]
            self.assertEqual(kinds[-1], "result")  # the verdict comes after the agent's own events
            self.assertTrue(events[-1]["ok"])


def request_bundle(root: Path, harness: str, slot=3) -> Path:
    """A bundle whose harness (Codex CLI, Claude Code) has no per-tool log: only the scripted server's request rows."""
    b = root / "bundle"
    attempt = f"c002-{harness}-n4-s{slot:03d}"
    d = b / "logs" / attempt
    d.mkdir(parents=True)
    task = root / "task"
    (task / "tests").mkdir(parents=True)
    (task / "instruction.md").write_text(f"Fix it in {WORK}\nkey sk-ant-api03-abcdefghijklmnop url {URL}\n")
    (task / "tests" / "test.sh").write_text("#!/bin/bash\ntrue\n")
    now = time.time()
    rows = [{"index": 0, "status": 200, "op": "read", "tool": "exec_command" if harness == "codex" else "Read", "arg": "app.log", "at": now},
            {"index": 1, "status": 200, "op": "bash", "tool": "exec_command" if harness == "codex" else "Bash",
             "arg": f"python3 triage.py {WORK}/x --token feedfacefeedfacefeedfacefeedface", "at": now + 0.1},
            {"index": 2, "status": 404, "error": "route_rejected", "path": "/s/<token>/x"},
            {"index": 3, "status": 200, "op": "final", "tool": None, "arg": "All done.", "at": now + 0.2}]
    text = "".join(json.dumps(r) + "\n" for r in rows)
    (d / "requests.live.jsonl").write_text(text)
    (d / "agent.log").write_text(f"OpenAI Codex\nworkdir: {WORK}\nmodel provider base_url {URL}\n")  # never shown
    (b / "plan.json").write_text(json.dumps({"spec": {"model": {"id": "swarm-scripted-1", "effort": "medium"}},
                                              "tasks": [{"id": "log-triage", "path": str(task)}]}))
    (b / "attempts.jsonl").write_text(json.dumps({"attempt": attempt, "task": "log-triage", "passed": True,
                                                  "agent": {"wall_s": 0.3, "max_process_rss_kb": 99000}, "check": {"changed": []}}) + "\n")
    (b / "admission.jsonl").write_text("")
    return b


class OtherHarnessTests(unittest.TestCase):
    def test_clean_removes_keys_and_bare_tokens(self):
        s = clean("ANTHROPIC_API_KEY=sk-ant-api03-abcdefghijklmnop OPENAI=sk-proj-abcdefghijklmnopqrstuvwx "
                  "token feedfacefeedfacefeedfacefeedface run 0123456789ab disk-usage task-list sk-short")
        self.assertEqual(s, "ANTHROPIC_API_KEY=<key> OPENAI=<key> token <token> run 0123456789ab disk-usage task-list sk-short")
        self.assertEqual(clean("http://127.0.0.1:41233/s/feedfacefeedfacefeedfacefeedface"), "<model>")
        self.assertEqual(clean("/home/exedev/c001-claude-code-n4-s002/home/.claude/x /private/var/folders/aa/T/swarm-q/c001-codex-n4-s002/work/a.py"),
                         "~/.claude/x a.py")

    def test_transcripts_fall_back_to_request_rows_and_stay_clean(self):
        for harness, tools in (("codex", ["exec_command"] * 2), ("claude-code", ["Read", "Bash"])):
            with tempfile.TemporaryDirectory() as d:
                t = transcript(request_bundle(Path(d), harness), 3)
                blob = json.dumps(t)
                for leak in ("/private/var", "feedfacefeed", "127.0.0.1", "sk-ant", "/home/", "/Users/", "swarm-x1", "OpenAI Codex"):
                    self.assertNotIn(leak, blob)
                self.assertEqual(t["harness"], harness)
                self.assertEqual([s["tool"] for s in t["steps"]], tools)
                self.assertEqual(t["steps"][0]["arg"], "app.log")
                self.assertEqual(t["steps"][1]["arg"], "python3 triage.py x --token <token>")
                self.assertEqual((t["messages"], t["passed"], t["agent"]["rss_mb"]), (["All done."], True, 96.7))
                self.assertEqual(t["command"][0], "codex" if harness == "codex" else "claude")
                self.assertIn("<prompt below>", t["command"])
                self.assertIn("--dangerously", " ".join(t["command"]))
                self.assertTrue(t["prompt"].startswith("Fix it in ."))
                self.assertIn("<key>", t["prompt"])

    def test_tailer_reads_request_rows_for_other_harnesses(self):
        for harness in ("codex", "claude-code"):
            with tempfile.TemporaryDirectory() as d:
                tailer = Tailer(request_bundle(Path(d), harness))
                tailer.finish()
                events, _ = tailer.since(0)
                mine = [e for e in events if e["slot"] == 3]
                self.assertEqual([e["k"] for e in mine], ["tool", "tool", "msg", "result"])
                self.assertEqual(mine[1]["arg"], "python3 triage.py x --token <token>")
                blob = json.dumps(events)
                for leak in ("/private/var", "feedfacefeed", "127.0.0.1", "OpenAI Codex"):
                    self.assertNotIn(leak, blob)


if __name__ == "__main__":
    unittest.main()
