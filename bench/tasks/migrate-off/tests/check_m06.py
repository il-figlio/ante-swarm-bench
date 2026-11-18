"""Check m06: AGENTS.md was rewritten to carry the project conventions that
used to live in CLAUDE.md, plus pointers at the ported skills."""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AGENTS = ROOT / 'AGENTS.md'

REQUIRED = [
    'make check',        # the test command
    'single quot',       # style rule
    '4-space',           # style rule
    'stdlib',            # no third-party deps
    'changelog.md',      # generated, hands off
    'scripts/',          # hands off
    'tests/',            # hands off
    'area:',             # commit convention
    '72',                # subject length cap
    'co-authored-by',    # no such trailers
    '.devpulse',         # where notes live
    '.ante/skills',      # the ported skills
    'digest', 'label-pr', 'changelog',
]
FORBIDDEN = re.compile(r'(?i)\b(codex|claude)\b')


def normalise(text):
    return re.sub(r'[-_]', ' ', text.lower())


class TestAgentsMd(unittest.TestCase):
    def read(self):
        if not AGENTS.is_file():
            self.fail('AGENTS.md does not exist')
        return AGENTS.read_text()

    def test_conventions_present(self):
        norm = normalise(self.read())
        missing = [t for t in REQUIRED
                   if normalise(t) not in norm and t.lower() not in norm]
        self.assertFalse(missing,
                         'AGENTS.md is missing conventions: %s'
                         % ', '.join(missing))

    def test_no_old_tool_references(self):
        hit = FORBIDDEN.search(self.read())
        self.assertIsNone(
            hit, 'AGENTS.md still references the old tooling: %r'
            % (hit.group(0) if hit else ''))


if __name__ == '__main__':
    unittest.main()
