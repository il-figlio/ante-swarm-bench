"""OpenAI Codex CLI (`codex exec`), an unmodified third-party binary, pointed at the scripted model.

Codex reads CODEX_HOME/config.toml. Each attempt gets a fresh CODEX_HOME with one custom model provider
(`swarm`, wire_api = "responses") whose base_url is the attempt's scripted URL, so every model call stays on
this machine and no OpenAI service is contacted. The settings below are the fair-play set: they switch off
what contacts the network or issues extra model calls, mirror Ante's own off-switches (no session save, no
memory) and leave everything else at Codex's defaults.
"""
import json

from .base import Harness

# Codex features that reach the network or make extra model calls. Everything else keeps its default.
DISABLED_FEATURES = ("plugins", "remote_plugin", "apps", "daemon_auto_start", "in_app_updates", "memories")


def toml_str(value: str) -> str:
    return json.dumps(value)  # a JSON string is a valid TOML basic string


class Codex(Harness):
    name = "codex"
    binary_names = ("codex",)
    required_options: dict = {}

    def prepare(self, home, base_url, model, effort, max_output_tokens):
        config = home / ".codex"
        config.mkdir(parents=True, exist_ok=True)
        lines = [
            f"model = {toml_str(model)}",
            'model_provider = "swarm"',
            f"model_reasoning_effort = {toml_str(effort)}",
            "check_for_update_on_startup = false",
            'web_search = "disabled"',
            "",
            "[analytics]", "enabled = false", "",
            "[feedback]", "enabled = false", "",
            "[features]", *(f"{name} = false" for name in DISABLED_FEATURES), "",
            "[model_providers.swarm]",
            'name = "swarm"',
            f"base_url = {toml_str(base_url)}",
            'env_key = "SWARM_API_KEY"',
            'wire_api = "responses"',
            "requires_openai_auth = false",
            "supports_websockets = false",
            "",
        ]
        (config / "config.toml").write_text("\n".join(lines))
        return {"CODEX_HOME": str(config), "SWARM_API_KEY": "swarm-local-no-key"}

    def argv(self, binary, prompt, workdir, model, effort):
        return [str(binary), "exec", "--strict-config", "--skip-git-repo-check", "--ephemeral",
                "--color", "never", "--dangerously-bypass-approvals-and-sandbox", prompt]
