#!/usr/bin/env python3
"""Local preview for the page in site/ (the live launch page): static files from the repository root plus the two
API proxies the page expects on its own origin. Standard library only; loopback only.

    python3 site/preview.py                       # http://127.0.0.1:8088/site/live/live.html
    python3 site/preview.py --lane http://127.0.0.1:8080 --hub http://127.0.0.1:28703

Routes:
  /swarm/live/api/*  -> <lane>/api/*         a `swarm serve` lane (loopback lanes need no ingress secret)
  /swarm/public/*    -> <hub>/swarm/public/* the hub's read-only receipts API
  anything else      -> files under the repository root

Without a lane the live page still opens and shows its offline presets.
"""
import argparse
import http.server
import urllib.error
import urllib.request
from functools import partial
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


class Handler(http.server.SimpleHTTPRequestHandler):
    lane = hub = None

    def _proxy(self, url: str):
        body = None
        if self.command == "POST":
            body = self.rfile.read(min(int(self.headers.get("Content-Length") or 0), 1 << 16))
        req = urllib.request.Request(url, data=body, method=self.command,
                                     headers={k: v for k, v in self.headers.items()
                                              if k.lower() in ("content-type", "accept", "last-event-id")})
        try:
            with urllib.request.urlopen(req, timeout=900) as r:
                self.send_response(r.status)
                for k, v in r.headers.items():
                    if k.lower() in ("content-type", "cache-control"):
                        self.send_header(k, v)
                self.end_headers()
                while chunk := r.read1(65536) if hasattr(r, "read1") else r.read(65536):
                    self.wfile.write(chunk)
                    self.wfile.flush()
        except urllib.error.HTTPError as e:
            self.send_response(e.code)
            self.send_header("Content-Type", e.headers.get("Content-Type", "application/json"))
            self.end_headers()
            self.wfile.write(e.read())
        except (urllib.error.URLError, OSError):
            self.send_error(502, "upstream not running")

    def _route(self) -> bool:
        if self.path.startswith("/swarm/live/api/") and self.lane:
            self._proxy(self.lane.rstrip("/") + "/api/" + self.path[len("/swarm/live/api/"):])
            return True
        if self.path.startswith("/swarm/public/") or self.path == "/swarm/public":
            if self.hub:
                self._proxy(self.hub.rstrip("/") + self.path)
            else:
                self.send_error(503, "no hub configured (--hub)")
            return True
        return False

    def do_GET(self):
        if not self._route():
            super().do_GET()

    def do_POST(self):
        if not self._route():
            self.send_error(405)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--port", type=int, default=8088)
    ap.add_argument("--lane", help="base URL of a local `swarm serve`, e.g. http://127.0.0.1:8080")
    ap.add_argument("--hub", help="base URL of a local hub, e.g. http://127.0.0.1:28703")
    a = ap.parse_args()
    Handler.lane, Handler.hub = a.lane, a.hub
    srv = http.server.ThreadingHTTPServer(("127.0.0.1", a.port), partial(Handler, directory=str(ROOT)))
    print(f"preview on http://127.0.0.1:{a.port}/site/live/live.html", flush=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
