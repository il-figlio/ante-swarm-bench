"""Check m01: .ante/settings.json carries over the .claude/settings.json
semantics — model, permission posture (allow/ask/deny), env vars."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / '.ante' / 'settings.json'

EXPECTED_DENY = {
    'Bash(git push:*)',
    'Bash(rm -rf:*)',
    'Bash(git reset --hard:*)',
    'Read(.env*)',
    'Read(**/.env*)',
}
EXPECTED_ASK = {'Bash(git commit:*)'}
# the allow list, checked as substrings so rule shape is not over-pinned
EXPECTED_ALLOW_PARTS = ['scripts/weekly_digest.py', 'scripts/label_pr.py',
                        'scripts/bump_changelog.py', 'make check']
EXPECTED_ENV = {'DEVPULSE_DATA_DIR': '.devpulse',
                'DEVPULSE_DEFAULT_DAYS': '7'}
CLAUDE_ONLY_KEYS = {'includeCoAuthoredBy', '$schema'}


def load_settings(test):
    if not SETTINGS.is_file():
        test.fail('.ante/settings.json does not exist')
    try:
        data = json.loads(SETTINGS.read_text())
    except json.JSONDecodeError as exc:
        test.fail('.ante/settings.json is not valid JSON: %s' % exc)
    if not isinstance(data, dict):
        test.fail('.ante/settings.json must be a JSON object')
    return data


class TestAnteSettings(unittest.TestCase):
    def test_file_is_valid_json_object(self):
        load_settings(self)

    def test_model_is_the_claude_one(self):
        data = load_settings(self)
        self.assertEqual(
            data.get('model'), 'claude-sonnet-4-6',
            'model should match .claude/settings.json, not the Codex model '
            'in .codex/config.toml')

    def test_effort(self):
        data = load_settings(self)
        self.assertEqual(data.get('effort'), 'high')

    def test_permission_mode_strict(self):
        data = load_settings(self)
        self.assertEqual(
            data.get('permission_mode'), 'strict',
            'permission_mode should be "strict" so anything not on the '
            'allow list still asks')

    def test_deny_rules_preserved(self):
        perms = load_settings(self).get('permissions') or {}
        self.assertIsInstance(perms, dict,
                              '"permissions" should be an object with '
                              'allow/ask/deny lists')
        deny = set(perms.get('deny') or [])
        missing = EXPECTED_DENY - deny
        self.assertFalse(missing, 'deny rules dropped: %s'
                         % ', '.join(sorted(missing)))

    def test_ask_rule_preserved(self):
        perms = load_settings(self).get('permissions') or {}
        ask = set(perms.get('ask') or [])
        missing = EXPECTED_ASK - ask
        self.assertFalse(missing, 'ask rules dropped: %s'
                         % ', '.join(sorted(missing)))

    def test_allow_rules_preserved(self):
        perms = load_settings(self).get('permissions') or {}
        allow = perms.get('allow') or []
        self.assertIsInstance(allow, list)
        for part in EXPECTED_ALLOW_PARTS:
            self.assertTrue(any(part in rule for rule in allow),
                            'allow list lost the rule covering %r' % part)

    def test_env_vars_carried(self):
        env = load_settings(self).get('env') or {}
        self.assertIsInstance(env, dict, '"env" should be an object')
        for key, want in EXPECTED_ENV.items():
            self.assertEqual(env.get(key), want,
                             'env.%s should be %r' % (key, want))

    def test_no_claude_only_keys(self):
        data = load_settings(self)
        leftovers = CLAUDE_ONLY_KEYS & set(data)
        self.assertFalse(leftovers, 'Claude-specific keys copied over: %s'
                         % ', '.join(sorted(leftovers)))


if __name__ == '__main__':
    unittest.main()
