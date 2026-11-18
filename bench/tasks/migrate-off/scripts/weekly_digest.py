#!/usr/bin/env python3
"""Print a markdown digest of recent devpulse notes to stdout.

    python3 scripts/weekly_digest.py [--days N] [--data DIR]

The window ends at the newest note in the store rather than wall-clock now,
so the same data always produces the same digest. The window length defaults
to $DEVPULSE_DEFAULT_DAYS, then 7.
"""
import argparse
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from devpulse.cli import default_days
from devpulse.render import render_digest
from devpulse.store import NOTES_FILE, NoteStore


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='markdown digest of recent devpulse notes')
    parser.add_argument('--days', type=int, default=None,
                        help='window size in days '
                             '(default: $DEVPULSE_DEFAULT_DAYS or 7)')
    parser.add_argument('--data', default=None,
                        help='notes dir (default: $DEVPULSE_DATA_DIR or '
                             '.devpulse)')
    args = parser.parse_args(argv)

    store = (NoteStore(os.path.join(args.data, NOTES_FILE))
             if args.data else NoteStore())
    days = args.days or default_days()
    sys.stdout.write(render_digest(store.since(days, now=store.latest_ts()),
                                   days))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
