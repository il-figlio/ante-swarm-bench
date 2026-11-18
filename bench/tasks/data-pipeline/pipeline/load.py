"""Load stage: write the cleaned rows into the orders table."""
import sqlite3
from pathlib import Path

TABLE = "orders"

SCHEMA = """\
CREATE TABLE orders (
    order_id TEXT,
    date     TEXT,
    customer TEXT,
    category TEXT,
    amount   REAL,
    note     TEXT
)"""

INSERT = "INSERT INTO orders VALUES (?, ?, ?, ?, ?, ?)"


def load(db_path, rows):
    """Create the orders table at ``db_path`` and insert ``rows``.

    Returns the number of rows written.
    """
    db_path = Path(db_path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(db_path))
    try:
        with con:
            con.execute(SCHEMA)
            con.executemany(INSERT, [
                (r["order_id"], r["date"], r["customer"], r["category"],
                 r["amount"], r["note"]) for r in rows])
    finally:
        con.close()
    return len(rows)
