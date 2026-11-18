"""Check f03: cli.py accepts --port and binds that port."""
import socket
import subprocess
import sys
import time
import unittest
import urllib.request


def _free_port():
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    return port


class TestPortFlag(unittest.TestCase):
    def test_port_flag_binds(self):
        port = _free_port()
        proc = subprocess.Popen(
            [sys.executable, "cli.py", "--port", str(port)],
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        try:
            deadline = time.time() + 15
            up = False
            while time.time() < deadline:
                if proc.poll() is not None:
                    break
                try:
                    with urllib.request.urlopen(
                            "http://127.0.0.1:%d/health" % port,
                            timeout=1) as resp:
                        up = resp.status == 200
                        break
                except Exception:
                    time.sleep(0.2)
            output = ""
            if proc.poll() is not None:
                output = proc.stdout.read() or ""
            self.assertTrue(
                up,
                "server did not come up on --port %d; process output: %s"
                % (port, output[-2000:]))
        finally:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()


if __name__ == "__main__":
    unittest.main()
