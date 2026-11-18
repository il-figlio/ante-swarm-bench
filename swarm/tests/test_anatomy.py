import copy
import json
import unittest
from pathlib import Path

from swarm import anatomy
from swarm.scripted import offered_tools

FIX = Path(__file__).resolve().parent / "fixtures" / "anatomy"
KINDS = {"responses": "responses", "messages": "messages", "chat": "chat", "nanocode": "responses"}
INSTRUCTION = "Write ok to out.txt.\n"
UA = "tool/1.0"


def load(name):
    return json.loads((FIX / f"{name}.json").read_text())


class SplitTests(unittest.TestCase):
    def test_segments_sum_to_canonical(self):
        for name, kind in KINDS.items():
            with self.subTest(name):
                a = anatomy.split(kind, load(name), INSTRUCTION)
                self.assertEqual(a["wire"], kind)
                total = a["system_bytes"] + a["tools_bytes"] + a["user_bytes"] + a["other_bytes"]
                self.assertEqual(total, a["canonical_bytes"])
                self.assertGreaterEqual(a["other_bytes"], 0)
                for k in ("system_bytes", "tools_bytes", "user_bytes"):
                    self.assertGreater(a[k], 0, k)

    def test_tool_count_matches_scripted(self):
        for name, kind in KINDS.items():
            with self.subTest(name):
                body = load(name)
                self.assertEqual(anatomy.split(kind, body, INSTRUCTION)["tool_count"], len(offered_tools(body)))
        self.assertEqual(anatomy.split("responses", load("nanocode"), None)["tool_count"], 3)  # tools + additional_tools

    def test_mcp_tool_count(self):
        counts = {n: anatomy.split(k, load(n), None)["mcp_tool_count"] for n, k in KINDS.items()}
        self.assertEqual(counts, {"responses": 1, "messages": 1, "chat": 0, "nanocode": 1})

    def test_instruction_and_injected(self):
        a = anatomy.split("responses", load("responses"), INSTRUCTION)
        self.assertEqual(a["instruction_bytes"], len(INSTRUCTION))
        self.assertEqual(a["injected_bytes"], a["user_text_bytes"] - len(INSTRUCTION))
        self.assertGreater(a["injected_bytes"], 0)
        # Only text parts count: the tool_result in the Messages fixture adds nothing.
        m = anatomy.split("messages", load("messages"), INSTRUCTION)
        self.assertEqual(m["user_text_bytes"], len("<system-reminder>cwd /tmp/swarm/c001-claude-n1-s001/work</system-reminder>") + len(INSTRUCTION))

    def test_instruction_not_verbatim_is_null(self):
        a = anatomy.split("responses", load("responses"), "Something else entirely")
        self.assertIsNone(a["instruction_bytes"])
        self.assertIsNone(a["injected_bytes"])
        self.assertIsNotNone(a["user_text_bytes"])
        self.assertIsNone(anatomy.split("responses", load("responses"), None)["instruction_bytes"])

    def test_malformed_bodies(self):
        bad = [None, 5, "x", [], {}, {"tools": 3, "input": 7}, {"messages": [None, 1, {"role": "user", "content": 4}]},
               {"input": [None, {"role": "user", "content": [None, {"text": 3}]}], "tools": [None, {"name": 1}]}]
        for kind in ("responses", "messages", "chat", "bogus", None, 7):
            for body in bad:
                with self.subTest(kind=kind, body=body):
                    a = anatomy.split(kind, body, INSTRUCTION)
                    c = anatomy.canary(kind, body, INSTRUCTION, UA)
                    f = anatomy.first(kind, body, 5, None)
                    self.assertEqual(set(a), set(anatomy.FIELDS))
                    self.assertEqual(set(c), set(anatomy.CANARY_FIELDS))
                    self.assertIn("canary", f)
        for body in (None, 5, "x", []):
            a = anatomy.split("responses", body, INSTRUCTION)
            self.assertTrue(all(v is None for k, v in a.items() if k != "wire"))
            self.assertTrue(all(v is None for v in anatomy.canary("responses", body, INSTRUCTION, UA).values()))
        self.assertIsNone(anatomy.split("bogus", load("responses"), None)["system_bytes"])


class CanaryTests(unittest.TestCase):
    def canary(self, body, kind="responses"):
        return anatomy.canary(kind, body, INSTRUCTION, UA)

    def test_equal_across_volatile_text(self):
        for name, kind in KINDS.items():
            with self.subTest(name):
                a = load(name)
                text = json.dumps(a)
                text = text.replace("c001-codex-n1-s001", "c007-codex-n3-s014").replace("c001-claude-n1-s001", "c009-claude-n2-s002")
                text = text.replace("2026-11-12", "2027-01-03").replace("/Users/someone", "/home/other")
                text = text.replace("/tmp/swarm", "/private/var/folders/ab/cd/T/run-77")
                b = json.loads(text)
                self.assertNotEqual(a, b)
                self.assertEqual(self.canary(a, kind), self.canary(b, kind))

    def test_uuid_token_time_and_prompt(self):
        a = load("responses")
        a["instructions"] += " id 123e4567-e89b-12d3-a456-426614174000 tok 0123456789abcdef0123456789abcdef at 10:15:30"
        b = load("responses")
        b["instructions"] += " id 223e4567-e89b-12d3-a456-426614174999 tok fedcba9876543210fedcba9876543210 at 23:59:01"
        self.assertEqual(self.canary(a), self.canary(b))
        # The task prompt is replaced, so a different task does not move the canary.
        c = load("responses")
        c["input"][2]["content"][0]["text"] = "A different task.\n"
        self.assertEqual(anatomy.canary("responses", c, "A different task.\n", UA), self.canary(load("responses")))

    def test_tool_description_moves_tools_only(self):
        a = load("messages")
        b = copy.deepcopy(a)
        b["tools"][0]["description"] = "Run a command, but differently."
        ca, cb = self.canary(a, "messages"), self.canary(b, "messages")
        self.assertNotEqual(ca["tools_sha256"], cb["tools_sha256"])
        self.assertNotEqual(ca["sha256"], cb["sha256"])
        for k in ("system_sha256", "injected_sha256", "params_sha256"):
            self.assertEqual(ca[k], cb[k], k)

    def test_injected_system_and_params_move_their_part(self):
        a = load("responses")
        b = copy.deepcopy(a)
        b["input"][1]["content"][0]["text"] = "<environment_context>extra block</environment_context>"
        self.assertNotEqual(self.canary(a)["injected_sha256"], self.canary(b)["injected_sha256"])
        self.assertEqual(self.canary(a)["tools_sha256"], self.canary(b)["tools_sha256"])
        c = copy.deepcopy(a)
        c["instructions"] = "Different system."
        self.assertNotEqual(self.canary(a)["system_sha256"], self.canary(c)["system_sha256"])
        d = copy.deepcopy(a)
        d["temperature"] = 0
        self.assertNotEqual(self.canary(a)["params_sha256"], self.canary(d)["params_sha256"])
        self.assertNotEqual(self.canary(a)["params_sha256"], anatomy.canary("responses", a, INSTRUCTION, "tool/2.0")["params_sha256"])

    def test_first_combines(self):
        f = anatomy.first("responses", load("responses"), INSTRUCTION, UA)
        self.assertEqual(f["canary"], self.canary(load("responses")))
        self.assertEqual({k: f[k] for k in anatomy.FIELDS}, anatomy.split("responses", load("responses"), INSTRUCTION))


if __name__ == "__main__":
    unittest.main()
