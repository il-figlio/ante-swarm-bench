"""Check v02: the corrupt scenario stuffs raw garbage bytes and a
bad-checksum frame into the middle of a stream. The client must drop
them, warn on stderr, resync, and still print every good SAMPLE in
order — and nothing else on stdout."""
import re
import unittest

from tests.harness import (PackTestCase, expected_samples,
                           run_client, stream_samples)

WARN_RE = re.compile(
    r"(?i)(warn|bad|corrupt|malform|invalid|checksum|magic|resync|"
    r"skipp|frame|garbage|noise|drop)")


class TestCorruptStream(PackTestCase):
    @classmethod
    def setUpClass(cls):
        cls.proc = run_client("stream", "8", scenario="corrupt")

    def test_clean_source(self):
        self.assert_clean_source()

    def test_exits_zero(self):
        self.assert_no_traceback(self.proc)
        self.assertEqual(self.proc.returncode, 0,
                         self.proc.stderr[-800:])

    def test_every_good_frame_in_order(self):
        self.assertEqual(stream_samples(self.proc.stdout),
                         expected_samples(8),
                         "stdout: %r" % self.proc.stdout[-800:])

    def test_stdout_is_only_samples(self):
        bad = [line for line in self.proc.stdout.splitlines()
               if line.strip() and not line.startswith("SAMPLE ")]
        self.assertFalse(
            bad, "non-SAMPLE lines on stdout: %r" % bad)

    def test_warned_on_stderr(self):
        self.assertRegex(
            self.proc.stderr, WARN_RE,
            "expected a dropped-frame warning on stderr; got %r"
            % self.proc.stderr[-500:])


if __name__ == "__main__":
    unittest.main()
