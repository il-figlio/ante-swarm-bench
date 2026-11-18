import sys
from pathlib import Path

from .base import Harness

AGENT = Path(__file__).with_name("mini_agent.py")


class Mini(Harness):
    """The reference floor: a bare Responses loop with two tools."""

    name = "mini"

    def locate(self, override=None):
        return Path(override or sys.executable).resolve()

    def fingerprint(self, binary):
        info = super().fingerprint(binary)
        from ..util import sha256_file
        info["agent_sha256"] = sha256_file(AGENT)
        return info

    def prepare(self, home, base_url, model, effort, max_output_tokens):
        return {"SWARM_BASE_URL": base_url, "SWARM_MAX_OUTPUT_TOKENS": str(max_output_tokens), "PYTHONDONTWRITEBYTECODE": "1"}

    def argv(self, binary, prompt, workdir, model, effort):
        return [str(binary), "-I", str(AGENT), "--model", model, "--effort", effort, "--prompt", prompt]
