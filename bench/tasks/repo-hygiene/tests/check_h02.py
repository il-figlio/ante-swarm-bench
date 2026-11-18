"""Check h02: the dead functions listed in tests/manifest.json are gone,
every other top-level function survived untouched (the manifest's
function inventory is the exact expected set — removing a live function
or adding a helper both fail), and the repo's own suite still passes."""
import unittest

from tests.hygiene import MANIFEST, ROOT, function_names, run_suite


def dead_by_file():
    out = {}
    for entry in MANIFEST["dead_functions"]:
        out.setdefault(entry["file"], set()).add(entry["name"])
    return out


class TestDeadCode(unittest.TestCase):
    def test_dead_functions_removed(self):
        for entry in MANIFEST["dead_functions"]:
            path = ROOT / entry["file"]
            self.assertTrue(path.is_file(),
                            "%s is missing entirely" % entry["file"])
            self.assertNotIn(
                entry["name"], function_names(path),
                "%s: %s() is still defined" % (entry["file"],
                                               entry["name"]))

    def test_live_functions_untouched(self):
        dead = dead_by_file()
        for name, expected in MANIFEST["functions"].items():
            path = ROOT / name
            self.assertTrue(path.is_file(), "%s is missing" % name)
            want = set(expected) - dead.get(name, set())
            got = set(function_names(path))
            self.assertEqual(
                got, want,
                "%s: functions changed beyond the dead ones — "
                "missing %s, unexpected %s"
                % (name, sorted(want - got), sorted(got - want)))

    def test_suite_still_passes(self):
        proc = run_suite()
        self.assertEqual(
            proc.returncode, 0,
            "the repo test suite fails:\n%s"
            % (proc.stdout + proc.stderr)[-2000:])


if __name__ == "__main__":
    unittest.main()
