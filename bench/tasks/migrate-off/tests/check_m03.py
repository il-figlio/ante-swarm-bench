"""Check m03: .claude/commands/label-pr.md ported to
.ante/skills/label-pr/SKILL.md."""
import unittest

from tests.skillcheck import SkillCase


class TestLabelPrSkill(SkillCase, unittest.TestCase):
    NAME = 'label-pr'
    SCRIPT = 'label_pr.py'

    def test_needs_triage_behaviour_kept(self):
        self.require()
        self.assertRegex(self.body or '', r'(?i)needs[- ]triage',
                         'the needs-triage fallback instruction is gone')

    def test_cheap_model_preference_noted(self):
        self.require()
        self.assertRegex(
            self.body or '', r'(?i)haiku|cheap|fast|lightweight',
            'the command pinned a cheap model; skill frontmatter has no '
            'model field, so the preference belongs in the body')


if __name__ == '__main__':
    unittest.main()
