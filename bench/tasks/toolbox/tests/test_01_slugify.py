import unittest
from toolbox.slugify import slugify

class TestSlugify(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(slugify("Hello, World!"), "hello-world")
    def test_collapses_separators(self):
        self.assertEqual(slugify("  Up   the -- Ante  "), "up-the-ante")
    def test_strips_non_alnum(self):
        self.assertEqual(slugify("C++ & Rust: 2026"), "c-rust-2026")
    def test_empty(self):
        self.assertEqual(slugify("!!!"), "")
