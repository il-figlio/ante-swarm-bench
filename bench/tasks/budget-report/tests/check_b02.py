"""Check b02: `python3 report.py caps` writes over-cap.csv — exactly
the keys whose week spend exceeds their caps.json weekly cap, with
spend, cap and overage matching the manifest."""
import unittest

from tests import truth

HEADER = ["key", "team", "spend_usd", "cap_usd", "overage_usd"]
CSV = truth.ROOT / "over-cap.csv"


class TestOverCapCsv(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tampered = truth.tampered_inputs()
        cls.proc = truth.run_report("caps")
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
            "`python3 report.py caps` failed: %s"
            % (self.proc.stderr.strip()[-400:] or "no stderr"))

    def test_file_written(self):
        self.assertTrue(CSV.is_file(),
                        "report.py caps did not write over-cap.csv")

    def test_header(self):
        if not self.rows:
            self.fail("over-cap.csv is empty or missing")
        self.assertEqual(
            self.rows[0], HEADER,
            "bad header — want %s, got %s" % (HEADER, self.rows[0]))

    def test_exact_key_set_and_order(self):
        want = [o["key"] for o in truth.MANIFEST["over_cap"]]
        got = [r[0] for r in self.rows[1:]]
        self.assertEqual(
            got, want,
            "over-cap rows should be exactly these keys, alphabetical; "
            "missing %s, unexpected %s"
            % (sorted(set(want) - set(got)),
               sorted(set(got) - set(want))))

    def test_figures(self):
        want = {o["key"]: o for o in truth.MANIFEST["over_cap"]}
        fails = []
        for r in self.rows[1:]:
            if len(r) != len(HEADER):
                fails.append("bad row shape: %r" % r)
                continue
            exp = want.get(r[0])
            if exp is None:
                continue  # set test reports it
            if r[1] != exp["team"]:
                fails.append("%s: team=%s, manifest %s"
                             % (r[0], r[1], exp["team"]))
            for i, field in ((2, "spend_usd"), (3, "cap_usd"),
                             (4, "overage_usd")):
                if not truth.MONEY2_RE.match(r[i]):
                    fails.append("%s: %s=%r not two decimals"
                                 % (r[0], field, r[i]))
                elif not truth.near(float(r[i]), [exp[field]]):
                    fails.append("%s: %s=%s, manifest %s"
                                 % (r[0], field, r[i], exp[field]))
        self.assertFalse(fails, "figure mismatches: %s" % fails)


if __name__ == "__main__":
    unittest.main()
