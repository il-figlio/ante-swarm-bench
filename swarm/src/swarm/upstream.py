"""Kept-alive HTTP(S) connections to one upstream, shared by the relay and the gateway.

A fresh TLS handshake per model request cost 50-130 ms per hop (measured on the live host). Every harness paid it on every
request, so a harness that makes more requests paid more. Reusing connections removes most of that cost for all of
them alike.

`Pool.post` returns a response whose body the caller reads to the end and then hands back with `done`. A connection
goes back to the pool only after a complete, kept-alive reply. A request on an idle connection that the server has
already closed is retried once on a new connection, because nothing reached the server.
"""
import http.client
import queue
import ssl
import urllib.parse

RETRYABLE = (http.client.RemoteDisconnected, http.client.CannotSendRequest, BrokenPipeError, ConnectionResetError)


class Pool:
    def __init__(self, base: str, timeout_s: float = 300, max_idle: int = 32):
        u = urllib.parse.urlsplit(base.rstrip("/"))
        if u.scheme not in ("http", "https") or not u.hostname:
            raise ValueError(f"bad upstream {base!r}")
        self.scheme, self.host, self.port = u.scheme, u.hostname, u.port
        self.prefix = u.path
        self.timeout_s = timeout_s
        self.idle: queue.LifoQueue = queue.LifoQueue(max_idle)
        self.context = ssl.create_default_context() if u.scheme == "https" else None
        self.opened = 0  # connections made; tests and the audit read it

    def _new(self):
        self.opened += 1
        if self.scheme == "https":
            return http.client.HTTPSConnection(self.host, self.port, timeout=self.timeout_s, context=self.context)
        return http.client.HTTPConnection(self.host, self.port, timeout=self.timeout_s)

    def _take(self):
        try:
            return self.idle.get_nowait(), True
        except queue.Empty:
            return self._new(), False

    def request(self, method: str, path: str, body: bytes | None, headers: dict):
        """Send and return (connection, response). Raises OSError / http.client errors when unreachable."""
        conn, reused = self._take()
        try:
            conn.request(method, self.prefix + path, body=body, headers=headers)
            return conn, conn.getresponse()
        except RETRYABLE:
            conn.close()
            if not reused:
                raise
        except BaseException:
            conn.close()
            raise
        conn = self._new()  # the idle connection had been closed by the server: once more, fresh
        try:
            conn.request(method, self.prefix + path, body=body, headers=headers)
            return conn, conn.getresponse()
        except BaseException:
            conn.close()
            raise

    def post(self, path: str, body: bytes, headers: dict):
        return self.request("POST", path, body, headers)

    def done(self, conn, response, complete: bool = True):
        """Return the connection after the body was read to the end; close it otherwise."""
        if complete and not response.will_close and response.isclosed():
            try:
                self.idle.put_nowait(conn)
                return
            except queue.Full:
                pass
        response.close()
        conn.close()

    def close(self):
        while True:
            try:
                self.idle.get_nowait().close()
            except queue.Empty:
                return
