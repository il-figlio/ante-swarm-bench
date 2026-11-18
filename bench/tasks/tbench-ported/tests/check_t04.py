"""Check t04: a server is listening on port 3000 with a GET /fib?n=
endpoint returning the nth Fibonacci number as JSON {"result": ...},
400 on missing / non-integer / negative n.

Ported from the source task's pytest module; urllib (stdlib) stands in
for requests.
"""
import json
import time
import unittest
import urllib.error
import urllib.request

BASE = "http://localhost:3000"


def get(path):
    """GET BASE+path -> (status, parsed_json_or_None). Retries briefly
    on connection errors so a slow-starting server still counts."""
    last = None
    for _ in range(6):
        try:
            with urllib.request.urlopen(BASE + path, timeout=2) as r:
                return r.status, json.loads(r.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read()
            try:
                return e.code, json.loads(body.decode())
            except (ValueError, UnicodeDecodeError):
                return e.code, None
        except (urllib.error.URLError, OSError) as e:
            last = e
            time.sleep(1)
    raise AssertionError("Server is not running on port 3000: %s" % last)


class TestFibServer(unittest.TestCase):
    def test_server_running(self):
        status, _ = get("/fib?n=0")
        self.assertIn(status, (200, 400),
                      "Server is not responding properly")

    def test_fibonacci_endpoint_small_numbers(self):
        for n, expected in {"0": 0, "1": 1, "2": 1, "3": 2, "4": 3,
                            "5": 5, "6": 8, "10": 55}.items():
            status, data = get("/fib?n=%s" % n)
            self.assertEqual(status, 200,
                             "Failed to get response for n=%s" % n)
            self.assertIsNotNone(data)
            self.assertIn("result", data,
                          "Response for n=%s doesn't contain 'result' "
                          "field" % n)
            self.assertEqual(
                data["result"], expected,
                "For n=%s, expected %s but got %s"
                % (n, expected, data["result"]))

    def test_fibonacci_large_number(self):
        n = 100  # F(100) = 354224848179261915075
        status, data = get("/fib?n=%s" % n)
        self.assertEqual(status, 200,
                         "Failed to get response for n=%s" % n)
        self.assertIsNotNone(data)
        self.assertIn("result", data,
                      "Response for n=%s doesn't contain 'result' field"
                      % n)
        self.assertGreater(data["result"], 1000000,
                           "Result for n=%s should be a large number" % n)

    def test_negative_number(self):
        status, _ = get("/fib?n=-5")
        self.assertEqual(status, 400,
                         "Server should return 400 for negative input")

    def test_missing_parameter(self):
        status, _ = get("/fib")
        self.assertEqual(status, 400,
                         "Missing parameter should return 400 Bad Request")

    def test_non_integer_parameter(self):
        status, _ = get("/fib?n=abc")
        self.assertEqual(status, 400,
                         "Non-integer parameter should return 400 Bad "
                         "Request")
        status, _ = get("/fib?n=1.5")
        self.assertEqual(status, 400,
                         "Non-integer parameter should return 400 Bad "
                         "Request")


if __name__ == "__main__":
    unittest.main()
