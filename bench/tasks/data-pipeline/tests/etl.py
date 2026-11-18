"""Shared helpers for the data-pipeline checks.

Every check runs the real entry point — ``run.py --input <fixture>
--db <tmp db>`` — in a subprocess against a fixture under
``tests/fixtures/`` (tracked, so the harness restores it) and compares
the resulting ``orders`` table to ``tests/manifest.json`` ground truth.
"""
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = json.loads((ROOT / "tests" / "manifest.json").read_text())
FIXTURES = ROOT / "tests" / "fixtures"


def fixture(name):
    return str(FIXTURES / name)


def run_pipeline(input_csv, db_path):
    """Run run.py end-to-end; returns the CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(ROOT / "run.py"),
         "--input", str(input_csv), "--db", str(db_path)],
        capture_output=True, text=True, timeout=60, cwd=ROOT)


def table_rows(db_path):
    """Every row of the orders table, as lists."""
    con = sqlite3.connect(str(db_path))
    try:
        return [list(r) for r in con.execute("SELECT * FROM orders")]
    finally:
        con.close()


def canon(rows):
    """Sorted canonical JSON strings for a set of table rows — safe to
    compare whatever types land in the columns."""
    return sorted(json.dumps(r, sort_keys=True) for r in rows)


def expected(fixture_name):
    """The manifest's expected orders-table rows for a fixture."""
    return MANIFEST["fixtures"][fixture_name]["expected_rows"]


def content_hash(db_path):
    """sha256 over the canonical table contents."""
    return hashlib.sha256(
        "\n".join(canon(table_rows(db_path))).encode()).hexdigest()


class PipelineCase(unittest.TestCase):
    """Base for checkers: run the pipeline into a temp db once."""

    FIXTURE = None

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.addClassCleanup(cls.tmp.cleanup)
        cls.db = str(Path(cls.tmp.name) / "out.db")
        cls.proc = run_pipeline(fixture(cls.FIXTURE), cls.db)
        cls.rows = (table_rows(cls.db)
                    if cls.proc.returncode == 0 and Path(cls.db).exists()
                    else None)

    def require_run(self):
        """Fail with the pipeline's stderr when the run didn't exit 0."""
        self.assertEqual(
            self.proc.returncode, 0,
            "run.py exited %d:\n%s"
            % (self.proc.returncode,
               (self.proc.stderr or "")[-800:]))
        self.assertIsNotNone(self.rows, "no orders table was written")
