"""Shared helpers for the migrate-off checkers.

Skills are validated as structure, not executed: the check parses the
SKILL.md frontmatter and asserts the body still invokes the same repo script
with argument forwarding.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / '.ante' / 'skills'

# Frontmatter keys the SKILL.md format accepts. `name` and `description` are
# required; the rest are optional. Keys like `model` that command files use
# do not exist here.
ALLOWED_KEYS = {'name', 'description', 'argument-hint', 'allowed-tools',
                'disable-model-invocation', 'user-invocable', 'metadata',
                'license', 'compatibility'}


def skill_path(name):
    return SKILLS / name / 'SKILL.md'


def parse_frontmatter(text):
    """Return (frontmatter_dict, body). The dict is None without a --- block.

    Scalars only; indented lines continue the previous key, matching the
    simple YAML subset the format uses.
    """
    m = re.match(r'^\ufeff?---[ \t]*\r?\n(.*?)\r?\n---[ \t]*\r?\n?',
                 text, re.DOTALL)
    if not m:
        return None, text
    fm, last = {}, None
    for line in m.group(1).splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        if line[0] in ' \t' and last is not None:
            fm[last] = (fm[last] + ' ' + line.strip()).strip()
            continue
        key, sep, val = line.partition(':')
        if not sep:
            continue
        last = key.strip()
        fm[last] = val.strip().strip('"').strip("'")
    return fm, text[m.end():]


class SkillCase:
    """Mixin for the three command-to-skill port checks.

    Subclasses set NAME (skill dir / command name), SCRIPT (the repo script
    the command wraps), subclass ``unittest.TestCase`` as well, and may add
    behaviour-token tests. Not a TestCase itself so the loader never runs it
    directly.
    """
    NAME = None
    SCRIPT = None

    @classmethod
    def setUpClass(cls):
        cls.path = skill_path(cls.NAME)
        cls.text = cls.path.read_text() if cls.path.is_file() else None
        cls.fm, cls.body = (parse_frontmatter(cls.text)
                            if cls.text is not None else (None, None))

    def require(self):
        if self.text is None:
            self.fail('%s does not exist'
                      % self.path.relative_to(ROOT).as_posix())

    def test_file_exists(self):
        self.require()

    def test_frontmatter_parses(self):
        self.require()
        self.assertIsNotNone(self.fm,
                             'SKILL.md has no --- frontmatter block')
        self.assertTrue((self.body or '').strip(),
                        'SKILL.md body is empty')

    def test_name_and_description(self):
        self.require()
        fm = self.fm or {}
        self.assertEqual(fm.get('name'), self.NAME,
                         "frontmatter 'name' must be %r (the command name)"
                         % self.NAME)
        self.assertGreater(len(fm.get('description') or ''), 10,
                           "frontmatter needs a real 'description'")

    def test_only_supported_frontmatter_keys(self):
        self.require()
        extra = sorted(set(self.fm or {}) - ALLOWED_KEYS)
        self.assertFalse(
            extra, 'frontmatter keys the skill format does not support: %s'
            % ', '.join(extra))

    def test_argument_hint_kept(self):
        self.require()
        self.assertIn('argument-hint', self.fm or {},
                      'the command had an argument-hint; keep it')

    def test_allowed_tools_kept(self):
        self.require()
        tools = (self.fm or {}).get('allowed-tools') or ''
        self.assertIn(self.SCRIPT, tools,
                      'allowed-tools should still name %s' % self.SCRIPT)

    def test_invokes_script_with_arguments(self):
        self.require()
        self.assertRegex(
            self.body or '',
            r'python3?\s+scripts/%s' % re.escape(self.SCRIPT),
            'body must still run scripts/%s' % self.SCRIPT)
        self.assertIn('$ARGUMENTS', self.body or '',
                      'argument forwarding ($ARGUMENTS) was dropped')

    def test_no_bang_execution_syntax(self):
        self.require()
        self.assertNotRegex(
            self.text, r'!`',
            'the `!`...`` inline-execution syntax is command-file only; '
            'write the invocation as an instruction instead')
