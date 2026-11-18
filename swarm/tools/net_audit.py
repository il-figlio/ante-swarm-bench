#!/usr/bin/env python3
"""Check that a harness makes no connection except to the scripted model.

Runs a plan's first cell once and, while it runs, (1) sets HTTP(S)_PROXY / ALL_PROXY to a local recorder that
logs and refuses every request, and (2) polls the host's sockets for connections owned by the harness's
processes. Anything that is not loopback to the scripted server is reported. Proxy-ignoring clients are caught
by the socket poll; a connection shorter than the poll interval (0.05 s) could be missed, so this is evidence,
not proof. Usage: net_audit.py PLAN.json OUT_DIR
"""
import json
import os
import re
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from swarm import runner  # noqa: E402
from swarm.harnesses import base  # noqa: E402
from swarm.sandbox import tagged_processes  # noqa: E402

PROXY_LOG: list[str] = []


def proxy_server():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(64)

    def loop():
        while True:
            conn, _ = srv.accept()
            try:
                conn.settimeout(1)
                data = conn.recv(2048).decode("latin-1", "replace").splitlines()
                PROXY_LOG.append(data[0] if data else "")
                conn.sendall(b"HTTP/1.1 502 Refused by net_audit\r\nContent-Length: 0\r\n\r\n")
            except OSError:
                pass
            finally:
                conn.close()

    threading.Thread(target=loop, daemon=True).start()
    return srv.getsockname()[1]


def sockets():
    """(pid, endpoints) for sockets owned by processes of this run (found by their SWARM_ATTEMPT tag)."""
    pids = set(tagged_processes("c", prefix=True))
    found = set()
    if not pids:
        return found
    if sys.platform == "darwin":
        out = subprocess.run(["lsof", "-nP", "-i", "-a", "-p", ",".join(map(str, pids)), "-Fcn"], capture_output=True, text=True).stdout
        command = None
        for line in out.splitlines():
            if line.startswith("c"):
                command = line[1:]
            elif line.startswith("n") and command:
                found.add((command, line[1:]))
    else:
        out = subprocess.run(["ss", "-H", "-tunap"], capture_output=True, text=True).stdout
        for line in out.splitlines():
            m = re.search(r'users:\(\("([^"]+)",pid=(\d+)', line)
            if m and int(m.group(2)) in pids:
                found.add((m.group(1), " ".join(line.split()[4:6])))
    return found


def main(plan_path, out):
    port = proxy_server()
    proxy = f"http://127.0.0.1:{port}"
    original = base.Harness.environment

    def environment(self, binary, home):
        env = original(self, binary, home)
        env.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy, ALL_PROXY=proxy, http_proxy=proxy, https_proxy=proxy,
                   NO_PROXY="127.0.0.1,localhost", no_proxy="127.0.0.1,localhost")
        return env

    base.Harness.environment = environment
    plan = json.loads(Path(plan_path).read_text())
    seen, stop = set(), threading.Event()

    def poll():
        while not stop.is_set():
            seen.update(sockets())
            time.sleep(0.05)

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    bundle = runner.run(Path(plan_path), Path(out))
    stop.set()
    thread.join(2)
    external = sorted(s for s in seen if not re.match(r"^(127\.0\.0\.1|\[::1\]|localhost|\*|::1)[:\]]?", s[1].split("->")[-1])
                      and "127.0.0.1" not in s[1] and "[::1]" not in s[1])
    report = {"bundle": str(bundle), "plan_sha256": plan["plan_sha256"], "proxy_requests": PROXY_LOG,
              "sockets_seen": sorted(f"{c} {e}" for c, e in seen), "external_sockets": [f"{c} {e}" for c, e in external]}
    print(json.dumps(report, indent=2))
    return 0 if not PROXY_LOG and not external else 1


if __name__ == "__main__":
    sys.exit(main(*sys.argv[1:3]))
