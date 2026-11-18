"""Check v01: `stats <chan> <n>` takes n READings of a channel and
prints one `STATS <chan> n=<n> min=<v> max=<v> avg=<v>` line built
from the values the pod actually returned."""
import re
import unittest

from tests.harness import MANIFEST, PackTestCase, run_client

LINE_RE = re.compile(
    r"^STATS (\S+) n=(\d+) min=(\S+) max=(\S+) avg=(\S+)\s*$")


class TestStats(PackTestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = run_client("stats", "temp", "5", scenario="clean")

    def test_clean_source(self):
        self.assert_clean_source()

    def test_exits_clean(self):
        self.assert_no_traceback(self.proc)
        self.assertEqual(self.proc.returncode, 0,
                         self.proc.stderr[-800:])

    def test_stats_line_shape(self):
        match = LINE_RE.search(self.proc.stdout)
        self.assertIsNotNone(
            match, "no `STATS <chan> n=<n> min=<v> max=<v> avg=<v>` "
                   "line in output: %r" % self.proc.stdout[-500:])
        self.assertEqual(match.group(1), "temp")
        self.assertEqual(match.group(2), "5")

    def test_stats_values(self):
        match = LINE_RE.search(self.proc.stdout)
        if not match:
            self.fail("no STATS line in output: %r"
                      % self.proc.stdout[-500:])
        values = MANIFEST["reads"]["temp"][:5]
        self.assertAlmostEqual(float(match.group(3)), min(values),
                               places=2)
        self.assertAlmostEqual(float(match.group(4)), max(values),
                               places=2)
        self.assertAlmostEqual(float(match.group(5)),
                               sum(values) / len(values), places=2)

    def test_existing_commands_still_work(self):
        proc = run_client("ping", scenario="clean")
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        self.assertIn("PONG", proc.stdout)


if __name__ == "__main__":
    unittest.main()
