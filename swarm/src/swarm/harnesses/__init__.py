from .ante import Ante
from .base import Harness
from .claude_code import ClaudeCode
from .codex import Codex
from .hermes import Hermes
from .mini import Mini
from .nanocode import Nanocode
from .pi import Pi

REGISTRY: dict[str, type[Harness]] = {cls.name: cls for cls in (Ante, ClaudeCode, Codex, Hermes, Mini, Nanocode, Pi)}


def get(name: str, options: dict | None = None) -> Harness:
    if name not in REGISTRY:
        raise ValueError(f"unknown harness {name!r}; known: {', '.join(sorted(REGISTRY))}")
    return REGISTRY[name](options)
