"""Check b01: `python3 report.py spend` writes spend.csv — one row per
key in caps.json (idle keys included, zeros), one per team, a total
row, all for the current week only, every figure matching the manifest.
"""
import unittest

from tests import truth

HEADER = ["scope", "name", "team", "calls", "tokens_in",
          "tokens_out", "spend_usd"]
CSV = truth.ROOT / "spend.csv"


def expected_rows():
    rows = []
    for key in sorted(truth.MANIFEST["keys"]):
        k = truth.MANIFEST["keys"][key]
        rows.append(("key", key, k["team"], k))
    for team in sorted(truth.MANIFEST["teams"]):
        rows.append(("team", team, team, truth.MANIFEST["teams"][team]))
    rows.append(("total", "all", "all", truth.MANIFEST["totals"]))
    return rows


class TestSpendCsv(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tampered = truth.tampered_inputs()
        cls.proc = truth.run_report("spend")
        cls.rows = truth.csv_rows(CSV) if CSV.is_file() else []

    def test_inputs_untouched(self):
        self.assertFalse(
            self.tampered,
            "input files changed or missing — the exports, caps.json "
            "and prices.json are read-only evidence: %s" % self.tampered)

    def test_command_runs(self):
        self.assertIsNotNone(self.proc, "report.py does not exist")
        self.assertEqual(
            self.proc.returncode, 0,
            "`python3 report.py spend` failed: %s"
            % (self.proc.stderr.strip()[-400:] or "no stderr"))

    def test_file_written(self):
        self.assertTrue(CSV.is_file(),
                        "report.py spend did not write spend.csv")

    def test_header(self):
        if not self.rows:
            self.fail("spend.csv is empty or missing")
        self.assertEqual(
            self.rows[0], HEADER,
            "bad header — want %s, got %s" % (HEADER, self.rows[0]))

    def test_row_layout(self):
        """scope/name/team columns: all key rows alphabetical, then
        team rows alphabetical, then the total row — nothing else."""
        if len(self.rows) < 2:
            self.fail("spend.csv has no data rows")
        want = [(s, n, t) for s, n, t, _ in expected_rows()]
        got = [(r[0], r[1], r[2]) for r in self.rows[1:]
               if len(r) == len(HEADER)]
        badshape = [r for r in self.rows[1:] if len(r) != len(HEADER)]
        self.assertFalse(badshape, "rows with wrong column count: %s"
                         % badshape[:3])
        self.assertEqual(got, want,
                         "row layout mismatch — want key rows "
                         "alphabetical, then team rows, then total; "
                         "got %s" % got)

    def test_figures(self):
        if len(self.rows) < 2:
            self.fail("spend.csv has no data rows")
        got = {(r[0], r[1]): r for r in self.rows[1:]
               if len(r) == len(HEADER)}
        fails = []
        for scope, name, _, exp in expected_rows():
            row = got.get((scope, name))
            if row is None:
                fails.append("%s %s: row missing" % (scope, name))
                continue
            for i, field in ((3, "calls"), (4, "tokens_in"),
                             (5, "tokens_out")):
                if not truth.INT_RE.match(row[i]):
                    fails.append("%s %s: %s=%r not an integer"
                                 % (scope, name, field, row[i]))
                elif int(row[i]) != exp[field]:
                    fails.append("%s %s: %s=%s, manifest %s"
                                 % (scope, name, field, row[i],
                                    exp[field]))
            if not truth.MONEY2_RE.match(row[6]):
                fails.append("%s %s: spend_usd=%r not two decimals"
                             % (scope, name, row[6]))
            elif not truth.near(float(row[6]), [exp["spend_usd"]]):
                fails.append("%s %s: spend_usd=%s, manifest %s"
                             % (scope, name, row[6],
                                exp["spend_usd"]))
        self.assertFalse(fails, "figure mismatches: %s" % fails)


if __name__ == "__main__":
    unittest.main()
