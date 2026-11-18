"""Check t02: recover.json holds the rows recovered from trunc.db.

The expected (word, value) rows live in tests/manifest.json as sha256
digests of "word|repr(float(value))" so the answer isn't sitting in the
tree in plaintext. At least ``min_matches`` of them must appear.
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


def _token(word, value):
    return hashlib.sha256(
        ("%s|%r" % (word, float(value))).encode()).hexdigest()


class TestRecoverJson(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _skip_without("trunc.db", "recover.json")

    def test_json_data(self):
        p = ROOT / "recover.json"
        self.assertTrue(p.is_file(), "recover.json does not exist")
        data = json.loads(p.read_text())
        self.assertIsInstance(data, list)

        expected = set(MANIFEST["t02"]["entry_sha256"])
        got = set()
        for item in data:
            try:
                got.add(_token(item["word"], item["value"]))
            except (KeyError, TypeError, ValueError):
                continue
        score = len(expected & got)
        self.assertGreater(
            score, 6,
            "Did not load data correctly (%d rows matched)"
            % score)


if __name__ == "__main__":
    unittest.main()
