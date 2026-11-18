"""Check d05: malformed export rows (wrong field count, non-numeric
amount, bad ISO date) are skipped, never loaded, and the run reports
`skipped N malformed rows` while still exiting 0."""
import re
import unittest

from tests.etl import MANIFEST, PipelineCase, canon, expected

SKIP_RE = re.compile(r"skipped\s+(\d+)\s+malformed", re.I)


class TestMalformedPolicy(PipelineCase):
    FIXTURE = "malformed.csv"

    def test_run_succeeds(self):
        self.require_run()

    def test_only_valid_rows_loaded(self):
        self.require_run()
        got, want = canon(self.rows), canon(expected(self.FIXTURE))
        self.assertEqual(
            got, want,
            "orders table differs from expected (valid rows only) — "
            "missing %s, extra %s"
            % (sorted(set(want) - set(got)), sorted(set(got) - set(want))))

    def test_skip_count_reported(self):
        self.require_run()
        out = (self.proc.stdout or "") + (self.proc.stderr or "")
        m = SKIP_RE.search(out)
        self.assertIsNotNone(
            m, "run did not print a 'skipped N malformed rows' line")
        want = MANIFEST["fixtures"][self.FIXTURE]["malformed_count"]
        self.assertEqual(
            int(m.group(1)), want,
            "reported %s skipped rows, expected %d" % (m.group(1), want))


if __name__ == "__main__":
    unittest.main()
