import json
import re

from .base import Harness


class Ante(Harness):
    name = "ante"
    binary_names = ("ante",)
    # Ante exports telemetry by default; its flush adds ~2 s to every exit (0.2.9).
    # A run must say which it measures.
    required_options = {"telemetry": ("on", "off")}

    def validate_options(self):
        if self.options.get("telemetry") not in ("on", "off"):
            raise ValueError("harness ante: options.telemetry must be stated explicitly, one of ['on', 'off']")
        label = self.options.get("telemetry_env")
        if self.options["telemetry"] == "on" and not (isinstance(label, str) and re.fullmatch(r"swarm-bench-[a-z0-9-]{1,64}", label)):
            raise ValueError("harness ante: telemetry = 'on' needs telemetry_env, a synthetic label like 'swarm-bench-density'")
        unknown = set(self.options) - {"telemetry", "telemetry_env"}
        if unknown:
            raise ValueError(f"harness ante: unknown options {sorted(unknown)}")

    def prepare(self, home, base_url, model, effort, max_output_tokens):
        config = home / ".ante"
        config.mkdir(parents=True, exist_ok=True)
        # Chat-Completions-only models use Ante's OpenAI-compatible wire; everything else its Responses wire.
        provider = {"base_url": base_url, "wire_style": "OpenAiCompatible" if self.wire == "chat" else "OpenAiResponse",
                    "preferred_models": [{"id": model, "max_tokens": max_output_tokens, "context_limit": 128000}]}
        # No key for the local model. 0.2.x spells that "auth": null; 0.3.x
        # spells it "credential": null and rejects a provider carrying "auth".
        if self.frozen.get("version", "").startswith("ante 0.2"):
            provider["auth"] = None
        else:
            provider["credential"] = None
        catalog = {"providers": {"swarm": provider}}
        (config / "catalog.json").write_text(json.dumps(catalog))
        env = {"ANTE_HOME": str(config), "ANTE_TELEMETRY": self.options["telemetry"]}
        if self.options["telemetry"] == "on":
            # Synthetic environment label; no user identity or collector override is ever passed.
            env["ANTE_ENV"] = self.options["telemetry_env"]
        return env

    def argv(self, binary, prompt, workdir, model, effort):
        return [str(binary), "--provider", "swarm", "--model", model, "--effort", effort, "--no-skills",
                "--disable-auto-memory", "--no-session-save", "--output-format", "json", "-p", prompt]
