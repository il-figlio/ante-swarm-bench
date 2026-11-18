"""Check d01: quoted CSV fields — commas, escaped quotes, embedded
newlines — parse correctly end to end. The check runs the pipeline on
tests/fixtures/quoted.csv and diffs the whole orders table against the
manifest."""
import unittest

from tests.etl import PipelineCase, canon, expected


class TestQuotedFields(PipelineCase):
    FIXTURE = "quoted.csv"

    def test_run_succeeds(self):
        self.require_run()

    def test_table_matches_manifest(self):
        self.require_run()
        got, want = canon(self.rows), canon(expected(self.FIXTURE))
        self.assertEqual(
            got, want,
            "orders table differs from expected — missing %s, extra %s"
            % (sorted(set(want) - set(got)), sorted(set(got) - set(want))))

    def test_nasty_field_values_survive(self):
        self.require_run()
        notes = {r[0]: r[5] for r in self.rows}
        self.assertIn("\n", notes.get("Q2003", ""),
                      "embedded newline in Q2003's note was lost")
        self.assertIn(",", notes.get("Q2002", ""),
                      "embedded comma in Q2002's note was lost")
        self.assertIn('"', notes.get("Q2004", ""),
                      "escaped quote in Q2004's note was lost")
        customers = {r[0]: r[2] for r in self.rows}
        self.assertEqual(customers.get("Q2001"), "Lopez, Ana",
                         "quoted customer name was split")


if __name__ == "__main__":
    unittest.main()
