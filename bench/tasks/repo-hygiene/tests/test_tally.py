"""The suite. Run it with:

    python3 -m unittest discover -s tests -t . -p 'test_*.py'

There is no Makefile target for it (yet).
"""
import io
import tempfile
import unittest
from pathlib import Path

from tally.cli import main as cli_main
from tally.ledger import (append_row, categories, filter_month, grand_total,
                          load_rows, parse_row, totals_by_category)
from tally.report import money, render_categories, render_summary

ROWS_CSV = (
    "date,amount,category,note\n"
    "2026-08-02,42.10,groceries,weekly shop\n"
    "2026-08-05,3.50,coffee,flat white\n"
    "2026-08-11,1200.00,rent,august\n"
    "2026-09-01,4.20,coffee,beans\n"
)


def write_csv(tmp, text=ROWS_CSV):
    path = Path(tmp) / "expenses.csv"
    path.write_text(text)
    return str(path)


class TestLedger(unittest.TestCase):
    def test_parse_row_normalises(self):
        row = parse_row({"date": "2026-08-02", "amount": "42.1",
                         "category": " Groceries ", "note": " shop "})
        self.assertEqual(row["date"], "2026-08-02")
        self.assertEqual(row["amount"], 42.1)
        self.assertEqual(row["category"], "groceries")
        self.assertEqual(row["note"], "shop")

    def test_parse_row_rejects_bad_date(self):
        with self.assertRaises(ValueError):
            parse_row({"date": "August 2", "amount": "1.00"})

    def test_load_and_filter_month(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = load_rows(write_csv(tmp))
            self.assertEqual(len(rows), 4)
            august = filter_month(rows, "2026-08")
            self.assertEqual(len(august), 3)

    def test_totals(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = filter_month(load_rows(write_csv(tmp)), "2026-08")
            self.assertEqual(totals_by_category(rows),
                             {"groceries": 42.10, "coffee": 3.50,
                              "rent": 1200.00})
            self.assertEqual(grand_total(rows), 1245.60)

    def test_append_row_writes_header_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = str(Path(tmp) / "new.csv")
            append_row(path, parse_row({"date": "2026-09-10",
                                        "amount": "4.2",
                                        "category": "coffee",
                                        "note": "beans"}))
            append_row(path, parse_row({"date": "2026-09-11",
                                        "amount": "1.0",
                                        "category": "misc", "note": ""}))
            rows = load_rows(path)
            self.assertEqual([r["note"] for r in rows], ["beans", ""])
            self.assertEqual(rows[0]["amount"], 4.2)

    def test_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(categories(load_rows(write_csv(tmp))),
                             ["coffee", "groceries", "rent"])


class TestReport(unittest.TestCase):
    def test_money(self):
        self.assertEqual(money(3.5), "3.50")
        self.assertEqual(money(1200), "1200.00")

    def test_render_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            rows = load_rows(write_csv(tmp))
            out = render_summary(rows)
            self.assertIn("coffee", out)
            self.assertIn("TOTAL", out)
            self.assertIn("1249.80", out)
            out = render_summary(rows, month="2026-08")
            self.assertIn("TOTAL 2026-08", out)

    def test_render_categories(self):
        self.assertEqual(render_categories(["b", "a"]), "a\nb\n")
        self.assertEqual(render_categories([]), "no expenses yet\n")


class TestCli(unittest.TestCase):
    def run_cli(self, argv, csv_path=None):
        out = io.StringIO()
        rc = cli_main(argv, out=out)
        return rc, out.getvalue()

    def test_summary(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(tmp)
            rc, out = self.run_cli(["summary", path])
            self.assertEqual(rc, 0)
            self.assertIn("TOTAL", out)
            rc, out = self.run_cli(["summary", path, "--month", "2026-09"])
            self.assertIn("4.20", out)
            self.assertNotIn("42.10", out)

    def test_categories(self):
        with tempfile.TemporaryDirectory() as tmp:
            rc, out = self.run_cli(["categories", write_csv(tmp)])
            self.assertEqual(rc, 0)
            self.assertIn("groceries", out)

    def test_add(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_csv(tmp)
            rc, out = self.run_cli(
                ["add", path, "--date", "2026-09-10", "--amount", "9.99",
                 "--category", "books", "--note", "paperback"])
            self.assertEqual(rc, 0)
            self.assertIn("books", out)
            rows = load_rows(path)
            self.assertEqual(rows[-1]["category"], "books")
            self.assertEqual(rows[-1]["amount"], 9.99)


if __name__ == "__main__":
    unittest.main()
