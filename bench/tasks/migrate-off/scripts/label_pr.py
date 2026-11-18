#!/usr/bin/env python3
"""Suggest PR labels from a title and body using fixed keyword rules.

    python3 scripts/label_pr.py --title "fix: empty digest" [--body "..."]

Prints one label per line, sorted; prints `needs-triage` when no rule
matches. Deterministic — same input, same labels.
"""
import argparse
import re

RULES = [
    ('bug', r'\b(fix|bug|bugfix|hotfix|crash|regression)\b'),
    ('feature', r'\b(feat|feature|add|adds|support|introduce)\b'),
    ('docs', r'\b(docs?|readme|changelog|comment)s?\b'),
    ('refactor', r'\b(refactor|cleanup|clean up|simplif\w*|rename)\b'),
    ('tests', r'\b(test|tests|testing|coverage)\b'),
    ('release', r'\b(release|version|bump|tag)\b'),
]


def labels_for(title, body=''):
    """All labels whose keyword rule matches the title or body."""
    text = (title + '\n' + body).lower()
    return sorted({label for label, pat in RULES if re.search(pat, text)})


def main(argv=None):
    parser = argparse.ArgumentParser(description='suggest labels for a PR')
    parser.add_argument('--title', required=True)
    parser.add_argument('--body', default='')
    args = parser.parse_args(argv)
    labels = labels_for(args.title, args.body)
    print('\n'.join(labels) if labels else 'needs-triage')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
