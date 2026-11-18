import unittest
from toolbox.median import running_median

class TestRunningMedian(unittest.TestCase):
    def test_sequence(self):
        self.assertEqual(running_median([5, 1, 3, 2, 4]), [5, 3.0, 3, 2.5, 3])
    def test_single(self):
        self.assertEqual(running_median([7]), [7])
    def test_empty(self):
        self.assertEqual(running_median([]), [])
