"""Check b04: `python3 report.py top` writes top-calls.csv — the 3 most
expensive calls of the current week, ranked, matching the manifest."""
import unittest

from tests import truth

HEADER = ["rank", "key", "team", "model", "ts",
          "cost_usd", "tokens_in", "tokens_out"]
CSV = truth.ROOT / "top-calls.csv"


class TestTopCalls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tampered = truth.tampered_inputs()
        cls.proc = truth.run_report("top")
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
            "`python3 report.py top` failed: %s"
            % (self.proc.stderr.strip()[-400:] or "no stderr"))

    def test_file_written(self):
        self.assertTrue(CSV.is_file(),
                        "report.py top did not write top-calls.csv")

    def test_header_and_count(self):
        if not self.rows:
            self.fail("top-calls.csv is empty or missing")
        self.assertEqual(
            self.rows[0], HEADER,
            "bad header — want %s, got %s" % (HEADER, self.rows[0]))
        self.assertEqual(
            len(self.rows), 4,
            "want exactly 3 data rows, got %d" % (len(self.rows) - 1))

    def test_rows_match_manifest(self):
        want = truth.MANIFEST["top_calls"]
        if len(self.rows) != 4:
            self.fail("wrong row count")
        fails = []
        for exp, row in zip(want, self.rows[1:]):
            if len(row) != len(HEADER):
                fails.append("bad row shape: %r" % row)
                continue
            if row[0] != str(exp["rank"]):
                fails.append("rank %s != %s" % (row[0], exp["rank"]))
            for i, field in ((1, "key"), (2, "team"), (3, "model"),
                             (4, "ts")):
                if row[i] != exp[field]:
                    fails.append("rank %s: %s=%r, manifest %r"
                                 % (exp["rank"], field, row[i],
                                    exp[field]))
            if not truth.MONEY2_RE.match(row[5]):
                fails.append("rank %s: cost_usd=%r not two decimals"
                             % (exp["rank"], row[5]))
            elif not truth.near(float(row[5]), [exp["cost_usd"]]):
                fails.append("rank %s: cost_usd=%s, manifest %s"
                             % (exp["rank"], row[5], exp["cost_usd"]))
            for i, field in ((6, "tokens_in"), (7, "tokens_out")):
                if row[i] != str(exp[field]):
                    fails.append("rank %s: %s=%r, manifest %r"
                                 % (exp["rank"], field, row[i],
                                    exp[field]))
        self.assertFalse(fails, "row mismatches: %s" % fails)


if __name__ == "__main__":
    unittest.main()
