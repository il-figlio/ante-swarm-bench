"""Check h05: the README is back in sync with the code — the phantom
`export` command and `make test` are gone, and everything the README
still tells a user to run is real."""
import re
import unittest

from tests.hygiene import ROOT

README = ROOT / "README.md"


def load(test):
    if not README.is_file():
        test.fail("README.md does not exist")
    return README.read_text()


class TestReadmeSync(unittest.TestCase):
    def test_phantom_export_gone(self):
        text = load(self)
        self.assertIsNone(
            re.search(r"(?i)\bexport\b", text),
            "README still documents the removed `export` command")
        self.assertNotIn(
            "--format", text,
            "README still documents the removed --format flag")

    def test_phantom_make_test_gone(self):
        text = load(self)
        self.assertIsNone(
            re.search(r"make\s+test\b", text),
            "README still says `make test` — there is no such target")

    def test_real_commands_documented(self):
        text = load(self)
        self.assertIn("python3 -m tally", text,
                      "README should still show how to run the CLI")
        for cmd in ("summary", "categories", "add"):
            self.assertRegex(
                text, r"\b%s\b" % cmd,
                "README dropped the real `%s` command" % cmd)
        self.assertIn("--month", text,
                      "README dropped the real --month flag")

    def test_real_test_command_documented(self):
        text = load(self)
        self.assertRegex(
            text, r"python3?\s+-m\s+unittest\s+discover",
            "the suite exists — the README should name the real test "
            "command instead of the phantom `make test`")


if __name__ == "__main__":
    unittest.main()
