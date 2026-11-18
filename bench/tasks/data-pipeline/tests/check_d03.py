"""Check d03: the pipeline is idempotent — a second run against the same
database exits cleanly and leaves exactly the same state (row count and
content hash) as the first."""
import unittest

from tests.etl import (PipelineCase, canon, content_hash, expected,
                       fixture, run_pipeline, table_rows)


class TestIdempotent(PipelineCase):
    FIXTURE = "basic.csv"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.first_hash = (content_hash(cls.db)
                          if cls.proc.returncode == 0 else None)
        cls.proc2 = run_pipeline(fixture(cls.FIXTURE), cls.db)
        cls.second_rows = (table_rows(cls.db)
                           if cls.proc2.returncode == 0 else None)

    def test_first_run_succeeds(self):
        self.assertEqual(
            self.proc.returncode, 0,
            "first run exited %d:\n%s"
            % (self.proc.returncode, (self.proc.stderr or "")[-800:]))

    def test_second_run_succeeds(self):
        self.assertEqual(
            self.proc2.returncode, 0,
            "second run exited %d:\n%s"
            % (self.proc2.returncode, (self.proc2.stderr or "")[-800:]))

    def test_state_unchanged_by_second_run(self):
        self.assertIsNotNone(self.first_hash, "first run produced no db")
        self.assertIsNotNone(self.second_rows, "second run produced no db")
        self.assertEqual(
            canon(self.second_rows), canon(self.rows),
            "the second run changed the table (rows doubled or replaced)")

    def test_final_table_matches_manifest(self):
        self.assertIsNotNone(self.second_rows, "second run produced no db")
        got, want = canon(self.second_rows), canon(expected(self.FIXTURE))
        self.assertEqual(
            got, want,
            "orders table differs from expected — missing %s, extra %s"
            % (sorted(set(want) - set(got)), sorted(set(got) - set(want))))


if __name__ == "__main__":
    unittest.main()
