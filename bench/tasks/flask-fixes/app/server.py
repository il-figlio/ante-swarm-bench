"""A minimal Flask-ish web layer on top of http.server.

Routes are registered with a decorator, handlers receive a Request and
return ``(status, headers, body)`` via the ``json_response`` helper.
"""
import json
import re
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


class Request:
    """One inbound HTTP request."""

    def __init__(self, method, path, query, headers, body):
        self.method = method
        self.path = path
        self.query = query          # dict[str, list[str]]
        self.headers = headers
        self.body = body            # bytes, may be empty

    def arg(self, name, default=None):
        """First value of a query parameter, or default."""
        values = self.query.get(name)
        if not values:
            return default
        return values[0]

    def json(self):
        """Decode the request body as JSON."""
        return json.loads(self.body.decode("utf-8"))


def json_response(payload, status=200):
    body = json.dumps(payload).encode("utf-8")
    return status, {"Content-Type": "application/json"}, body


def _compile(pattern):
    """'/notes/<int:note_id>' -> regex with a named int group."""
    parts = []
    for seg in pattern.strip("/").split("/"):
        m = re.fullmatch(r"<int:(\w+)>", seg)
        if m:
            parts.append(r"(?P<%s>\d+)" % m.group(1))
        else:
            parts.append(re.escape(seg))
    return re.compile("^/" + "/".join(parts) + "$")


class App:
    def __init__(self):
        self.routes = []  # list of (method, compiled_pattern, handler)

    def route(self, method, pattern):
        rx = _compile(pattern)

        def decorator(fn):
            self.routes.append((method, rx, fn))
            return fn

        return decorator

    def dispatch(self, req):
        """Find a handler for req and call it. Returns (status, headers, body)."""
        for method, rx, handler in self.routes:
            if method != req.method:
                continue
            match = rx.match(req.path)
            if not match:
                continue
            kwargs = {k: int(v) for k, v in match.groupdict().items()}
            try:
                return handler(req, **kwargs)
            except Exception as exc:
                return json_response(
                    {"error": "%s: %s" % (type(exc).__name__, exc)}, 500)
        return json_response({"error": "not found"}, 404)

    def make_server(self, host, port):
        """Build (but do not start) an HTTP server for this app."""
        app = self

        class Handler(BaseHTTPRequestHandler):
            def _handle(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                split = urlsplit(self.path)
                req = Request(self.command, split.path,
                              parse_qs(split.query), self.headers, body)
                status, headers, payload = app.dispatch(req)
                self.send_response(status)
                for key, value in headers.items():
                    self.send_header(key, value)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = _handle
            do_POST = _handle
            do_PUT = _handle
            do_DELETE = _handle

            def log_message(self, *args):
                pass

        return ThreadingHTTPServer((host, port), Handler)

    def serve(self, host, port):
        """Serve forever on host:port."""
        httpd = self.make_server(host, port)
        bound_host, bound_port = httpd.server_address[:2]
        print("listening on http://%s:%d" % (bound_host, bound_port))
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass
        finally:
            httpd.server_close()
