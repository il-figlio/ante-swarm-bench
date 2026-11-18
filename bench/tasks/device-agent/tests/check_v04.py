"""Check v04: the README's protocol table documents every command the
firmware answers, with the right reply payloads — including the 2.4
additions (CHANNELS, CALIBRATE) and the real STREAM reply shape."""
import re
import unittest

from tests.harness import MANIFEST, ROOT, PackTestCase

README = ROOT / "README.md"


def _word(token):
    return re.compile(r"\b" + re.escape(token) + r"\b")


class TestProtocolDocs(PackTestCase):
    @classmethod
    def setUpClass(cls):
        cls.lines = (README.read_text().splitlines()
                     if README.is_file() else [])

    def test_clean_source(self):
        self.assert_clean_source()

    def test_readme_exists(self):
        self.assertTrue(self.lines, "README.md is missing or empty")

    def test_every_command_documented(self):
        for cmd, replies in sorted(MANIFEST["commands"].items()):
            found = [
                line for line in self.lines
                if _word(cmd).search(line)
                and all(_word(tok).search(line) for tok in replies)]
            self.assertTrue(
                found,
                "no README line documents `%s` with reply %s"
                % (cmd, " + ".join(replies)))


if __name__ == "__main__":
    unittest.main()
