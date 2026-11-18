"""The suite `make check` runs."""
import io
import os
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

from devpulse.cli import main as cli_main
from devpulse.render import group_by_tag, render_digest, render_list
from devpulse.store import NoteStore

ROOT = Path(__file__).resolve().parents[1]
NOW = datetime(2026, 9, 14, 12, 0, tzinfo=timezone.utc)


def make_store(tmp):
    return NoteStore(str(Path(tmp) / 'notes.jsonl'))


def note(text, tag='note', days_ago=0):
    return {'ts': (NOW - timedelta(days=days_ago)).strftime(
        '%Y-%m-%dT%H:%M:%SZ'), 'tag': tag, 'text': text}


class TestStore(unittest.TestCase):
    def test_add_appends_and_roundtrips(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add('first', tag='note')
            store.add('second', tag='fix')
            self.assertEqual([n['text'] for n in store.all()],
                             ['first', 'second'])
            self.assertEqual(store.all()[1]['tag'], 'fix')

    def test_all_sorts_by_ts(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add('newer', ts=NOW)
            store.add('older', ts=NOW - timedelta(days=3))
            self.assertEqual([n['text'] for n in store.all()],
                             ['older', 'newer'])

    def test_since_window(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add('recent', ts=NOW - timedelta(days=2))
            store.add('stale', ts=NOW - timedelta(days=10))
            recent = store.since(7, now=NOW)
            self.assertEqual([n['text'] for n in recent], ['recent'])

    def test_search_matches_text_and_tag(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add('Buy Milk Today')
            store.add('unrelated', tag='chore')
            self.assertEqual(
                [n['text'] for n in store.search('milk')], ['Buy Milk Today'])
            self.assertEqual(
                [n['text'] for n in store.search('CHORE')], ['unrelated'])

    def test_missing_file_is_empty(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            self.assertEqual(store.all(), [])
            self.assertIsNone(store.latest_ts())


class TestRender(unittest.TestCase):
    def test_group_by_tag_orders_blockers_first(self):
        groups = group_by_tag([note('a', 'feat'), note('b', 'blocker'),
                               note('c', 'fix')])
        self.assertEqual(list(groups), ['blocker', 'feat', 'fix'])

    def test_digest_header_and_sections(self):
        out = render_digest([note('shipped it', 'feat')], 7)
        self.assertIn('# Digest — last 7 days', out)
        self.assertIn('## feat (1)', out)
        self.assertIn('**feat** shipped it', out)

    def test_render_list_empty(self):
        self.assertEqual(render_list([]), 'No notes.\n')


class TestCli(unittest.TestCase):
    def run_cli(self, argv, store):
        out = io.StringIO()
        rc = cli_main(argv, store=store, out=out)
        return rc, out.getvalue()

    def test_add_then_list(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            rc, out = self.run_cli(['add', 'shipped search', '--tag', 'feat'],
                                   store)
            self.assertEqual(rc, 0)
            self.assertIn('[feat] shipped search', out)
            rc, out = self.run_cli(['list'], store)
            self.assertIn('shipped search', out)

    def test_search_cli(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add('fixed the digest crash')
            rc, out = self.run_cli(['search', 'digest'], store)
            self.assertEqual(rc, 0)
            self.assertIn('digest crash', out)

    def test_digest_respects_default_days_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.add('today', ts=datetime.now(timezone.utc))
            store.add('old', ts=datetime.now(timezone.utc) - timedelta(days=30))
            os.environ['DEVPULSE_DEFAULT_DAYS'] = '3'
            try:
                rc, out = self.run_cli(['digest'], store)
            finally:
                del os.environ['DEVPULSE_DEFAULT_DAYS']
            self.assertEqual(rc, 0)
            self.assertIn('today', out)
            self.assertNotIn('old', out)


class TestScripts(unittest.TestCase):
    def run_script(self, *args, cwd=None):
        return subprocess.run([sys.executable] + list(args),
                              cwd=cwd or ROOT, capture_output=True,
                              text=True, timeout=60)

    def test_weekly_digest_is_deterministic(self):
        a = self.run_script('scripts/weekly_digest.py', '--days', '7')
        b = self.run_script('scripts/weekly_digest.py', '--days', '7')
        self.assertEqual(a.returncode, 0, a.stderr)
        self.assertIn('# Digest', a.stdout)
        self.assertEqual(a.stdout, b.stdout)

    def test_label_pr(self):
        proc = self.run_script('scripts/label_pr.py',
                               '--title', 'fix: crash on empty input')
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('bug', proc.stdout.split())

    def test_bump_changelog(self):
        with tempfile.TemporaryDirectory() as tmp:
            changelog = Path(tmp) / 'CHANGELOG.md'
            changelog.write_text('# Changelog\n\n## Unreleased\n\n- a thing\n')
            proc = self.run_script(str(ROOT / 'scripts' / 'bump_changelog.py'),
                                   '--version', '9.9.9', '--date',
                                   '2026-09-16', cwd=tmp)
            self.assertEqual(proc.returncode, 0, proc.stderr)
            text = changelog.read_text()
            self.assertIn('## Unreleased', text)
            self.assertIn('## [9.9.9] - 2026-09-16', text)
            self.assertLess(text.index('## Unreleased'),
                            text.index('## [9.9.9]'))


if __name__ == '__main__':
    unittest.main()
