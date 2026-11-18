"""Check n04: notes/projects/README.md summarises the folder — one
`- [title](file.md) — description` line per other note, links resolving,
descriptions non-empty, the file itself a summary (under 4 KB)."""
import unittest
from collections import Counter
from pathlib import Path

import tests.kb as kb

SUMMARY = kb.ROOT / "notes" / "projects" / "README.md"
MAX_BYTES = 4096
MIN_DESC = 10


class TestProjectsReadme(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exists = SUMMARY.is_file()
        cls.text = (SUMMARY.read_text(errors="replace")
                    if cls.exists else "")
        cls.folder = {
            f.relative_to(kb.ROOT).as_posix()
            for f in (kb.ROOT / "notes" / "projects").glob("*.md")
        } - {"notes/projects/README.md"}

    def test_file_exists_and_is_a_summary(self):
        self.assertTrue(self.exists,
                        "notes/projects/README.md does not exist")
        self.assertLessEqual(
            SUMMARY.stat().st_size, MAX_BYTES,
            f"file is {SUMMARY.stat().st_size} bytes — a summary, not a "
            f"copy (cap {MAX_BYTES})")

    def test_every_note_listed_once(self):
        if not self.exists:
            self.fail("notes/projects/README.md does not exist")
        counts = Counter()
        for _, m in kb.iter_links(SUMMARY):
            t = kb.link_target(m)
            if not kb.is_file_link(t):
                continue
            p = kb.link_path(SUMMARY, t)
            if p is not None and p.as_posix() in self.folder:
                counts[p.as_posix()] += 1
        listed = set(counts)
        self.assertEqual(listed, self.folder,
                         f"missing: {sorted(self.folder - listed)}; "
                         f"extra: {sorted(listed - self.folder)}")
        dupes = {p: n for p, n in counts.items() if n > 1}
        self.assertFalse(dupes, f"notes linked more than once: {dupes}")

    def test_links_resolve(self):
        if not self.exists:
            self.fail("notes/projects/README.md does not exist")
        bad = kb.broken_links([SUMMARY])
        self.assertFalse(bad, f"unresolvable links: {bad}")

    def test_each_entry_has_a_description(self):
        if not self.exists:
            self.fail("notes/projects/README.md does not exist")
        missing = []
        for i, line in enumerate(self.text.splitlines(), 1):
            for m in kb.LINK_RE.finditer(line):
                t = kb.link_target(m)
                if not kb.is_file_link(t):
                    continue
                p = kb.link_path(SUMMARY, t)
                if p is None or p.as_posix() not in self.folder:
                    continue
                tail = line[m.end():].strip().lstrip("-—–: ").strip()
                if len(tail) < MIN_DESC:
                    missing.append(f"line {i}: {t} has no real description")
        self.assertFalse(missing, missing)


if __name__ == "__main__":
    unittest.main()
