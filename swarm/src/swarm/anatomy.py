"""First-request anatomy (sizes only) and the canary (hashes of normalised parts), see docs/evaluation.md.

Never raises: a body that is not what its wire says gives None values, not zeros."""
import re

from . import live
from .util import canonical, sha256_bytes

WIRES = ("responses", "messages", "chat")
SYSTEM_ROLES = ("system", "developer")
FIELDS = ("wire", "canonical_bytes", "system_bytes", "tools_bytes", "user_bytes", "user_text_bytes",
          "instruction_bytes", "injected_bytes", "other_bytes", "tool_count", "mcp_tool_count")
CANARY_FIELDS = ("sha256", "tools_sha256", "system_sha256", "injected_sha256", "params_sha256")

UUID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b")
# ISO date with an optional time, then month-name dates ("Nov 12, 2026", "12 November 2026"), then bare times.
ISO = re.compile(r"\b\d{4}[-/]\d{2}[-/]\d{2}(?:[T ]\d{2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|[+-]\d{2}:?\d{2})?)?")
MONTHS = r"(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?"
NAMED = re.compile(rf"\b(?:(?:Mon|Tue|Wed|Thu|Fri|Sat|Sun)[a-z]*,? )?(?:{MONTHS} \d{{1,2}}(?:st|nd|rd|th)?,? \d{{4}}"
                   rf"|\d{{1,2}} {MONTHS},? \d{{4}})")
CLOCK = re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?: ?[AP]M)?\b")


def _empty(wire=None) -> dict:
    out = dict.fromkeys(FIELDS)
    out["wire"] = wire
    return out


def _texts(content) -> list[str]:
    """The text parts of one message content, in order (a plain string is one part)."""
    if isinstance(content, str):
        return [content]
    parts = content if isinstance(content, list) else []
    return [p["text"] for p in parts if isinstance(p, dict) and isinstance(p.get("text"), str)]


def _role_items(items, roles) -> list:
    return [m for m in items if isinstance(m, dict) and m.get("role") in roles] if isinstance(items, list) else []


def _present(*values) -> list:
    return [v for v in values if v not in (None, [], "")]


def segments(kind, body) -> dict | None:
    """{system, tools, user: lists of JSON pieces, text: user text parts}; None for an unknown wire or body."""
    if not isinstance(body, dict) or kind not in WIRES:
        return None
    if kind == "messages":
        sys_msgs, user = [], _role_items(body.get("messages"), ("user",))
        system = _present(body.get("system"))
        tools = _present(body.get("tools"))
    elif kind == "chat":
        sys_msgs = _role_items(body.get("messages"), SYSTEM_ROLES)
        user = _role_items(body.get("messages"), ("user",))
        system, tools = [], _present(body.get("tools"))
    else:
        items = body.get("input")
        if isinstance(items, str):
            items = [{"role": "user", "content": items}]
        sys_msgs, user = _role_items(items, SYSTEM_ROLES), _role_items(items, ("user",))
        system = _present(body.get("instructions"))
        extra = [i for i in items if isinstance(i, dict) and i.get("type") == "additional_tools"] if isinstance(items, list) else []
        tools = _present(body.get("tools")) + extra
    return {"system": system + sys_msgs, "tools": tools, "user": user,
            "text": [t for m in user for t in _texts(m.get("content"))]}


def _bytes(pieces) -> int:
    # Piece by piece, never re-wrapped, so the segments can only add up to less than the body.
    return sum(len(canonical(p)) for p in pieces)


def _find(instruction, text):
    """The instruction as it appears in the text: verbatim, else without its trailing newline; None if absent."""
    if not isinstance(instruction, str):
        return None
    for needle in (instruction, instruction.strip()):
        if needle and needle in text:
            return needle
    return None


def _count_tools(body) -> tuple:
    from .scripted import offered_tools  # lazy: scripted imports this module

    names = offered_tools(body)
    return len(names), sum(1 for n in names if n.startswith("mcp_"))  # "mcp__x__y" and "mcp_x_y"


def split(kind, body, instruction) -> dict:
    out = _empty(kind if isinstance(kind, str) else None)
    try:
        seg = segments(kind, body)
        if seg is None:
            return out
        whole = len(canonical(body))
        out.update(canonical_bytes=whole, system_bytes=_bytes(seg["system"]), tools_bytes=_bytes(seg["tools"]),
                   user_bytes=_bytes(seg["user"]))
        out["other_bytes"] = whole - out["system_bytes"] - out["tools_bytes"] - out["user_bytes"]
        text = "".join(seg["text"])
        out["user_text_bytes"] = len(text.encode("utf-8", "surrogateescape"))
        found = _find(instruction, text)
        if found is not None:
            out["instruction_bytes"] = len(found.encode("utf-8", "surrogateescape"))
            out["injected_bytes"] = out["user_text_bytes"] - out["instruction_bytes"]
        out["tool_count"], out["mcp_tool_count"] = _count_tools(body)
    except Exception:
        return _empty(out["wire"])
    return out


def normalise(text: str) -> str:
    """Volatile text out: sandbox and temp paths, home, ids, dates and times."""
    s = live.SANDBOX.sub("<sandbox>/", text)
    s = live.TEMP.sub("<tmp>", s)
    s = live.HOME.sub("~", s)
    s = UUID.sub("<uuid>", s)
    s = live.TOKEN.sub("<token>", s)
    s = ISO.sub("<date>", s)
    s = NAMED.sub("<date>", s)
    return CLOCK.sub("<time>", s)


def _hash(pieces) -> str:
    return sha256_bytes(normalise("\n".join(canonical(p).decode("utf-8", "replace") for p in pieces)).encode())


def canary(kind, body, instruction, user_agent) -> dict:
    out = dict.fromkeys(CANARY_FIELDS)
    try:
        seg = segments(kind, body)
        if seg is None:
            return out
        text = "".join(seg["text"])
        found = _find(instruction, text)
        injected = text.replace(found, "<instruction>") if found else text
        out.update(tools_sha256=_hash(seg["tools"]), system_sha256=_hash(seg["system"]),
                   injected_sha256=sha256_bytes(normalise(injected).encode("utf-8", "replace")),
                   params_sha256=sha256_bytes(canonical({"params": sorted(map(str, body)), "user_agent": user_agent})))
        # The canary is the three parts (POLICY). params_sha256 is recorded beside it, not in it: key names and the
        # User-Agent move with model options and versions, which the cohort already names.
        out["sha256"] = sha256_bytes(canonical([out["tools_sha256"], out["system_sha256"], out["injected_sha256"]]))
    except Exception:
        return dict.fromkeys(CANARY_FIELDS)
    return out


def first(kind, body, instruction, user_agent) -> dict:
    """The `anatomy` row field: split plus canary."""
    return {**split(kind, body, instruction), "canary": canary(kind, body, instruction, user_agent)}
