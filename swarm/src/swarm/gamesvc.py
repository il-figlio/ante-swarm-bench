"""The service a live game's Antes call: one rules model, HTTP over a Unix socket in a private directory, a token per
agent honoured only from that agent's process tree. The same contract as MazeService (src/swarm/maze.py) and the same
peer-pid helpers, imported from there unchanged; MazeService itself is not touched, so maze evals behave as before.

The model is any object with `call(agent_id, op, arg) -> dict`. Every honoured call runs under one lock, then
`on_call(agent_id, op, arg, result, model)` with the lock still held, so events leave in call order. The server's own
work on the model (the clock, the human player's moves) goes through `read(fn)`, under the same lock.
"""
from __future__ import annotations

import json
import secrets
import shutil
import socketserver
import struct
import sys
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler
from pathlib import Path

GATE_WAIT_S = 40.0

from .maze import BIND_WAIT_S, MAX_BODY, PeerUnavailable, _cap, descends_from, peer_check_problem, peer_pid

CLIENT_TEMPLATE = '''#!/usr/bin/env python3
"""<NAME> client. One command is one call: it prints the service's JSON answer on one line."""
import http.client
import json
import socket
import sys

USAGE = <USAGE>
OPS = <OPS>


class UnixConnection(http.client.HTTPConnection):
    """HTTP over the service's Unix socket: the service is on this machine and is never reached through the network."""

    def __init__(self, path):
        super().__init__("localhost", timeout=30)
        self.unix_path = path

    def connect(self):
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.sock.settimeout(self.timeout)
        self.sock.connect(self.unix_path)


def main(argv):
    # OPS: op -> its argument count; -1 takes the rest of the line (one word or more), as `note <text>`
    if not argv or argv[0] not in OPS or (len(argv) < 2 if OPS[argv[0]] < 0 else len(argv) - 1 != OPS[argv[0]]):
        print(json.dumps({"error": USAGE}))
        return 2
    try:
        with open(".<NAME>.json") as f:
            conn = json.load(f)
        body = json.dumps({"token": conn["token"], "op": argv[0], "arg": " ".join(argv[1:]) if len(argv) > 1 else None})
        client = UnixConnection(conn["socket"])
        try:
            client.request("POST", "/call", body=body.encode(), headers={"Content-Type": "application/json"})
            response = client.getresponse()
            print(response.read().decode().strip())
            return 0 if response.status == 200 else 1
        finally:
            client.close()
    except Exception as err:
        print(json.dumps({"error": "transport: " + str(err)}))
        return 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
'''


def client_source(name: str, ops: dict, usage: str) -> str:
    """The `./<name>` script an agent runs. It reads `.<name>.json` ({socket, token}) from its working directory."""
    return (CLIENT_TEMPLATE.replace("<NAME>", name).replace("<USAGE>", json.dumps(usage))
            .replace("<OPS>", json.dumps(ops)))


class GameService:
    def __init__(self, model, name: str = "game", on_call=None, socket_dir: Path | None = None, gate=None,
                 free_ops=("look",)):
        self.model, self.name, self.on_call = model, name, on_call
        # `gate` (a threading.Event): ops other than free_ops wait for it (up to GATE_WAIT_S) before they run, so an
        # agent that is ready early cannot act before the round's clock starts. Looking is always allowed.
        self.gate, self.free_ops = gate, tuple(free_ops)
        self._socket_dir = socket_dir
        self._lock = threading.Lock()
        self._bound = threading.Condition(self._lock)
        self._tokens: dict[str, str] = {}
        self._roots: dict[str, int] = {}
        self._server = None
        self._thread: threading.Thread | None = None
        self._dir: Path | None = None
        self.socket_path: Path | None = None
        self.rows: list[dict] = []
        self._t0 = 0.0
        self.forbidden = 0
        self.stolen = 0
        self._stopped = False

    def start(self) -> str:
        problem = peer_check_problem()
        if problem:
            raise PeerUnavailable(f"cannot tie tokens to agent processes here: {problem}")
        base = self._socket_dir or tempfile.gettempdir()
        if len(str(base)) > 60:
            base = "/tmp"
        self._dir = Path(tempfile.mkdtemp(prefix=f"swarm-{self.name}-", dir=base))   # mode 0700
        self.socket_path = self._dir / f"{self.name}.sock"
        service = self

        class Handler(BaseHTTPRequestHandler):
            def do_POST(self):
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                except ValueError:
                    length = -1
                if not 0 <= length <= MAX_BODY:
                    self.close_connection = True
                    return self._send(400, {"error": "bad_length"})
                data = self.rfile.read(length)
                if self.path != "/call":
                    return self._send(404, {"error": "not_found"})
                try:
                    body = json.loads(data)
                    if not isinstance(body, dict):
                        raise ValueError("body must be an object")
                except ValueError:
                    return self._send(400, {"error": "bad_request"})
                try:
                    peer = peer_pid(self.request)
                except (PeerUnavailable, OSError, struct.error):
                    peer = None
                self._send(*service._handle(body, peer))

            def _send(self, status: int, payload: dict):
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def log_message(self, *args):
                pass

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True
            request_queue_size = 64

            def handle_error(self, request, client_address):
                if not isinstance(sys.exc_info()[1], (BrokenPipeError, ConnectionResetError)):
                    super().handle_error(request, client_address)

        try:
            self._server = Server(str(self.socket_path), Handler)
        except BaseException:
            shutil.rmtree(self._dir, ignore_errors=True)
            raise
        self._t0 = time.monotonic()
        self._thread = threading.Thread(target=self._server.serve_forever, name=f"{self.name}-service", daemon=True)
        self._thread.start()
        return str(self.socket_path)

    def register(self, agent_id: str) -> str:
        with self._lock:
            token = secrets.token_hex(16)
            self._tokens[token] = agent_id
            return token

    def bind(self, agent_id: str, pid: int) -> None:
        with self._lock:
            if not isinstance(pid, int) or isinstance(pid, bool) or pid <= 1:
                raise ValueError(f"cannot bind a token to pid {pid!r}")
            self._roots[agent_id] = pid
            self._bound.notify_all()

    def _handle(self, body: dict, peer: int | None) -> tuple[int, dict]:
        token, op, arg = body.get("token"), body.get("op"), body.get("arg")
        agent_id = self._tokens.get(token) if isinstance(token, str) else None
        refusal = None
        if agent_id is not None:
            with self._lock:
                self._bound.wait_for(lambda: agent_id in self._roots or self._stopped, timeout=BIND_WAIT_S)
                root = self._roots.get(agent_id)
            if root is None:
                refusal = "unbound"
            elif peer is None:
                refusal = "peer_unknown"
            elif not descends_from(peer, root):
                refusal = "stolen_token"
        if agent_id is not None and not refusal and self.gate is not None and op not in self.free_ops:
            self.gate.wait(GATE_WAIT_S)
        with self._lock:
            now = round(time.monotonic() - self._t0, 4)
            if agent_id is None or refusal:
                logged, _ = _cap(op if isinstance(op, str) else None)
                self.rows.append({"t": now, "agent": None, "op": logged, "ok": False, "error": refusal or "forbidden",
                                  **({"owner": agent_id} if refusal else {})})
                if refusal:
                    self.stolen += 1
                else:
                    self.forbidden += 1
                return 403, {"error": "forbidden", "done": False}
            if self._stopped:
                return 410, {"error": "stopped", "done": True}
            if not isinstance(arg, (str, type(None))):
                arg = None
            result = self.model.call(agent_id, op if isinstance(op, str) else "", arg)
            logged_op, _ = _cap(op)
            logged_arg, _ = _cap(arg)
            self.rows.append({"t": now, "agent": agent_id, "op": logged_op, "arg": logged_arg, "ok": "error" not in result})
            if self.on_call is not None:
                self.on_call(agent_id, op, arg, result, self.model)
            return 200, result

    def read(self, fn):
        """fn(model) under the service lock: the server's own work on the model (the clock, the human's moves)."""
        with self._lock:
            return fn(self.model)

    @property
    def running(self) -> bool:
        return self._server is not None and not self._stopped

    def stop(self) -> None:
        if self._stopped or self._server is None:
            return
        with self._lock:
            self._stopped = True
            self._bound.notify_all()
        try:
            self._server.shutdown()
            self._server.server_close()
            if self._thread is not None:
                self._thread.join(timeout=5)
        finally:
            if self._dir is not None:
                shutil.rmtree(self._dir, ignore_errors=True)
