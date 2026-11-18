"""The repo's own suite (`make check`) — covers the stages that are
meant to work. Small, temp-file based, no fixtures needed."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from pipeline import extract, load, transform


def _records(res):
    """read_records returns the record dicts; if it also reports a
    skipped-row count (a (records, skipped) tuple), take the records."""
    return res[0] if isinstance(res, tuple) else res


class TestExtract(unittest.TestCase):
    def test_reads_simple_csv(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "in.csv"
            p.write_text("order_id,date\nA1,2026-09-01\nA2,2026-09-02\n")
            recs = _records(extract.read_records(str(p)))
        self.assertEqual(len(recs), 2)
        self.assertEqual(recs[0]["order_id"], "A1")
        self.assertEqual(recs[1]["date"], "2026-09-02")

    def test_empty_file(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "empty.csv"
            p.write_text("")
            self.assertEqual(_records(extract.read_records(str(p))), [])


class TestTransform(unittest.TestCase):
    def test_clean_coerces_amount_and_strips(self):
        rows = transform.clean([
            {"order_id": " A1 ", "date": "2026-09-01", "customer": "x",
             "category": "coffee", "amount": " 4.20 ", "note": " n "}])
        self.assertEqual(rows[0]["order_id"], "A1")
        self.assertEqual(rows[0]["amount"], 4.2)
        self.assertEqual(rows[0]["note"], "n")

    def test_clean_bad_amount_is_none(self):
        rows = transform.clean([
            {"order_id": "A1", "date": "2026-09-01", "customer": "x",
             "category": "coffee", "amount": "oops", "note": ""}])
        self.assertIsNone(rows[0]["amount"])


class TestLoad(unittest.TestCase):
    def test_round_trip(self):
        rows = [{"order_id": "A1", "date": "2026-09-01", "customer": "x",
                 "category": "coffee", "amount": 4.2, "note": "n"},
                {"order_id": "A2", "date": "2026-09-02", "customer": "y",
                 "category": "fun", "amount": 10.0, "note": ""}]
        with tempfile.TemporaryDirectory() as tmp:
            db = str(Path(tmp) / "o.db")
            self.assertEqual(load.load(db, rows), 2)
            con = sqlite3.connect(db)
            got = con.execute(
                "SELECT order_id, amount FROM orders ORDER BY order_id"
            ).fetchall()
            con.close()
        self.assertEqual(got, [("A1", 4.2), ("A2", 10.0)])


if __name__ == "__main__":
    unittest.main()
