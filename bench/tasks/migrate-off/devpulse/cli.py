"""devpulse command line interface: add, list, search and digest.

    python3 -m devpulse add "shipped the search fix" --tag fix
    python3 -m devpulse list --days 7 --tag feat
    python3 -m devpulse search changelog
    python3 -m devpulse digest --days 7
"""
import argparse
import os
import sys

from devpulse.render import render_digest, render_list
from devpulse.store import NoteStore

DEFAULT_DAYS_ENV = 'DEVPULSE_DEFAULT_DAYS'
DEFAULT_DAYS = 7


def default_days():
    """Default digest window: $DEVPULSE_DEFAULT_DAYS, else 7."""
    try:
        days = int(os.environ.get(DEFAULT_DAYS_ENV) or '')
    except ValueError:
        return DEFAULT_DAYS
    return days if days > 0 else DEFAULT_DAYS


def build_parser():
    parser = argparse.ArgumentParser(
        prog='devpulse',
        description='standup notes and changelog helper')
    sub = parser.add_subparsers(dest='cmd', required=True)

    add = sub.add_parser('add', help='append a note')
    add.add_argument('text')
    add.add_argument('--tag', default='note')

    lst = sub.add_parser('list', help='list notes')
    lst.add_argument('--days', type=int, default=None)
    lst.add_argument('--tag', default=None)

    search = sub.add_parser('search', help='search note text and tags')
    search.add_argument('query')

    digest = sub.add_parser('digest', help='markdown digest of recent notes')
    digest.add_argument('--days', type=int, default=None)
    return parser


def main(argv=None, store=None, out=None):
    """CLI entry point. `store` and `out` are injectable for tests."""
    out = out or sys.stdout
    store = store or NoteStore()
    args = build_parser().parse_args(argv)

    if args.cmd == 'add':
        note = store.add(args.text, tag=args.tag)
        print('saved: [%s] %s' % (note['tag'], note['text']), file=out)
        return 0
    if args.cmd == 'list':
        notes = store.since(args.days) if args.days else store.all()
        if args.tag:
            notes = [n for n in notes if n['tag'] == args.tag]
        out.write(render_list(notes))
        return 0
    if args.cmd == 'search':
        out.write(render_list(store.search(args.query)))
        return 0
    if args.cmd == 'digest':
        days = args.days or default_days()
        out.write(render_digest(store.since(days), days))
        return 0
    return 2


if __name__ == '__main__':
    raise SystemExit(main())
