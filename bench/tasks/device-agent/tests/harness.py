"""Shared helpers for the device-agent checks.

Every check spawns the real client CLI against tests/fake_device.py —
the same contract an operator uses:

    proc = run_client("stream", "8", scenario="corrupt")

The fake speaks the wire protocol deterministically out of
tests/manifest.json. No clocks, no sleeps: the pod either answers or
its pipe closes, and the checks wait on process exit, never on time.
"""
import ast
import json
import re
import shlex
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CLIENT = ROOT / "sensor_client.py"
FAKE = ROOT / "tests" / "fake_device.py"
MANIFEST = json.loads((ROOT / "tests" / "manifest.json").read_text())

SIZE_BUDGET = 24 * 1024   # the README's deploy-image budget

SAMPLE_RE = re.compile(r"^SAMPLE (\d+) (\S+) (\S+)\s*$")


def device_cmd(scenario, state_dir=None):
    """The --device string for a fake pod running `scenario`."""
    parts = [sys.executable, str(FAKE), "--scenario", scenario]
    if state_dir is not None:
        parts += ["--state-dir", str(state_dir)]
    return shlex.join(parts)


def run_client(*cli_args, scenario="clean", state_dir=None, timeout=60):
    """Run the client end to end; returns the CompletedProcess."""
    return subprocess.run(
        [sys.executable, str(CLIENT), "--device",
         device_cmd(scenario, state_dir)] + [str(a) for a in cli_args],
        capture_output=True, text=True, errors="replace",
        timeout=timeout, cwd=ROOT)


def stream_samples(text):
    """[(seq, chan, value)] for each SAMPLE line in client output."""
    out = []
    for line in text.splitlines():
        match = SAMPLE_RE.match(line)
        if match:
            out.append((int(match.group(1)), match.group(2),
                        float(match.group(3))))
    return out


def expected_samples(count):
    """[(seq, chan, value)] the pod should emit for STREAM `count`."""
    return [(seq, chan, float(value)) for seq, (chan, value)
            in enumerate(MANIFEST["stream"][:count])]


def imported_modules(path):
    """Top-level module names a Python source file imports."""
    tree = ast.parse(Path(path).read_text())
    mods = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            mods.update(a.name.split(".")[0] for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            mods.add(node.module.split(".")[0])
    return mods


def source_files():
    """Python files that ship in the image — everything outside
    tests/ (which is bench scaffolding, not shipped code)."""
    files = sorted(ROOT.glob("*.py"))
    tools = ROOT / "tools"
    if tools.is_dir():
        files += sorted(tools.rglob("*.py"))
    return files


class PackTestCase(unittest.TestCase):
    """Assertions every check in this pack wants."""

    def assert_clean_source(self):
        """The deploy-image rules: every shipped import inside the
        manifest allowlist (standard library, no network modules, no
        new dependencies) and the client inside its size budget."""
        allow = set(MANIFEST["import_allowlist"])
        local = {p.stem for p in ROOT.glob("*.py")}
        for path in source_files():
            mods = imported_modules(path)
            bad = sorted(m for m in mods
                         if m not in allow and m not in local)
            self.assertFalse(
                bad, "%s imports outside the manifest allowlist: %s"
                     % (path.name, bad))
        size = CLIENT.stat().st_size
        self.assertLessEqual(
            size, SIZE_BUDGET,
            "sensor_client.py is %d bytes; image budget is %d"
            % (size, SIZE_BUDGET))

    def assert_no_traceback(self, proc):
        self.assertNotIn(
            "Traceback", proc.stderr,
            "client crashed: %s" % proc.stderr[-800:])
