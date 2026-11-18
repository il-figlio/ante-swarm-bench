"""Check f01: POST /notes with an empty or malformed body returns 400, not 500."""
import unittest

from tests.support import http_json, request, running_server


class TestEmptyBody(unittest.TestCase):
    def test_empty_body_is_400(self):
        with running_server() as base:
            status, _ = request("POST", base + "/notes")
            self.assertEqual(status, 400)

    def test_malformed_json_is_400(self):
        with running_server() as base:
            status, _ = request("POST", base + "/notes", b"{not json")
            self.assertEqual(status, 400)

    def test_error_body_is_json(self):
        with running_server() as base:
            status, payload = http_json("POST", base + "/notes")
            self.assertEqual(status, 400)
            self.assertIsInstance(payload, dict)
            self.assertIn("error", payload)

    def test_valid_post_still_works(self):
        with running_server() as base:
            status, payload = http_json(
                "POST", base + "/notes", {"title": "still works"})
            self.assertEqual(status, 201)
            self.assertEqual(payload["note"]["title"], "still works")


if __name__ == "__main__":
    unittest.main()
