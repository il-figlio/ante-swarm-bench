"""Check f04: tests/test_note_404.py exists, covers the GET /notes/<id>
404 behavior, and passes under unittest."""
import os
import subprocess
import sys
import unittest

MISSING = os.path.join("tests", "test_note_404.py")


class TestMissingTestFile(unittest.TestCase):
    def test_file_exists(self):
        self.assertTrue(os.path.isfile(MISSING),
                        "tests/test_note_404.py does not exist")

    def test_file_covers_the_behavior(self):
        if not os.path.isfile(MISSING):
            self.fail("tests/test_note_404.py does not exist")
        with open(MISSING) as fh:
            src = fh.read()
        self.assertRegex(src, r"def test_\w+\(",
                         "no test methods in test_note_404.py")
        self.assertIn("/notes", src)
        self.assertIn("404", src)

    def test_file_passes_under_unittest(self):
        if not os.path.isfile(MISSING):
            self.fail("tests/test_note_404.py does not exist")
        proc = subprocess.run(
            [sys.executable, "-m", "unittest", "tests.test_note_404", "-q"],
            capture_output=True, text=True, timeout=120)
        out = proc.stdout + proc.stderr
        self.assertRegex(out, r"Ran [1-9]\d* test",
                         "no tests ran: %s" % out[-1000:])
        self.assertEqual(proc.returncode, 0, out[-3000:])
        self.assertIn("OK", out)


if __name__ == "__main__":
    unittest.main()
