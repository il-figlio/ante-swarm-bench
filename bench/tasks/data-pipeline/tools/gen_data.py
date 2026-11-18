#!/usr/bin/env python3
"""Generate data/orders.csv, tests/fixtures/*.csv and tests/manifest.json.

Deterministic (authored content, no clock or RNG): re-running rewrites
every output byte-identically.

    python3 tools/gen_data.py

data/orders.csv is the "production" input — a realistic export mixing
plain rows, standard CSV quoting (commas, escaped quotes, an embedded
newline) and a few upstream-retry duplicates. The files under
tests/fixtures/ each isolate one behaviour for a check: nasty quoting,
duplicates, a clean baseline, malformed rows.

The manifest records the ground truth a correct pipeline produces:
the expected ``orders`` table contents per fixture (deduped on order_id,
first occurrence kept; malformed rows skipped) and the report values
check_d04 diffs against report.txt.
"""
import csv
import io
import json
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
COLUMNS = ["order_id", "date", "customer", "category", "amount", "note"]

# ---------------------------------------------------------------- rows
# Each row is a list of raw CSV fields (strings). Rows that should be
# rejected by the malformed-row policy are authored too — ``expected``
# computes which survive.

QUOTED = [
    ["Q2001", "2026-09-01", "Lopez, Ana", "coffee", "4.20", "beans refill"],
    ["Q2002", "2026-09-02", "Marta R", "fun", "64.00", "2x mugs, gift-wrapped"],
    ["Q2003", "2026-09-03", "K. Osei", "transport", "18.75",
     "call before delivery\nleave at side door"],
    ["Q2004", "2026-09-04", "Chen, Wei", "groceries", "42.10",
     'she said "keep the receipt"'],
    ["Q2005", "2026-09-05", "Priya S", "coffee", "3.80",
     "flat white, extra shot"],
    ["Q2006", "2026-09-08", "Garcia, Tom", "fun", "15.00", "cinema"],
    ["Q2007", "2026-09-09", "Marta R", "groceries", "38.90",
     "weekly shop, top-up"],
    ["Q2008", "2026-09-10", "Lopez, Ana", "transport", "12.00", "taxi split"],
    ["Q2009", "2026-09-11", "K. Osei", "fun", "9.50",
     'rain check, "maybe later"'],
    ["Q2010", "2026-09-12", "Chen, Wei", "coffee", "4.20", "beans"],
]

DUPES = [
    ["D3001", "2026-09-01", "Ana Lopez", "coffee", "4.20", "beans"],
    ["D3002", "2026-09-01", "Tom Garcia", "groceries", "55.10",
     "weekly shop"],
    ["D3003", "2026-09-02", "Priya S", "transport", "18.75", "train"],
    ["D3004", "2026-09-02", "Wei Chen", "fun", "64.00", "climbing pass"],
    ["D3002", "2026-09-02", "Tom Garcia", "groceries", "55.10",
     "weekly shop RETRY"],
    ["D3005", "2026-09-03", "Marta R", "coffee", "3.80", "cortado"],
    ["D3006", "2026-09-04", "Ana Lopez", "fun", "15.00", "cinema"],
    ["D3003", "2026-09-05", "Priya S", "transport", "99.99",
     "duplicate resend"],
    ["D3007", "2026-09-05", "K. Osei", "groceries", "27.40", "top-up"],
    ["D3008", "2026-09-06", "Tom Garcia", "transport", "15.00", "bus card"],
    ["D3006", "2026-09-07", "Ana Lopez", "fun", "22.50", "cinema reupload"],
    ["D3009", "2026-09-08", "Wei Chen", "coffee", "4.20", "beans"],
    ["D3008", "2026-09-08", "Tom Garcia", "transport", "15.00",
     "bus card duplicate"],
    ["D3010", "2026-09-09", "Marta R", "groceries", "41.60", "weekly shop"],
]

BASIC = [
    ["B4001", "2026-08-02", "Ana Lopez", "groceries", "42.10",
     "weekly shop"],
    ["B4002", "2026-08-05", "Priya S", "coffee", "3.50", "flat white"],
    ["B4003", "2026-08-08", "Tom Garcia", "transport", "18.75",
     "train to brimley"],
    ["B4004", "2026-08-11", "Marta R", "groceries", "120.00",
     "bulk staples"],
    ["B4005", "2026-08-14", "Ana Lopez", "groceries", "9.20", "top-up"],
    ["B4006", "2026-08-17", "K. Osei", "fun", "64.00", "climbing gym pass"],
    ["B4007", "2026-08-21", "Priya S", "coffee", "3.80", "cortado"],
    ["B4008", "2026-08-25", "Tom Garcia", "groceries", "27.40",
     "weekly shop"],
    ["B4009", "2026-08-28", "Wei Chen", "transport", "15.00", "bus card"],
    ["B4010", "2026-08-30", "Ana Lopez", "fun", "41.60",
     "cinema + noodles"],
    ["B4011", "2026-09-03", "Priya S", "coffee", "4.20", "beans"],
    ["B4012", "2026-09-06", "Tom Garcia", "transport", "12.00",
     "taxi split"],
]

MALFORMED = [
    ["M5001", "2026-09-01", "Ana Lopez", "coffee", "4.20", "beans"],
    ["M5002", "2026-09-01", "Tom Garcia", "groceries", "55.10",
     "weekly shop"],
    ["M5003", "2026-09-02", "Priya S", "transport", "18.75", "train"],
    # wrong field count: one field too many
    ["M5004", "2026-09-02", "Wei Chen", "fun", "64.00", "climbing pass",
     "EXTRA-FIELD"],
    # wrong field count: note column missing entirely
    ["M5005", "2026-09-03", "Marta R", "coffee", "3.80"],
    ["M5006", "2026-09-03", "Ana Lopez", "fun", "15.00", "cinema"],
    # amount is not a number
    ["M5007", "2026-09-04", "K. Osei", "groceries", "oops", "top-up"],
    ["M5008", "2026-09-05", "Tom Garcia", "transport", "15.00", "bus card"],
    ["M5009", "2026-09-06", "Wei Chen", "coffee", "4.20", "beans"],
    ["M5010", "2026-09-07", "Marta R", "groceries", "41.60",
     "weekly shop"],
    # date is not ISO YYYY-MM-DD
    ["M5011", "not-a-date", "Priya S", "transport", "9.90", "refund"],
    ["M5012", "2026-09-08", "K. Osei", "fun", "12.50", "board game"],
    ["M5013", "2026-09-09", "Ana Lopez", "coffee", "3.50", "flat white"],
]

ORDERS = [
    ["A1001", "2026-08-31", "Lopez, Ana", "groceries", "55.10",
     "weekly shop"],
    ["A1002", "2026-09-01", "Marta R", "coffee", "4.20", "beans refill"],
    ["A1003", "2026-09-01", "Chen, Wei", "transport", "18.75",
     "train, return leg"],
    ["A1004", "2026-09-02", "K. Osei", "fun", "64.00",
     "climbing gym pass"],
    ["A1005", "2026-09-02", "Garcia, Tom", "groceries", "9.20", "top-up"],
    ["A1002", "2026-09-03", "Marta R", "coffee", "4.20",
     "beans refill (resend)"],
    ["A1006", "2026-09-03", "Priya S", "coffee", "3.80",
     "flat white, extra shot"],
    ["A1007", "2026-09-04", "Lopez, Ana", "fun", "15.00",
     "cinema + noodles"],
    ["A1008", "2026-09-04", "Marta R", "groceries", "38.90",
     "weekly shop, top-up"],
    ["A1009", "2026-09-05", "Chen, Wei", "transport", "12.00",
     "taxi split"],
    ["A1010", "2026-09-05", "K. Osei", "coffee", "4.20", "beans"],
    ["A1011", "2026-09-06", "Garcia, Tom", "fun", "9.50",
     'rain check, "maybe later"'],
    ["A1012", "2026-09-07", "Priya S", "groceries", "27.40",
     "weekly shop"],
    ["A1013", "2026-09-08", "Lopez, Ana", "transport", "15.00",
     "bus card"],
    ["A1014", "2026-09-08", "Marta R", "fun", "41.60",
     "dinner, set menu"],
    ["A1015", "2026-09-09", "Chen, Wei", "coffee", "3.50",
     "cortado"],
    ["A1009", "2026-09-09", "Chen, Wei", "transport", "12.00",
     "taxi split RETRY"],
    ["A1016", "2026-09-10", "K. Osei", "groceries", "41.60",
     "weekly shop"],
    ["A1017", "2026-09-10", "Garcia, Tom", "coffee", "4.20", "beans"],
    ["A1018", "2026-09-11", "Priya S", "fun", "22.50", "climbing, guest"],
    ["A1019", "2026-09-11", "Lopez, Ana", "transport", "9.90",
     "bus top-up"],
    ["A1020", "2026-09-12", "Marta R", "groceries", "120.00",
     "bulk staples"],
    ["A1021", "2026-09-12", "Chen, Wei", "fun", "15.00", "cinema"],
    ["A1022", "2026-09-13", "K. Osei", "coffee", "3.80",
     "flat white"],
    ["A1023", "2026-09-14", "Garcia, Tom", "groceries", "33.75",
     "weekly shop"],
    ["A1024", "2026-09-14", "Priya S", "transport", "18.75", "train"],
    ["A1025", "2026-09-15", "Lopez, Ana", "coffee", "4.20",
     "beans, dark roast"],
    ["A1026", "2026-09-15", "Marta R", "fun", "12.50",
     "board game night"],
    ["A1027", "2026-09-16", "Chen, Wei", "groceries", "8.40",
     "top-up"],
    ["A1028", "2026-09-16", "K. Osei", "transport", "6.00",
     "metro card"],
    ["A1014", "2026-09-17", "Marta R", "fun", "41.60",
     "dinner, set menu (duplicate)"],
    ["A1029", "2026-09-17", "Garcia, Tom", "coffee", "3.50",
     "flat white"],
    ["A1030", "2026-09-18", "Priya S", "groceries", "21.30",
     "weekly shop"],
]

# ------------------------------------------------------------- helpers


def _is_iso_date(value):
    """True for strict YYYY-MM-DD with valid month/day ranges."""
    parts = value.split("-")
    if len(parts) != 3 or [len(p) for p in parts] != [4, 2, 2]:
        return False
    try:
        y, m, d = (int(p) for p in parts)
    except ValueError:
        return False
    return 1 <= m <= 12 and 1 <= d <= 31 and y >= 1000


def malformed_reason(fields):
    """Why a raw row is malformed, or None if it is loadable."""
    if len(fields) != len(COLUMNS):
        return "field_count"
    try:
        float(fields[4].strip())
    except ValueError:
        return "amount"
    if not _is_iso_date(fields[1].strip()):
        return "date"
    return None


def clean_row(fields):
    """What the transform stage makes of a well-formed raw row."""
    order_id, date, customer, category, amount, note = (
        f.strip() for f in fields)
    return [order_id, date, customer, category, float(amount), note]


def expected_rows(rows):
    """The orders table a correct pipeline loads: well-formed rows,
    cleaned, deduped on order_id keeping the first occurrence."""
    seen = set()
    out = []
    for fields in rows:
        if malformed_reason(fields) is not None:
            continue
        row = clean_row(fields)
        if row[0] in seen:
            continue
        seen.add(row[0])
        out.append(row)
    return out


def report_values(rows):
    """The report.txt contents check_d04 expects for `rows`."""
    amounts = [r[4] for r in rows]
    dates = sorted(r[1] for r in rows)
    cats = {}
    for r in rows:
        cats[r[3]] = cats.get(r[3], 0) + 1
    return {
        "rows": len(rows),
        "total_amount": "%.2f" % sum(amounts),
        "date_range": [dates[0], dates[-1]],
        "by_category": dict(sorted(cats.items())),
    }


def write_csv(path, rows):
    buf = io.StringIO()
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    w.writerows(rows)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(buf.getvalue())


def main():
    write_csv(ROOT / "data" / "orders.csv", ORDERS)

    fixtures = {
        "quoted.csv": QUOTED,
        "dupes.csv": DUPES,
        "basic.csv": BASIC,
        "malformed.csv": MALFORMED,
    }
    for name, rows in fixtures.items():
        write_csv(ROOT / "tests" / "fixtures" / name, rows)

    manifest = {
        "note": "generated by tools/gen_data.py — do not edit",
        "table": "orders",
        "columns": COLUMNS,
        "fixtures": {},
    }
    for name, rows in fixtures.items():
        exp = expected_rows(rows)
        counts = Counter(f[0] for f in rows
                         if malformed_reason(f) is None)
        entry = {
            "input_rows": len(rows),
            "expected_rows": exp,
            "malformed_count": sum(
                1 for f in rows if malformed_reason(f) is not None),
            "malformed_ids": [f[0] for f in rows
                              if malformed_reason(f) is not None],
            "duplicate_ids": sorted(k for k, v in counts.items()
                                    if v > 1),
        }
        if name == "basic.csv":
            entry["report"] = report_values(exp)
        manifest["fixtures"][name] = entry
    manifest["data/orders.csv"] = {
        "input_rows": len(ORDERS),
        "expected_rows": expected_rows(ORDERS),
    }

    out = ROOT / "tests" / "manifest.json"
    out.write_text(json.dumps(manifest, indent=1, sort_keys=True) + "\n")
    print("wrote data/orders.csv, %d fixtures, tests/manifest.json"
          % len(fixtures))


if __name__ == "__main__":
    main()
