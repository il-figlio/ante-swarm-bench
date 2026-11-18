"""Check m05: the shell-env exclusions from .codex/config.toml
(shell_environment_policy.exclude) survive in .ante/settings.json as an
env_exclude list, and permission_mode is strict."""
import json
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SETTINGS = ROOT / '.ante' / 'settings.json'
EXPECTED_EXCLUDE = {'AWS_*', 'PROD_*'}


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


class TestEnvExclude(unittest.TestCase):
    def test_file_is_valid_json_object(self):
        load_settings(self)

    def test_env_exclude_carries_codex_patterns(self):
        data = load_settings(self)
        excl = data.get('env_exclude')
        self.assertIsInstance(excl, list,
                              '"env_exclude" should be a list of shell '
                              'patterns')
        missing = EXPECTED_EXCLUDE - set(excl)
        self.assertFalse(missing, 'env_exclude is missing: %s'
                         % ', '.join(sorted(missing)))

    def test_permission_mode_strict(self):
        data = load_settings(self)
        self.assertEqual(data.get('permission_mode'), 'strict')


if __name__ == '__main__':
    unittest.main()
