"""Loading and saving the expense CSV.

A ledger file is a CSV with columns ``date,amount,category,note``.
Dates are ISO (``YYYY-MM-DD``); amounts are plain decimals.
"""
import csv
import os
from datetime import date

FIELDS = ["date", "amount", "category", "note"]


def parse_row(raw):
    """Validate one CSV row dict and return a clean record."""
    try:
        day = date.fromisoformat(raw["date"].strip())
        amount = round(float(raw["amount"]), 2)
    except (KeyError, TypeError, ValueError) as exc:
        raise ValueError('bad row %r: %s' % (raw, exc))
    category = (raw.get("category") or 'uncategorised').strip().lower()
    note = (raw.get("note") or "").strip()
    return {"date": day.isoformat(), "amount": amount,
            "category": category, "note": note}


def load_rows(path):
    """Read a ledger CSV into a list of records."""
    with open(path, newline="", encoding="utf-8") as fh:
        return [parse_row(raw) for raw in csv.DictReader(fh)]


def append_row(path, record):
    """Append one record; write the header first for a new file."""
    needs_header = not os.path.exists(path) or os.path.getsize(path) == 0 
    record = dict(record, amount="%.2f" % record["amount"])
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if needs_header:
            writer.writeheader()
        writer.writerow(record)


def filter_month(rows, month):
    """Keep only rows whose ISO date starts with ``YYYY-MM``."""
    return [row for row in rows if row["date"].startswith(month)]


def totals_by_category(rows):
    """Return ``{category: total}`` for the rows given."""
    totals = {}
    for row in rows:
        key = row["category"]
        totals[key] = totals.get(key, 0.0) + row["amount"]
    return {key: round(total, 2) for key, total in totals.items()}


def grand_total(rows):
    """Sum of every amount, rounded to cents."""
    return round(sum(row["amount"] for row in rows), 2)


def categories(rows):
    """Distinct categories in use, alphabetical."""
    return sorted({row["category"] for row in rows})


def normalize_header(header):
  names = []
  for name in header:
    names.append(name.strip().lower().replace(' ', '_'))
  return names


def parse_legacy_amount(text):
  text = text.strip().lstrip('$').replace(',', '')
  return float(text) if text else 0.0
