"""Check t03: output.txt is the exact call-site report for log.stack.

The source task compared against a reference file line-by-line; here the
reference is stored as a sha256 of the canonical text (lines rstripped,
file stripped) in tests/manifest.json so the expected report is not
readable in the tree.
"""
import hashlib
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests" / "manifest.json").read_text())


def _skip_without(inp, out):
    """The input is fetched by fetch_inputs.py, not committed: skip, not fail, when it was never fetched."""
    if not (ROOT / inp).exists() and not (ROOT / out).exists():
        raise unittest.SkipTest("%s is not present; run fetch_inputs.py (see ATTRIBUTION.md)" % inp)


def _canonical(text):
    return "\n".join(line.rstrip() for line in text.strip().splitlines())


class TestCountOutput(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _skip_without("log.stack", "output.txt")

    def test_output_file(self):
        p = ROOT / "output.txt"
        self.assertTrue(p.is_file(), "output.txt does not exist")
        text = p.read_text()
        self.assertTrue(
            text.lstrip().startswith("Found "),
            "output.txt does not start with the 'Found N stack traces' "
            "line")

    def test_output_matches_expected(self):
        p = ROOT / "output.txt"
        self.assertTrue(p.is_file(), "output.txt does not exist")
        digest = hashlib.sha256(
            _canonical(p.read_text()).encode()).hexdigest()
        self.assertEqual(
            digest, MANIFEST["t03"]["output_sha256"],
            "output.txt does not match the expected report "
            "(counts, ordering, and frame lines must be exact)")


if __name__ == "__main__":
    unittest.main()
