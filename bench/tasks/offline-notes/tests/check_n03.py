"""Check n03: every broken cross-link is fixed to the right target, and
nothing else changed — same file set, same prose (hashed with link
targets blanked), zero unresolvable links left."""
import unittest
from pathlib import Path

import tests.kb as kb


class TestFixBrokenLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.m = kb.MANIFEST

    def test_file_set_unchanged(self):
        actual = sorted(f.relative_to(kb.ROOT).as_posix()
                        for f in kb.note_files())
        self.assertEqual(actual, self.m["notes_files"],
                         "notes/ file set changed — fix links in place, "
                         "don't add, delete, rename or move files")

    def test_prose_unchanged(self):
        diffs = []
        for rel, want in self.m["content_hashes"].items():
            p = kb.ROOT / rel
            if not p.is_file() or kb.content_hash(p) != want:
                diffs.append(rel)
        self.assertFalse(diffs,
                         "non-link content changed (only link targets may "
                         f"be edited): {diffs}")

    def test_no_broken_links(self):
        paths = kb.note_files()
        index = kb.ROOT / "INDEX.md"
        if index.is_file():
            paths.append(index)
        bad = kb.broken_links(paths)
        self.assertFalse(bad, f"broken links remain: {bad}")

    def test_each_fix_landed(self):
        misses = []
        for b in self.m["broken_links"]:
            f = kb.ROOT / b["file"]
            if not f.is_file():
                misses.append(f"{b['file']} missing")
                continue
            lines = f.read_text(errors="replace").splitlines()
            if b["line"] > len(lines):
                misses.append(f"{b['file']}:{b['line']} out of range")
                continue
            ok = False
            for m in kb.LINK_RE.finditer(lines[b["line"] - 1]):
                p = kb.link_path(f, kb.link_target(m))
                if p is not None and p.as_posix() == b["correct_resolved"]:
                    ok = True
            if not ok:
                misses.append(
                    f"{b['file']}:{b['line']} should link to "
                    f"{b['correct_resolved']} (was {b['target']})")
        self.assertFalse(misses, "links not fixed to the right target: "
                                 f"{misses}")


if __name__ == "__main__":
    unittest.main()
