"""Check t05: the lost detached-HEAD commit in personal-site/ has been
recovered and merged into master.

The source task asserted the final content of the two patched files; the
expected md5 digests (of stripped file bytes) live in
tests/manifest.json. Two extra guards verify the merge actually
happened: HEAD is on master and the lost commit (found via the HEAD
reflog's "commit:" entry) is an ancestor of it.
"""
import hashlib
import json
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "personal-site"
MANIFEST = json.loads((ROOT / "tests" / "manifest.json").read_text())


def _skip_without(inp, out):
    """The input is fetched by fetch_inputs.py, not committed: skip, not fail, when it was never fetched."""
    if not (ROOT / inp).exists() and not (ROOT / out).exists():
        raise unittest.SkipTest("%s is not present; run fetch_inputs.py (see ATTRIBUTION.md)" % inp)


def _hash(path):
    return hashlib.md5(path.read_bytes().strip()).hexdigest()


def _git(*args):
    return subprocess.run(
        ["git", "-C", str(SITE)] + list(args),
        capture_output=True, text=True, timeout=30)


def _lost_commit():
    """The commit made on the detached HEAD, from the HEAD reflog."""
    reflog = SITE / ".git" / "logs" / "HEAD"
    if not reflog.is_file():
        return None
    for line in reflog.read_text(errors="replace").splitlines():
        # the first bare "commit:" entry is the detached-HEAD commit
        # (later agent commits would be appended below it)
        if "\tcommit:" in line or " commit:" in line:
            return line.split()[1]
    return None


class TestFixGit(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        _skip_without("personal-site.tar.gz", "personal-site")

    def test_site_unpacked(self):
        self.assertTrue((SITE / ".git").is_dir(),
                        "personal-site/ is not unpacked (tar xzf "
                        "personal-site.tar.gz first)")

    def test_about_file(self):
        p = SITE / "_includes" / "about.md"
        self.assertTrue(p.is_file(), "%s missing" % p)
        self.assertEqual(
            _hash(p), MANIFEST["t05"]["file_md5"]["_includes/about.md"],
            "File %s is not in the correct state" % p)

    def test_layout_file(self):
        p = SITE / "_layouts" / "default.html"
        self.assertTrue(p.is_file(), "%s missing" % p)
        self.assertEqual(
            _hash(p),
            MANIFEST["t05"]["file_md5"]["_layouts/default.html"],
            "File %s is not in the correct state" % p)

    def test_lost_commit_merged_into_master(self):
        if not (SITE / ".git").is_dir():
            self.fail("personal-site/ is not unpacked")
        proc = _git("rev-parse", "--abbrev-ref", "HEAD")
        self.assertEqual(proc.stdout.strip(), "master",
                         "HEAD is not on master")
        lost = _lost_commit()
        self.assertIsNotNone(lost, "no detached commit found in the "
                                   "HEAD reflog")
        merged = _git("merge-base", "--is-ancestor",
                      lost, "master").returncode == 0
        if not merged:
            # a cherry-pick / rebase keeps the subject but not the sha
            log = _git("log", "master", "--format=%s")
            merged = "Move to Stanford" in log.stdout.splitlines()
        self.assertTrue(
            merged,
            "the lost commit (%s) was never merged into master"
            % lost[:8])


if __name__ == "__main__":
    unittest.main()
