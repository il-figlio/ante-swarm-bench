"""Run the environment-dependent integration suites against the scripted model."""
import unittest


if __name__ == "__main__":
    suites = (
        "test_arcade.StandinRoundTests", "test_play.EndToEndTests", "test_swarm.AnteTests",
        "test_harnesses.RealCliTests", "test_pi.PiLiveTests", "test_nanocode.NanocodeLiveTests",
        "test_limits.RealCgroupTests",
    )
    tests = unittest.defaultTestLoader.loadTestsFromNames(suites)
    result = unittest.TextTestRunner(verbosity=2).run(tests)
    raise SystemExit(0 if result.wasSuccessful() and result.testsRun and not result.skipped else 1)
