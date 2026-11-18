"""Check h03: tools/check_style.py exits clean AND the code is
semantically unchanged — each pre-existing file's fingerprint still
matches the manifest (ast.dump hash, with the normalized token stream
as a cross-version fallback), and the suite still passes."""
import subprocess
import sys
import unittest

from tests.hygiene import MANIFEST, ROOT, ast_hash, run_suite, token_hash


class TestStyleReformat(unittest.TestCase):
    def test_style_gate_is_clean(self):
        proc = subprocess.run(
            [sys.executable, "tools/check_style.py"],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(
            proc.returncode, 0,
            "tools/check_style.py still reports violations:\n%s"
            % (proc.stdout + proc.stderr)[-3000:])

    def test_behaviour_unchanged(self):
        for name, want in MANIFEST["ast_hashes"].items():
            path = ROOT / name
            self.assertTrue(path.is_file(), "%s is missing" % name)
            same = (ast_hash(path) == want
                    or token_hash(path)
                    == MANIFEST["token_hashes"][name])
            self.assertTrue(
                same, "%s changed semantically — the task was a pure "
                      "reformat, no behaviour changes" % name)

    def test_suite_still_passes(self):
        proc = run_suite()
        self.assertEqual(
            proc.returncode, 0,
            "the repo test suite fails:\n%s"
            % (proc.stdout + proc.stderr)[-2000:])


if __name__ == "__main__":
    unittest.main()
