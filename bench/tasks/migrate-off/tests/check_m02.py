"""Check m02: .claude/commands/digest.md ported to
.ante/skills/digest/SKILL.md."""
import unittest

from tests.skillcheck import SkillCase


class TestDigestSkill(SkillCase, unittest.TestCase):
    NAME = 'digest'
    SCRIPT = 'weekly_digest.py'

    def test_standup_shaping_kept(self):
        self.require()
        self.assertRegex(self.body or '', r'(?i)standup',
                         'the body should still shape the output into a '
                         'standup update')


if __name__ == '__main__':
    unittest.main()
