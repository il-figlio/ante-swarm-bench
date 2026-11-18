"""Check d02: duplicate order_ids are dropped, first occurrence in file
order kept. The check runs the pipeline on tests/fixtures/dupes.csv and
diffs the orders table against the manifest."""
import unittest
from collections import Counter

from tests.etl import MANIFEST, PipelineCase, canon, expected


class TestDedupe(PipelineCase):
    FIXTURE = "dupes.csv"

    def test_run_succeeds(self):
        self.require_run()

    def test_no_duplicate_order_ids(self):
        self.require_run()
        dupes = [k for k, v in Counter(r[0] for r in self.rows).items()
                 if v > 1]
        self.assertFalse(dupes, "order_ids still duplicated: %s" % dupes)

    def test_table_matches_manifest(self):
        self.require_run()
        got, want = canon(self.rows), canon(expected(self.FIXTURE))
        self.assertEqual(
            got, want,
            "orders table differs from expected (first occurrences kept) "
            "— missing %s, extra %s"
            % (sorted(set(want) - set(got)), sorted(set(got) - set(want))))

    def test_first_occurrence_kept(self):
        self.require_run()
        # D3003's resend carried amount 99.99; the first send was 18.75.
        amounts = {r[0]: r[4] for r in self.rows}
        self.assertEqual(
            amounts.get("D3003"), 18.75,
            "D3003 kept the resent copy (99.99), not the first (18.75)")


if __name__ == "__main__":
    unittest.main()
