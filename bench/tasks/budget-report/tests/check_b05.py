"""Check b05: `python3 report.py project` writes projection.txt —
exactly `daily_rate_usd=NN.NN` and `projected_october_usd=NN.NN` lines,
matching the manifest (either rounding path accepted)."""
import re
import unittest

from tests import truth

OUT = truth.ROOT / "projection.txt"
RATE_RE = re.compile(r"^daily_rate_usd=([0-9]+\.[0-9]+)\s*$")
PROJ_RE = re.compile(r"^projected_october_usd=([0-9]+\.[0-9]+)\s*$")


def expected_projection():
    """Both defensible derivations: raw rate × 31, or the displayed
    2-decimal rate × 31."""
    t = truth.MANIFEST["totals"]["spend_usd"]
    return {round(t / 7 * 31, 2), round(round(t / 7, 2) * 31, 2)}


class TestProjection(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tampered = truth.tampered_inputs()
        cls.proc = truth.run_report("project")
        cls.lines = (OUT.read_text(errors="replace").splitlines()
                     if OUT.is_file() else [])
        cls.lines = [l for l in cls.lines if l.strip()]

    def test_inputs_untouched(self):
        self.assertFalse(
            self.tampered,
            "input files changed or missing — the exports, caps.json "
            "and prices.json are read-only evidence: %s" % self.tampered)

    def test_command_runs(self):
        self.assertIsNotNone(self.proc, "report.py does not exist")
        self.assertEqual(
            self.proc.returncode, 0,
            "`python3 report.py project` failed: %s"
            % (self.proc.stderr.strip()[-400:] or "no stderr"))

    def test_file_written(self):
        self.assertTrue(OUT.is_file(),
                        "report.py project did not write projection.txt")

    def test_exactly_two_lines(self):
        self.assertEqual(
            len(self.lines), 2,
            "want exactly two lines "
            "(daily_rate_usd=… / projected_october_usd=…), got %r"
            % self.lines)

    def test_daily_rate(self):
        if len(self.lines) != 2:
            self.fail("wrong line count")
        m = RATE_RE.match(self.lines[0])
        self.assertIsNotNone(
            m, "first line must be daily_rate_usd=NN.NN, got %r"
            % self.lines[0])
        want = truth.MANIFEST["totals"]["daily_rate_usd"]
        self.assertTrue(
            truth.near(float(m.group(1)), [want]),
            "daily_rate_usd=%s, manifest %s" % (m.group(1), want))

    def test_projection(self):
        if len(self.lines) != 2:
            self.fail("wrong line count")
        m = PROJ_RE.match(self.lines[1])
        self.assertIsNotNone(
            m, "second line must be projected_october_usd=NN.NN, got %r"
            % self.lines[1])
        want = expected_projection()
        self.assertTrue(
            truth.near(float(m.group(1)), want),
            "projected_october_usd=%s, manifest %s"
            % (m.group(1), sorted(want)))


if __name__ == "__main__":
    unittest.main()
