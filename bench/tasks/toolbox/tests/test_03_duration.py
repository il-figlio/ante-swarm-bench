import unittest
from toolbox.duration import parse_duration

class TestParseDuration(unittest.TestCase):
    def test_full(self):
        self.assertEqual(parse_duration("1h30m15s"), 5415)
    def test_partial(self):
        self.assertEqual(parse_duration("45s"), 45)
        self.assertEqual(parse_duration("2m"), 120)
        self.assertEqual(parse_duration("3h"), 10800)
    def test_rejects(self):
        for bad in ["", "10", "1x", "h", "1h1h", "-5s"]:
            with self.assertRaises(ValueError, msg=bad):
                parse_duration(bad)
