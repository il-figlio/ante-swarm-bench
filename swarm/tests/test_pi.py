"""Pi coding agent support: locating the Node entry script, the provider config Swarm writes, argv, tool mapping.

The live run needs a real pi install and the registry entry; point SWARM_PI_BINARY at pi's bin shim or entry
script (e.g. ~/harnesses/pi/node_modules/.bin/pi), otherwise `pi` on PATH is used, else it is skipped.
"""
import json
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from helpers import OK_SCRIPT, make_spec, make_task, tempdir  # noqa: E402
from swarm import harnesses  # noqa: E402
from swarm.harnesses.pi import Pi  # noqa: E402
from swarm.scripted import map_step  # noqa: E402

PI_TOOLS = ["bash", "edit", "read", "write"]  # what pi 1.5.2 offers by default


def fake_install(base: Path) -> tuple[Path, Path]:
    """An npm prefix shaped like a pi install, with a stand-in node beside it: (bin shim, node)."""
    package = base / "node_modules" / "@earendil-works" / "pi-coding-agent"
    (package / "dist" / "bundle").mkdir(parents=True)
    (package / "package.json").write_text(json.dumps({"name": "@earendil-works/pi-coding-agent", "version": "9.9.9"}))
    entry = package / "dist" / "bundle" / "cli.js"
    entry.write_text("#!/usr/bin/env node\n")
    (base / "node_modules" / ".bin").mkdir()
    shim = base / "node_modules" / ".bin" / "pi"
    shim.symlink_to("../@earendil-works/pi-coding-agent/dist/bundle/cli.js")
    node = base / "bin" / "node"
    node.parent.mkdir()
    # Stands in for node: `node cli.js --version` and `node --version` both print a version.
    node.write_text("#!/bin/sh\necho 9.9.9\n")
    node.chmod(0o755)
    return shim, node


class PiAdapterTests(unittest.TestCase):
    def test_locate_resolves_the_bin_shim_to_the_package_entry_and_pins_node(self):
        with tempdir() as d:
            shim, node = fake_install(Path(d).resolve())
            pi = Pi()
            entry = pi.locate(str(shim))
            self.assertEqual(entry.name, "cli.js")
            self.assertTrue(str(entry).endswith("@earendil-works/pi-coding-agent/dist/bundle/cli.js"))
            info = pi.fingerprint(entry)
            self.assertEqual(info["node"], str(node))
            self.assertEqual(info["version"], "pi 9.9.9")
            self.assertEqual(len(info["agent_sha256"]), 64)  # the whole package, not the 160-byte loader
            self.assertEqual(len(info["node_sha256"]), 64)

    def test_standalone_executable_runs_directly_without_node(self):
        with tempdir() as d:
            root = Path(d).resolve()
            (root / "package.json").write_text(json.dumps({"name": "@earendil-works/pi-coding-agent", "version": "9.9.9"}))
            binary = root / "pi"
            binary.write_bytes(b"\x7fELF not really")
            binary.chmod(0o755)
            pi = Pi()
            self.assertEqual(pi.locate(str(binary)), binary)
            info = pi.fingerprint(binary)
            self.assertEqual((info["node"], info["node_sha256"]), (None, None))
            pi.frozen = info
            self.assertEqual(pi.argv(binary, "go", root, "m-1", "low")[:2], [str(binary), "--print"])

    def test_locate_refuses_a_file_outside_a_pi_package(self):
        with tempdir() as d:
            wrapper = Path(d) / "pi"
            wrapper.write_text("#!/bin/sh\nexec node cli.js \"$@\"\n")
            self.assertIsNone(Pi().locate(str(wrapper)))
            self.assertIsNone(Pi().locate(str(Path(d) / "missing")))

    def test_argv_launches_node_explicitly_headless_and_ephemeral(self):
        with tempdir() as d:
            shim, node = fake_install(Path(d).resolve())
            pi = Pi()
            entry = pi.locate(str(shim))
            pi.frozen = pi.fingerprint(entry)
            argv = pi.argv(entry, "-do it", Path(d), "m-1", "medium")
            self.assertEqual(argv[:3], [str(node), str(entry), "--print"])
            for flag in ("--no-session", "--no-skills", "--no-extensions", "--no-approve", "--offline"):
                self.assertIn(flag, argv)
            self.assertEqual(argv[argv.index("--provider") + 1], "swarm")
            self.assertEqual(argv[argv.index("--model") + 1], "m-1")
            self.assertEqual(argv[argv.index("--thinking") + 1], "medium")
            self.assertEqual(argv[-2:], ["--", "-do it"])  # a prompt starting with a dash stays a message

    def test_prepare_writes_one_local_provider_and_switches_off_network_extras(self):
        with tempdir() as d:
            home = Path(d)
            env = Pi().prepare(home, "http://127.0.0.1:9/s/tok/v1", "m-1", "medium", 16384)
            agent = home / ".pi" / "agent"
            self.assertEqual(env["PI_CODING_AGENT_DIR"], str(agent))
            self.assertEqual((env["PI_OFFLINE"], env["PI_SKIP_VERSION_CHECK"], env["PI_TELEMETRY"]), ("1", "1", "0"))
            provider = json.loads((agent / "models.json").read_text())["providers"]["swarm"]
            self.assertEqual(provider["baseUrl"], "http://127.0.0.1:9/s/tok/v1")
            self.assertEqual(provider["api"], "openai-responses")
            (model,) = provider["models"]
            self.assertEqual((model["id"], model["maxTokens"], model["reasoning"]), ("m-1", 16384, True))
            settings = json.loads((agent / "settings.json").read_text())
            self.assertIs(settings["enableInstallTelemetry"], False)
            self.assertEqual(settings["cacheWarming"], "off")
            self.assertEqual(settings["defaultProjectTrust"], "never")
            self.assertEqual(set(home.rglob("*")), {home / ".pi", agent, agent / "models.json", agent / "settings.json"})

    def test_options_are_rejected(self):
        with self.assertRaises(ValueError):
            Pi({"telemetry": "on"}).validate_options()
        Pi().validate_options()

    def test_script_steps_map_onto_pis_lowercase_tools(self):
        self.assertEqual(map_step("/w", {"op": "read", "path": "a.txt"}, PI_TOOLS), ("read", {"path": "/w/a.txt"}, None))
        self.assertEqual(map_step("/w", {"op": "write", "path": "a.txt", "content": "x"}, PI_TOOLS),
                         ("write", {"path": "/w/a.txt", "content": "x"}, None))
        self.assertEqual(map_step("/w", {"op": "bash", "command": "ls"}, PI_TOOLS), ("bash", {"command": "ls"}, None))


def real_pi() -> Path | None:
    return Pi().locate(os.environ.get("SWARM_PI_BINARY"))


@unittest.skipUnless("pi" in harnesses.REGISTRY and real_pi(), "pi is not installed or not registered")
class PiLiveTests(unittest.TestCase):
    def test_real_pi_completes_scripted_task_with_native_tools(self):
        from swarm.plan import freeze
        from swarm.runner import run

        with tempdir() as d:
            base = Path(d)
            make_task(base / "tasks", "one", OK_SCRIPT)
            plan = freeze(make_spec(base, harnesses=("pi",), levels=(1,)), {"pi": str(real_pi())})
            path = base / "plan.json"
            path.write_text(json.dumps(plan))
            bundle = run(path, base / "bundle")
            record = json.loads((bundle / "attempts.jsonl").read_text().splitlines()[0])
            self.assertTrue(record["passed"], record)
            self.assertEqual(record["requests"]["tools_used"], ["bash", "read", "write"])
            self.assertEqual(record["requests"]["errors"], [])


if __name__ == "__main__":
    unittest.main()
