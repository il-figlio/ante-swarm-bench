"""Check h04: CONTRIBUTING.md exists, is substantive, and tells a new
contributor the real things — the actual test command, the module
layout, the style gate, and the stdlib-only rule."""
import re
import unittest

from tests.hygiene import ROOT

CONTRIB = ROOT / "CONTRIBUTING.md"


def load(test):
    if not CONTRIB.is_file():
        test.fail("CONTRIBUTING.md does not exist")
    text = CONTRIB.read_text()
    if not text.strip():
        test.fail("CONTRIBUTING.md is empty")
    return text


class TestContributing(unittest.TestCase):
    def test_substantive(self):
        text = load(self)
        self.assertGreater(len(text), 400,
                           "CONTRIBUTING.md is a stub (%d chars)"
                           % len(text))

    def test_real_test_command(self):
        text = load(self)
        self.assertRegex(
            text, r"python3?\s+-m\s+unittest\s+discover",
            "CONTRIBUTING.md should name the real test command "
            "(python3 -m unittest discover ...) — there is no make "
            "target for the suite")

    def test_module_layout(self):
        text = load(self)
        self.assertIn("tally", text,
                      "CONTRIBUTING.md should describe the tally package")
        named = [m for m in ("cli.py", "ledger.py", "report.py")
                 if m in text]
        self.assertGreaterEqual(
            len(named), 2,
            "name the real modules (cli.py, ledger.py, report.py); "
            "found %s" % named)

    def test_style_rules(self):
        text = load(self)
        self.assertIn("check_style", text,
                      "mention the style gate tools/check_style.py")
        self.assertRegex(
            text, r"(?i)double[- ]quot",
            "spell out the style rules (double-quoted strings)")
        self.assertRegex(
            text, r"(?i)(4[- ]space|four spaces|indent)",
            "spell out the style rules (4-space indentation)")

    def test_stdlib_only(self):
        text = load(self)
        self.assertRegex(
            text, r"(?i)(standard[- ]library|stdlib|no (third[- ]party )?"
                  r"dependenc)",
            "CONTRIBUTING.md should state the stdlib-only rule")


if __name__ == "__main__":
    unittest.main()
