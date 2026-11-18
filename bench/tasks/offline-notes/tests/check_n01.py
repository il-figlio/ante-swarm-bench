"""Check n01: tools/check_links.py reports exactly the planted broken
links, in the `path:LINE -> target` format, exits 1 on this corpus, and
touches no network."""
import re
import subprocess
import sys
import unittest
from pathlib import Path

from tests.kb import MANIFEST, ROOT

SCRIPT = ROOT / "tools" / "check_links.py"
OUT_RE = re.compile(r"^(?:\./)?([^\s:]+\.md):\s*(\d+)\s*->\s*<?(\S+?)>?\s*$")
LINE_TOL = 3

# modules a local-file link checker has no business importing
NET_RE = re.compile(
    r"^\s*(?:import|from)\s+(socket|ssl|urllib|requests|requests_|http|"
    r"httpx|aiohttp|ftplib|smtplib|telnetlib|imaplib|poplib|websocket|"
    r"websockets|asyncio)\b", re.M)
NET_TOKENS = re.compile(
    r"\b(curl|wget|nc |ncat|ssh |scp |pip install)\b")


class TestCheckLinks(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.exists = SCRIPT.is_file()
        cls.src = SCRIPT.read_text(errors="replace") if cls.exists else ""
        cls.proc = None
        cls.reported = []
        if cls.exists:
            cls.proc = subprocess.run(
                [sys.executable, str(SCRIPT)], capture_output=True,
                text=True, timeout=120, cwd=ROOT)
            for line in cls.proc.stdout.splitlines():
                m = OUT_RE.match(line.strip())
                if m:
                    f = m.group(1)
                    if "notes/" in f:  # tolerate absolute/verbose paths
                        f = f[f.index("notes/"):]
                    cls.reported.append((f, int(m.group(2)), m.group(3)))

    def test_script_exists(self):
        self.assertTrue(self.exists, "tools/check_links.py does not exist")

    def test_no_network(self):
        if not self.exists:
            self.fail("tools/check_links.py does not exist")
        bad = NET_RE.findall(self.src) + NET_TOKENS.findall(self.src)
        self.assertFalse(
            bad, f"network-capable imports/commands in the script: {bad}")

    def test_exit_code_signals_broken(self):
        if not self.exists:
            self.fail("tools/check_links.py does not exist")
        self.assertEqual(
            self.proc.returncode, 1,
            f"corpus has broken links; expected exit 1, got "
            f"{self.proc.returncode} (stderr: {self.proc.stderr[-300:]})")

    def test_reports_exact_set(self):
        if not self.exists:
            self.fail("tools/check_links.py does not exist")
        expected = {(b["file"], b["target"])
                    for b in MANIFEST["broken_links"]}
        got = {(f, t) for f, _, t in self.reported}
        self.assertEqual(
            got, expected,
            f"missing: {sorted(expected - got)}; "
            f"extra: {sorted(got - expected)}")

    def test_line_numbers_close(self):
        if not self.exists:
            self.fail("tools/check_links.py does not exist")
        reported = {(f, t): ln for f, ln, t in self.reported}
        for b in MANIFEST["broken_links"]:
            key = (b["file"], b["target"])
            if key not in reported:
                continue  # the set test reports this
            self.assertAlmostEqual(
                reported[key], b["line"], delta=LINE_TOL,
                msg=f"{key}: reported line {reported[key]}, manifest "
                    f"line {b['line']}")


if __name__ == "__main__":
    unittest.main()
