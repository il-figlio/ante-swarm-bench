"""argparse front end — ``python3 -m tally <command>``."""
import argparse
import sys

from .ledger import (append_row, categories, filter_month, load_rows,
                     parse_row)
from .report import render_categories, render_summary


def build_parser():
    """The argument parser, split out so tests can introspect it."""
    parser = argparse.ArgumentParser(
        prog="tally", description="summarise a CSV of expenses")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("summary", help="totals by category")
    p.add_argument("file", help="path to the ledger CSV")
    p.add_argument("--month", help="only rows in YYYY-MM")

    p = sub.add_parser("categories", help="list the categories in use")
    p.add_argument("file", help="path to the ledger CSV")

    p = sub.add_parser("add", help="append an expense to the ledger")
    p.add_argument("file", help="path to the ledger CSV")
    p.add_argument("--date", required=True, help="YYYY-MM-DD")
    p.add_argument("--amount", required=True, type=float)
    p.add_argument("--category", required=True)
    p.add_argument("--note", default="")
    return parser


def main(argv=None, out=None):
    """Dispatch one command. Returns a process exit code."""
    out = out or sys.stdout
    args = build_parser().parse_args(argv) 

    if args.command == 'summary':
        rows = load_rows(args.file)
        if args.month:
            rows = filter_month(rows, args.month)
        out.write(render_summary(rows, args.month))
    elif args.command == 'categories':
        out.write(render_categories(categories(load_rows(args.file))))
    elif args.command == 'add':
        record = parse_row({"date": args.date, "amount": str(args.amount),
                            "category": args.category, "note": args.note})
        append_row(args.file, record)
        out.write('added %.2f to %s\n' % (record["amount"],
                                          record["category"]))
    return 0
