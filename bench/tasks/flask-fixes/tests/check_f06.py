"""Check f06: `make check` runs the shipped suite and is green."""
import subprocess
import unittest


class TestMakeCheck(unittest.TestCase):
    def test_make_check_passes(self):
        proc = subprocess.run(["make", "check"],
                              capture_output=True, text=True, timeout=180)
        out = proc.stdout + proc.stderr
        self.assertEqual(proc.returncode, 0, out[-3000:])
        self.assertIn("OK", proc.stderr,
                      "unittest did not report OK: %s" % out[-1000:])


if __name__ == "__main__":
    unittest.main()
