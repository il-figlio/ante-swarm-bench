"""Check d04: a report.txt is written next to the database on every run,
in the specified format, with values matching the manifest."""
import re
import unittest
from pathlib import Path

from tests.etl import MANIFEST, PipelineCase, canon, expected

ROWS_RE = re.compile(r"^rows:\s*(\d+)\s*$", re.M)
TOTAL_RE = re.compile(r"^total_amount:\s*([0-9]+(?:\.[0-9]+)?)\s*$", re.M)
RANGE_RE = re.compile(
    r"^date_range:\s*(\d{4}-\d{2}-\d{2})\s+to\s+(\d{4}-\d{2}-\d{2})\s*$",
    re.M)
CAT_RE = re.compile(r"^\s{1,4}([^\s:][^:]*?):\s*(\d+)\s*$", re.M)


class TestReport(PipelineCase):
    FIXTURE = "basic.csv"

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.report_path = Path(cls.db).parent / "report.txt"
        cls.text = (cls.report_path.read_text(errors="replace")
                    if cls.report_path.is_file() else None)

    def _report(self):
        self.require_run()
        self.assertIsNotNone(
            self.text,
            "report.txt was not written next to the database")
        return self.text

    def test_table_loads(self):
        self.require_run()
        self.assertEqual(canon(self.rows), canon(expected(self.FIXTURE)))

    def test_report_exists_and_format(self):
        text = self._report()
        for label, rx in (("rows", ROWS_RE), ("total_amount", TOTAL_RE),
                          ("date_range", RANGE_RE)):
            self.assertIsNotNone(
                rx.search(text), "report.txt is missing a %r line" % label)
        self.assertIsNotNone(
            re.search(r"^by_category:\s*$", text, re.M),
            "report.txt is missing the by_category: line")

    def test_report_values(self):
        text = self._report()
        want = MANIFEST["fixtures"][self.FIXTURE]["report"]
        self.assertEqual(int(ROWS_RE.search(text).group(1)), want["rows"])
        self.assertEqual(TOTAL_RE.search(text).group(1),
                         want["total_amount"])
        got_range = RANGE_RE.search(text)
        self.assertEqual([got_range.group(1), got_range.group(2)],
                         want["date_range"])
        cats = {m.group(1): int(m.group(2))
                for m in CAT_RE.finditer(text.split("by_category:")[-1])}
        self.assertEqual(cats, want["by_category"],
                         "per-category counts differ")


if __name__ == "__main__":
    unittest.main()
