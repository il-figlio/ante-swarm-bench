"""Check n02: INDEX.md at the repo root lists every note under notes/
exactly once, grouped under a `## <folder>` section per subfolder, with
links that resolve per the README convention."""
import unittest
from collections import Counter
from pathlib import Path

import tests.kb as kb

INDEX = kb.ROOT / "INDEX.md"


class TestIndex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exists = INDEX.is_file()
        cls.rows = kb.index_sections(INDEX) if cls.exists else []
        cls.notes = {f.relative_to(kb.ROOT).as_posix()
                     for f in kb.note_files()}

    def test_file_exists(self):
        self.assertTrue(self.exists, "INDEX.md does not exist at repo root")

    def test_links_resolve(self):
        if not self.exists:
            self.fail("INDEX.md does not exist")
        bad = kb.broken_links([INDEX])
        self.assertFalse(bad, f"unresolvable links in INDEX.md: {bad[:5]}")

    def test_every_note_exactly_once(self):
        if not self.exists:
            self.fail("INDEX.md does not exist")
        counts = Counter()
        for _, _, m in self.rows:
            p = kb.link_path(INDEX, kb.link_target(m))
            if p is not None and p.as_posix().startswith("notes/"):
                counts[p.as_posix()] += 1
        listed = set(counts)
        self.assertEqual(listed, self.notes,
                         f"missing: {sorted(self.notes - listed)}; "
                         f"extra: {sorted(listed - self.notes)}")
        dupes = {p: n for p, n in counts.items() if n > 1}
        self.assertFalse(dupes, f"notes listed more than once: {dupes}")

    def test_grouped_by_folder(self):
        if not self.exists:
            self.fail("INDEX.md does not exist")
        misplaced = []
        nosection = []
        for section, ln, m in self.rows:
            p = kb.link_path(INDEX, kb.link_target(m))
            if p is None:
                continue
            rel = p.as_posix()
            if rel not in self.notes:
                continue  # non-note link; resolve test covers validity
            if section is None:
                nosection.append(f"line {ln}: {rel}")
                continue
            if Path(rel).parent.name.casefold() != section:
                misplaced.append(
                    f"line {ln}: {rel} under section '{section}'")
        self.assertFalse(nosection,
                         f"note links outside any ## section: {nosection}")
        self.assertFalse(misplaced,
                         f"note links under the wrong folder section: "
                         f"{misplaced}")


if __name__ == "__main__":
    unittest.main()
