"""Shared helpers for tests that need a live server.

    with running_server() as base:
        status, payload = http_json("GET", base + "/notes")
"""
import contextlib
import json
import threading
import urllib.error
import urllib.request

from app.routes import app, store


@contextlib.contextmanager
def running_server():
    """Yield the base URL of the app serving on an ephemeral port."""
    httpd = app.make_server("127.0.0.1", 0)
    thread = threading.Thread(target=httpd.serve_forever, daemon=True)
    thread.start()
    try:
        yield "http://127.0.0.1:%d" % httpd.server_address[1]
    finally:
        httpd.shutdown()
        httpd.server_close()
        thread.join()


def request(method, url, data=None):
    """Raw HTTP request. `data` is bytes or None. Returns (status, body_bytes)."""
    req = urllib.request.Request(url, data=data, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        body = exc.read()
        exc.close()
        return exc.code, body


def http_json(method, url, obj=None):
    """HTTP request with a JSON body (or none when obj is None).

    Returns (status, decoded_json_or_None).
    """
    if obj is None:
        status, raw = request(method, url)
    else:
        status, raw = request(method, url, json.dumps(obj).encode("utf-8"))
    return status, (json.loads(raw) if raw else None)


def reset_store():
    """Empty the note store between tests."""
    store.clear()
