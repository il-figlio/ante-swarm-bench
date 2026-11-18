#!/usr/bin/env python3
"""Roll CHANGELOG.md's `## Unreleased` section into a versioned release.

    python3 scripts/bump_changelog.py --version 0.5.0 --date 2026-09-16

Leaves a fresh empty Unreleased section on top and moves the existing
bullets under `## [<version>] - <date>`. CHANGELOG.md is only ever written
by this script — never hand-edit release sections.
"""
import argparse
import re
import sys
from datetime import date

CHANGELOG = 'CHANGELOG.md'
UNRELEASED = '## Unreleased'


def bump(text, version, day):
    """Return `text` with the Unreleased bullets moved under a new heading."""
    if UNRELEASED not in text:
        raise SystemExit("error: no '## Unreleased' heading in " + CHANGELOG)
    if not re.match(r'^\d+\.\d+\.\d+$', version):
        raise SystemExit('error: version must look like X.Y.Z')
    heading = '## [%s] - %s' % (version, day)
    return text.replace(
        UNRELEASED,
        UNRELEASED + '\n\n_(nothing yet)_\n\n' + heading,
        1)


def main(argv=None):
    parser = argparse.ArgumentParser(
        description='cut a changelog release from the Unreleased section')
    parser.add_argument('--version', required=True, help='X.Y.Z')
    parser.add_argument('--date', default=str(date.today()),
                        help='release date (default: today)')
    args = parser.parse_args(argv)

    with open(CHANGELOG, encoding='utf-8') as fh:
        text = fh.read()
    new = bump(text, args.version, args.date)
    with open(CHANGELOG, 'w', encoding='utf-8') as fh:
        fh.write(new)
    print('released %s (%s)' % (args.version, args.date))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
