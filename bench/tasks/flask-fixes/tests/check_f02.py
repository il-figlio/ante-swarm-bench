"""Check f02: GET /notes supports ?limit= and ?offset=, newest first."""
import unittest

from tests.support import http_json, reset_store, running_server

TOTAL = 12


class TestPagination(unittest.TestCase):
    def setUp(self):
        reset_store()

    def _seed(self, base):
        ids = []
        for i in range(TOTAL):
            status, payload = http_json(
                "POST", base + "/notes", {"title": "note-%02d" % i})
            self.assertEqual(status, 201)
            ids.append(payload["note"]["id"])
        return ids

    def test_limit(self):
        with running_server() as base:
            self._seed(base)
            status, payload = http_json("GET", base + "/notes?limit=5")
            self.assertEqual(status, 200)
            self.assertEqual(len(payload["notes"]), 5)

    def test_limit_and_offset(self):
        with running_server() as base:
            ids = self._seed(base)
            expected = ids[::-1][5:10]  # newest first, second page of 5
            status, payload = http_json(
                "GET", base + "/notes?limit=5&offset=5")
            self.assertEqual(status, 200)
            self.assertEqual([n["id"] for n in payload["notes"]], expected)

    def test_offset_past_end(self):
        with running_server() as base:
            self._seed(base)
            status, payload = http_json(
                "GET", base + "/notes?limit=5&offset=10")
            self.assertEqual(status, 200)
            self.assertEqual(len(payload["notes"]), TOTAL - 10)


if __name__ == "__main__":
    unittest.main()
