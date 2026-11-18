#!/usr/bin/env python3
"""Run the order-load pipeline end to end: CSV -> clean -> dedupe -> SQLite.

    python3 run.py                          # data/orders.csv -> out/orders.db
    python3 run.py --input x.csv --db y.db
"""
import argparse
import sys

from pipeline import extract, load, transform


def build_parser():
    parser = argparse.ArgumentParser(
        prog="run.py",
        description="load an order export CSV into a SQLite database")
    parser.add_argument("--input", default="data/orders.csv",
                        help="path to the order export CSV "
                             "(default: data/orders.csv)")
    parser.add_argument("--db", default="out/orders.db",
                        help="path to the SQLite database to write "
                             "(default: out/orders.db)")
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)

    records = extract.read_records(args.input)
    rows = transform.clean(records)
    rows = transform.dedupe(rows)
    n = load.load(args.db, rows)
    print("loaded %d rows into %s" % (n, args.db))
    return 0


if __name__ == "__main__":
    sys.exit(main())
