"""Partition full Swarm discovery across isolated CI runners. Run from swarm/."""
import sys
import unittest


def cases(suite):
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from cases(item)
        else:
            yield item


def load_tests(loader, standard_tests, pattern):
    # Discover first, so new modules and discovery errors go through the same
    # loader as local runs. Keep each module together for its shared fixtures.
    discovered = loader.discover("tests")
    selected = unittest.TestSuite()
    for test in cases(discovered):
        module = test.__class__.__module__
        maze_play = module.startswith(("test_maze", "test_play"))
        if maze_play == (shard == "maze-play"):
            selected.addTest(test)
    return selected


if __name__ == "__main__":
    shard = sys.argv.pop(1)
    if shard not in ("maze-play", "other"):
        raise SystemExit("expected a maze-play or other shard")
    unittest.main()
