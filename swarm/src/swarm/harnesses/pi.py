"""Pi coding agent (`pi -p`, npm @earendil-works/pi-coding-agent, formerly @mariozechner/pi-coding-agent),
an unmodified third-party Node program, pointed at the scripted model.

Pi reads PI_CODING_AGENT_DIR/models.json. Each attempt gets a fresh agent dir inside its fresh HOME with one
custom provider (`swarm`, api = "openai-responses") whose baseUrl is the attempt's scripted URL, so every model
call stays on this machine and no provider, catalog or update service is contacted. The settings below are the
fair-play set: they switch off install telemetry, the version check, catalog refreshes and prompt-cache warming
(PI_OFFLINE, PI_SKIP_VERSION_CHECK, PI_TELEMETRY=0, enableInstallTelemetry and cacheWarming off), mirror Ante's
own off-switches (no session save, no skills, no extensions, project-local .pi files ignored) and leave
everything else at Pi's defaults: the default tools (read, bash, edit, write), AGENTS.md/CLAUDE.md context
files (as Codex and Claude Code read theirs), compaction and agent-level retry.

Two install shapes are accepted. The npm package's entry is a `#!/usr/bin/env node` script and an attempt's
PATH holds no node, so the plan pins both: `path` and `sha256` are pi's entry script, `agent_sha256` the whole
installed package tree (the entry script is a 160-byte loader), and `node`/`node_sha256`/`node_version` the Node
runtime that argv launches it with explicitly. The standalone release (a Bun-compiled executable next to its
package.json, as some VM images ship it at ~/.local/pi/pi) runs directly; its `node` fields are null.
"""
import json
import os
import shutil
import subprocess
from pathlib import Path

from ..util import sha256_file, tree_hash
from .base import BASE_PATH, Harness

PACKAGE_NAMES = ("@earendil-works/pi-coding-agent", "@mariozechner/pi-coding-agent")


def package_root(entry: Path) -> Path | None:
    """The installed npm package that holds pi's entry script (dist/bundle/cli.js or dist/cli.js)."""
    for parent in entry.parents:
        manifest = parent / "package.json"
        if manifest.is_file():
            try:
                if json.loads(manifest.read_text()).get("name") in PACKAGE_NAMES:
                    return parent
            except (OSError, ValueError):
                return None
    return None


def find_node(entry: Path) -> Path | None:
    """A Node runtime for pi: one installed beside it (…/bin/node or …/node/bin/node above the package), else
    the operator's `node` on PATH, else ~/node/bin/node. Resolved to the real file so its hash is pinned."""
    candidates = []
    for parent in entry.parents:
        candidates += [parent / "bin" / "node", parent / "node" / "bin" / "node"]
    which = shutil.which("node")
    if which:
        candidates.append(Path(which))
    candidates.append(Path.home() / "node" / "bin" / "node")
    for candidate in candidates:
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate.resolve()
    return None


def is_script(binary: Path) -> bool:
    """True for the npm entry script (run under node), False for the standalone compiled executable."""
    try:
        with open(binary, "rb") as stream:
            return stream.read(2) == b"#!"
    except OSError:
        return False


class Pi(Harness):
    name = "pi"
    binary_names = ("pi",)
    required_options: dict = {}

    def locate(self, override=None):
        found = super().locate(override)
        if found is None:
            return None
        # An npm bin shim is a symlink to the package's entry script; locate resolves it. A shell wrapper is not
        # accepted: the plan must pin pi itself.
        return found if package_root(found) else None

    def node(self, binary: Path) -> Path | None:
        if self.frozen:
            return Path(self.frozen["node"]) if self.frozen.get("node") else None
        return find_node(binary) if is_script(binary) else None

    def launcher(self, binary: Path) -> list[str]:
        if not is_script(binary):
            return [str(binary)]
        node = self.node(binary)
        if node is None:
            raise RuntimeError("harness pi: no node runtime found for the pi entry script")
        return [str(node), str(binary)]

    def version(self, binary):
        try:
            out = subprocess.run([*self.launcher(binary), "--version"], capture_output=True, text=True, timeout=30,
                                 stdin=subprocess.DEVNULL,
                                 env={"PATH": BASE_PATH, "HOME": "/nonexistent", "PI_OFFLINE": "1",
                                      "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0",
                                      "PI_CODING_AGENT_DIR": "/nonexistent/.pi/agent"})
            return "pi " + (out.stdout or out.stderr).strip().splitlines()[0][:200]
        except (OSError, RuntimeError, subprocess.TimeoutExpired, IndexError):
            return "unknown"

    def fingerprint(self, binary):
        info = super().fingerprint(binary)
        root = package_root(binary)
        info["agent_sha256"] = tree_hash(root) if root else None
        node = self.node(binary)
        info["node"] = str(node) if node else None
        info["node_sha256"] = sha256_file(node) if node else None
        info["node_version"] = None
        if node:
            try:
                info["node_version"] = subprocess.run([str(node), "--version"], capture_output=True, text=True, timeout=30,
                                                      stdin=subprocess.DEVNULL, env={"PATH": BASE_PATH}).stdout.strip() or None
            except (OSError, subprocess.TimeoutExpired):
                pass
        return info

    def prepare(self, home, base_url, model, effort, max_output_tokens):
        agent = home / ".pi" / "agent"
        agent.mkdir(parents=True, exist_ok=True)
        models = {"providers": {"swarm": {
            "name": "swarm",
            "baseUrl": base_url,  # Pi appends /responses (or /chat/completions)
            "api": "openai-completions" if self.wire == "chat" else "openai-responses",
            "apiKey": "swarm-local-no-key",
            "models": [{"id": model, "name": model, "reasoning": True, "input": ["text"], "contextWindow": 128000,
                        "maxTokens": max_output_tokens,
                        "cost": {"input": 0, "output": 0, "cacheRead": 0, "cacheWrite": 0}}],
        }}}
        settings = {
            "defaultProvider": "swarm",
            "defaultModel": model,
            "defaultThinkingLevel": effort,
            "enableInstallTelemetry": False,
            "enableAnalytics": False,
            "cacheWarming": "off",
            "defaultProjectTrust": "never",
            "quietStartup": True,
        }
        (agent / "models.json").write_text(json.dumps(models))
        (agent / "settings.json").write_text(json.dumps(settings))
        return {"PI_CODING_AGENT_DIR": str(agent), "PI_OFFLINE": "1", "PI_SKIP_VERSION_CHECK": "1", "PI_TELEMETRY": "0"}

    def argv(self, binary, prompt, workdir, model, effort):
        return [*self.launcher(binary), "--print", "--provider", "swarm", "--model", model, "--thinking", effort,
                "--no-session", "--no-skills", "--no-extensions", "--no-approve", "--offline", "--", prompt]
