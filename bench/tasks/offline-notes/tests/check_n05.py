"""Check n05: TAGS.md at the repo root indexes every inline #tag used in
notes/ — one `## #tag` section per tag, each followed by links to exactly
the notes carrying that tag. Ground truth is derived live, so the check
can't drift from the corpus."""
import unittest

import tests.kb as kb

TAGS = kb.ROOT / "TAGS.md"


class TestTagIndex(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exists = TAGS.is_file()
        cls.truth = kb.tag_truth()
        # section -> {repo_rel_note}
        cls.sections = {}
        if cls.exists:
            for section, ln, m in kb.index_sections(TAGS):
                if section is None:
                    continue
                p = kb.link_path(TAGS, kb.link_target(m))
                if p is None:
                    continue
                cls.sections.setdefault(section.lstrip("#"), set()).add(
                    p.as_posix())

    def test_file_exists(self):
        self.assertTrue(self.exists, "TAGS.md does not exist at repo root")

    def test_links_resolve(self):
        if not self.exists:
            self.fail("TAGS.md does not exist")
        bad = kb.broken_links([TAGS])
        self.assertFalse(bad, f"unresolvable links in TAGS.md: {bad[:5]}")

    def test_every_tag_covered(self):
        if not self.exists:
            self.fail("TAGS.md does not exist")
        have = set(self.sections)
        want = set(self.truth)
        self.assertEqual(have, want,
                         f"missing tags: {sorted(want - have)}; "
                         f"extra sections: {sorted(have - want)}")

    def test_sections_list_the_right_notes(self):
        if not self.exists:
            self.fail("TAGS.md does not exist")
        bad = []
        for tag, files in sorted(self.truth.items()):
            got = self.sections.get(tag, set())
            if got != files:
                bad.append(f"#{tag}: missing {sorted(files - got)}, "
                           f"extra {sorted(got - files)}")
        self.assertFalse(bad, bad)


if __name__ == "__main__":
    unittest.main()
