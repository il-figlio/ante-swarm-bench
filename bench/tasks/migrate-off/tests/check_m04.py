"""Check m04: .claude/commands/changelog.md ported to
.ante/skills/changelog/SKILL.md."""
import unittest

from tests.skillcheck import SkillCase


class TestChangelogSkill(SkillCase, unittest.TestCase):
    NAME = 'changelog'
    SCRIPT = 'bump_changelog.py'

    def test_shows_the_diff(self):
        self.require()
        self.assertRegex(self.body or '', r'git\s+diff',
                         'the body should still show `git diff` output '
                         'after the bump')

    def test_ownership_rule_kept(self):
        self.require()
        self.assertRegex(
            self.body or '', r'(?i)changelog\.md',
            'CHANGELOG.md is not mentioned')
        self.assertRegex(
            self.body or '', r'(?i)hand.?edit|script owns|only.*script',
            'the "never hand-edit CHANGELOG.md" rule was dropped')


if __name__ == '__main__':
    unittest.main()
