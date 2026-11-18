import unittest
from toolbox.wordfreq import word_frequencies

class TestWordFrequencies(unittest.TestCase):
    def test_counts(self):
        self.assertEqual(word_frequencies("a A b, b. B!"), {"a": 2, "b": 3})
    def test_empty(self):
        self.assertEqual(word_frequencies(""), {})
    def test_apostrophes_kept(self):
        self.assertEqual(word_frequencies("don't Don't stop"), {"don't": 2, "stop": 1})
