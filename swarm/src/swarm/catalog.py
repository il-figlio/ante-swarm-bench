"""Real models a race can launch: which provider serves each, the wire it speaks, and the efforts it accepts.

The front shows this as provider → model × effort. A lane offers a model only when its harness speaks the model's
wire (Codex: Responses only; Claude Code: Messages only), so the same table decides who may run what.
"""

EFFORTS = ["low", "medium", "high"]

PROVIDERS = [
    {"id": "chatgpt", "label": "ChatGPT", "note": "ChatGPT subscription through the relay upstream"},
    {"id": "claude", "label": "Claude", "note": "Claude subscription; Claude Code only (swarm gateway)"},
    {"id": "opencode", "label": "OpenCode", "note": "OpenCode free models (swarm gateway)"},
]

MODELS = [
    # The menu the front offers; edit it for your own upstreams. GPT 6.1 low or medium, Claude medium.
    {"id": "gpt-6.1-sol", "label": "GPT-6.1 Sol", "provider": "chatgpt", "wire": "responses", "efforts": ["low", "medium"]},
    {"id": "gpt-6.1-terra", "label": "GPT-6.1 Terra", "provider": "chatgpt", "wire": "responses", "efforts": ["low", "medium"]},
    # Claude Code hands sub-agent work to its Haiku tier: the relay admits that model too, for these races only.
    {"id": "claude-sonnet-5-5", "label": "Claude Sonnet 5.5", "provider": "claude", "wire": "messages", "efforts": ["medium"],
     "helper": "claude-haiku-5-5"},
    {"id": "claude-opus-5-5", "label": "Claude Opus 5.5", "provider": "claude", "wire": "messages", "efforts": ["medium"],
     "helper": "claude-haiku-5-5"},
    {"id": "glm-5.3-flash-free", "label": "GLM-5.3 Flash (free)", "provider": "opencode", "wire": "chat"},
    {"id": "deepseek-v4.1-flash-free", "label": "DeepSeek V4.1 Flash (free)", "provider": "opencode", "wire": "chat"},
    {"id": "beam-free", "label": "Beam (free)", "provider": "opencode", "wire": "chat"},
    {"id": "longcat-2.5-free", "label": "LongCat 2.5 (free)", "provider": "opencode", "wire": "chat"},
]
for _m in MODELS:
    _m.setdefault("efforts", list(EFFORTS))
    # Chat models default to high: Ante 0.3.0 sends reasoning_effort "low" when asked for medium on its
    # OpenAI-compatible wire (found 2026-11-11), which the strict check refuses. Same default for every harness.
    _m.setdefault("default_effort", "high" if _m["wire"] == "chat" else "medium")

BY_ID = {m["id"]: m for m in MODELS}

# The wires each harness can be pointed at (its first is the default).
HARNESS_WIRES = {
    "ante": ("responses", "chat"),
    "codex": ("responses",),
    "claude-code": ("messages",),
    "nanocode": ("responses",),
    "hermes": ("responses", "chat"),
    "pi": ("responses", "chat"),
    "mini": ("responses",),
}


def public() -> dict:
    return {"providers": PROVIDERS, "models": MODELS}


def helper_of(model: str) -> str | None:
    """The sub-agent model a race on `model` may also call, if any."""
    return (BY_ID.get(model) or {}).get("helper")


def wire_of(model: str) -> str | None:
    return (BY_ID.get(model) or {}).get("wire")
