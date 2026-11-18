"""Check v03: the flaky scenario drops the pipe three SAMPLE frames
into the first connection's stream (truncated mid-frame, like a real
brown-out). The client must reconnect — respawn the device command,
redo the HELLO handshake — and resume so every sample prints exactly
once, in order."""
import tempfile
import unittest
from pathlib import Path

from tests.harness import (PackTestCase, expected_samples,
                           run_client, stream_samples)


class TestReconnect(PackTestCase):
    @classmethod
    def setUpClass(cls):
        cls.state_dir = tempfile.mkdtemp(prefix="podstate-")
        cls.proc = run_client("stream", "8", scenario="flaky",
                              state_dir=cls.state_dir, timeout=90)

    def test_clean_source(self):
        self.assert_clean_source()

    def test_pod_really_died(self):
        """Scenario integrity: the first connection dropped."""
        marker = Path(self.state_dir) / "flaky-died"
        self.assertTrue(marker.exists(),
                        "the fake pod never recorded its brown-out "
                        "— is the check set up right?")

    def test_exits_zero(self):
        self.assert_no_traceback(self.proc)
        self.assertEqual(self.proc.returncode, 0,
                         self.proc.stderr[-800:])

    def test_every_sample_once_in_order(self):
        self.assertEqual(stream_samples(self.proc.stdout),
                         expected_samples(8),
                         "stdout: %r" % self.proc.stdout[-800:])

    def test_stdout_is_only_samples(self):
        bad = [line for line in self.proc.stdout.splitlines()
               if line.strip() and not line.startswith("SAMPLE ")]
        self.assertFalse(
            bad, "non-SAMPLE lines on stdout: %r" % bad)


if __name__ == "__main__":
    unittest.main()
