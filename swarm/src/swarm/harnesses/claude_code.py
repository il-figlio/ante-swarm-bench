"""Anthropic Claude Code (`claude -p`), an unmodified third-party binary, pointed at the scripted model.

Claude Code speaks the Anthropic Messages API. ANTHROPIC_BASE_URL points it at the attempt's scripted URL,
so every model call stays on this machine and no Anthropic service is contacted. Each attempt gets a fresh
HOME holding only a minimal ~/.claude.json (onboarding marked done) that Swarm writes. The settings below
are the fair-play set: they switch off telemetry, updates and error reporting, mirror Ante's own
off-switches (no session save, no skills, no auto memory) and leave everything else at the defaults.
"""
import json

from .base import Harness


class ClaudeCode(Harness):
    name = "claude-code"
    binary_names = ("claude",)
    required_options: dict = {}

    def prepare(self, home, base_url, model, effort, max_output_tokens):
        (home / ".claude").mkdir(parents=True, exist_ok=True)
        # Written by Swarm into the attempt's own fresh HOME: skips first-run onboarding prompts.
        (home / ".claude.json").write_text(json.dumps({"hasCompletedOnboarding": True, "numStartups": 10, "theme": "dark"}))
        env = {
            # The client appends /v1/messages itself.
            "ANTHROPIC_BASE_URL": base_url.removesuffix("/v1"),
            "ANTHROPIC_API_KEY": "sk-ant-swarm-local-no-key",
            "CLAUDE_CONFIG_DIR": str(home / ".claude"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
            "DISABLE_TELEMETRY": "1",
            "DISABLE_AUTOUPDATER": "1",
            "DISABLE_ERROR_REPORTING": "1",
            "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
            "CLAUDE_CODE_MAX_OUTPUT_TOKENS": str(max_output_tokens),
        }
        if self.helper:
            # Sub-agents asked for the Haiku tier get the race's helper model, which the relay admits.
            env["ANTHROPIC_DEFAULT_HAIKU_MODEL"] = self.helper
            env["ANTHROPIC_SMALL_FAST_MODEL"] = self.helper
        return env

    def argv(self, binary, prompt, workdir, model, effort):
        return [str(binary), "-p", prompt, "--dangerously-skip-permissions", "--output-format", "json", "--model", model,
                "--effort", effort, "--no-session-persistence", "--disable-slash-commands"]
