"""Suite run by `make check`."""
import unittest

from tests.support import http_json, running_server, reset_store


class TestNotesApi(unittest.TestCase):
    def setUp(self):
        reset_store()

    def test_health(self):
        with running_server() as base:
            status, payload = http_json("GET", base + "/health")
            self.assertEqual(status, 200)
            self.assertEqual(payload, {"status": "ok"})

    def test_create_returns_201_with_note(self):
        with running_server() as base:
            status, payload = http_json(
                "POST", base + "/notes",
                {"title": "first", "body": "hello", "tags": ["x"]})
            self.assertEqual(status, 201)
            self.assertEqual(payload["note"]["title"], "first")
            self.assertEqual(payload["note"]["body"], "hello")
            self.assertEqual(payload["note"]["tags"], ["x"])

    def test_get_note(self):
        with running_server() as base:
            _, created = http_json("POST", base + "/notes", {"title": "t"})
            note_id = created["note"]["id"]
            status, payload = http_json("GET", base + "/notes/%d" % note_id)
            self.assertEqual(status, 200)
            self.assertEqual(payload["note"]["id"], note_id)

    def test_get_missing_is_404(self):
        with running_server() as base:
            status, payload = http_json("GET", base + "/notes/9999")
            self.assertEqual(status, 404)
            self.assertIn("error", payload)

    def test_delete_note(self):
        with running_server() as base:
            _, created = http_json("POST", base + "/notes", {"title": "gone"})
            note_id = created["note"]["id"]
            status, payload = http_json(
                "DELETE", base + "/notes/%d" % note_id)
            self.assertEqual(status, 200)
            self.assertTrue(payload["deleted"])
            status, _ = http_json("GET", base + "/notes/%d" % note_id)
            self.assertEqual(status, 404)

    def test_list_is_newest_first(self):
        with running_server() as base:
            for title in ("older", "middle", "newer"):
                http_json("POST", base + "/notes", {"title": title})
            status, payload = http_json("GET", base + "/notes")
            self.assertEqual(status, 200)
            self.assertEqual(payload["notes"][0]["title"], "newer")
            self.assertEqual(payload["notes"][2]["title"], "older")

    def test_search_is_case_insensitive(self):
        with running_server() as base:
            http_json("POST", base + "/notes",
                      {"title": "Buy Milk Today"})
            http_json("POST", base + "/notes", {"title": "unrelated"})
            status, payload = http_json(
                "GET", base + "/notes/search?q=milk")
            self.assertEqual(status, 200)
            self.assertEqual(len(payload["notes"]), 1)
            self.assertEqual(payload["notes"][0]["title"], "Buy Milk Today")


if __name__ == "__main__":
    unittest.main()
