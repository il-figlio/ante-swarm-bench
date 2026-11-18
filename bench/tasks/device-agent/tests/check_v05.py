"""Check v05: `channels` asks the pod (CHANNELS -> `CHAN a b c`) and
prints each channel on its own line, in the order the pod reports."""
import unittest

from tests.harness import MANIFEST, PackTestCase, run_client


class TestChannels(PackTestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = run_client("channels", scenario="clean")

    def test_clean_source(self):
        self.assert_clean_source()

    def test_exits_clean(self):
        self.assert_no_traceback(self.proc)
        self.assertEqual(self.proc.returncode, 0,
                         self.proc.stderr[-800:])

    def test_channel_lines(self):
        lines = [line.strip() for line in self.proc.stdout.splitlines()
                 if line.strip()]
        self.assertEqual(lines, MANIFEST["device"]["channels"],
                         "stdout: %r" % self.proc.stdout[-500:])

    def test_existing_commands_still_work(self):
        proc = run_client("read", "temp", scenario="clean")
        self.assertEqual(proc.returncode, 0, proc.stderr[-500:])
        self.assertRegex(proc.stdout, r"^DATA temp \d")


if __name__ == "__main__":
    unittest.main()
