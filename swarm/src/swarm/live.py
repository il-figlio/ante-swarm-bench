"""What visitors see of each agent: a live console and per-agent transcripts, read from Ante's own event log.

Ante writes one JSON event per line to the attempt's agent.log while it runs (SessionStart, UserInput, ToolStart,
ToolEnd, AgentMessage, TurnEnd, ...). Everything shown publicly passes through clean(): sandbox paths become
relative, the per-attempt model URL (it carries an unguessable token) is dropped, and long strings are cut.
SessionStart is never shown (it holds the provider URL and the sandbox cwd).
"""
import json
import re
import threading
import time
from datetime import datetime
from pathlib import Path

from . import harnesses
from .util import read_jsonl

ATTEMPT = re.compile(r"^c\d{3}-[a-z0-9-]+-n\d+-s(\d{3,})$")
# A sandbox path: anything up to <attempt>/work/ (the agent's cwd) or <attempt>/home/ (its fresh HOME).
SANDBOX = re.compile(r"(?:/[^\s\"'/]+)+?/c\d{3}-[a-z0-9-]+-n\d+-s\d{3,}/(work|home)(/?)")
MODEL_URL = re.compile(r"https?://(?:127\.0\.0\.1|localhost)(?::\d+)?/s/[0-9a-f]+[^\s\"']*")
TEMP = re.compile(r"/(?:private/)?(?:var/folders|tmp)/[^\s\"']+")
HOME = re.compile(r"/(?:home|Users)/[^/\s\"']+")
# Keys and the per-attempt URL token, wherever they turn up (a CLI's own log, an env dump, a command).
KEY = re.compile(r"\bsk-(?:ant-[A-Za-z0-9_-]{6,}|[A-Za-z0-9_-]{16,})")
TOKEN = re.compile(r"\b[0-9a-f]{32}\b")


INFRA = {"disk_full": "The machine's disk was full.", "setup_failed": "The runner could not build the agent's sandbox.",
         "runner_error": "The runner failed while the attempt was in progress."}


def rss_mb(agent: dict) -> float | None:
    """Peak RSS of the agent's largest single process (kernel rusage), in MB. One process, so no shared page is
    counted twice; a harness of several processes holds more than this. Unknown stays None."""
    kb = agent.get("max_process_rss_kb")
    return round(kb / 1024, 1) if kb is not None else None


def lifecycle(directory: Path) -> dict:
    try:
        doc = json.loads((directory / "lifecycle.live.json").read_text())
        agent = doc.get("agent") or {}
        return {"phase": doc.get("phase"), "agent_limit_s": doc.get("agent_limit_s"),
                "agent": {k: agent.get(k) for k in ("exit_code", "termination", "wall_s", "cpu_s", "max_process_rss_kb")}}
    except (OSError, ValueError):
        return {}


def clean(text, limit=2000) -> str:
    s = str(text)
    s = SANDBOX.sub(lambda m: ("" if m.group(2) else ".") if m.group(1) == "work" else "~/", s)
    s = MODEL_URL.sub("<model>", s)
    s = TEMP.sub("<tmp>", s)
    s = HOME.sub("~", s)
    s = KEY.sub("<key>", s)
    s = TOKEN.sub("<token>", s)
    s = re.sub(r"\s*<folder-structure>.*?</folder-structure>\s*", "\n", s, flags=re.S).strip()
    return s if len(s) <= limit else s[:limit - 1] + "…"



# Descriptions explain recorded classes, not inferred provider or billing causes.
FAILURES = {
    "infra": ("Not run: runner error", "The runner could not set up or complete this attempt. This is not a failure of the harness."),
    "admission_timeout": ("Never admitted", "The agent never started before its admission wait limit."),
    "cancelled": ("Cancelled", "The attempt was cancelled before verification finished."),
    "timeout": ("Time limit reached", "The agent exceeded its recorded execution time limit."),
    "output_limit": ("Output limit reached", "The agent exceeded the allowed output size."),
    "oom_killed": ("Out of memory", "The kernel killed the agent while the machine was out of memory."),
    "descendant_leak": ("Process left running", "A descendant process survived the agent's exit."),
    "agent_error": ("Agent exited with an error", "The agent did not exit successfully. Inspect its recorded exit status."),
    "request_budget_exhausted": ("Request limit reached", "The agent exceeded its model-request budget."),
    "request_error": ("Request rejected", "One or more model requests were rejected. The recorded error codes are below."),
    "request_incomplete": ("Request incomplete", "Not every model request completed."),
    "script_unfinished": ("Script unfinished", "The scripted model's final step was not served."),
    "late_change": ("Files changed after exit", "Files changed after the agent exited."),
    "check_failed": ("Hidden test failed", "The hidden test did not pass. Its output is available below."),
    "off_target": ("Unexpected file changes", "The agent changed a path outside the allowed set."),
}


def failure_detail(record: dict) -> dict | None:
    code = record.get("failure")
    if not code:
        return None
    agent, request, check = record.get("agent") or {}, record.get("requests") or {}, record.get("check") or {}
    title, message = FAILURES.get(code, ("Attempt failed", "See the recorded evidence for this attempt."))
    extra = {}
    if code == "infra":
        reason = record.get("infra_reason") or "runner_error"
        message = f"{INFRA.get(reason, INFRA['runner_error'])} {message}"
        # harness_failure: what the agent's own outcome was when the machine failed under it (kept, not counted).
        extra = {"infra_reason": clean(reason, 40), "infra_stage": clean(record.get("infra_stage") or "", 40) or None,
                 "harness_failure": clean(record["harness_failure"], 80) if record.get("harness_failure") else None}
    if agent.get("oom_kills_during"):
        extra["oom_kills_during"] = agent["oom_kills_during"]  # the kernel killed something on this machine meanwhile
    return {"code": clean(code, 80), "title": title, "message": message, **extra,
            "exit_code": agent.get("exit_code"), "termination": clean(agent["termination"], 80) if agent.get("termination") else None,
            "requests": request.get("count"), "completed_requests": request.get("completed"),
            "request_errors": [clean(e, 120) for e in request.get("errors", [])[:8]],
            "admission_wait_s": (record.get("admission") or {}).get("wait_s"),
            "held_for": [clean(e, 80) for e in (record.get("admission") or {}).get("held_for", [])[:8]],
            "check_exit": check.get("check_exit"), "check_termination": check.get("check_termination")}


def harness_of(attempt: str) -> str | None:
    """The harness named in an attempt id: c012-claude-code-n32-s005 -> claude-code."""
    m = re.match(r"^c\d{3}-([a-z0-9-]+)-n\d+-s\d{3,}$", attempt)
    return m.group(1) if m else None


def _steps(path: Path):
    """Request rows of a harness whose own log has no per-tool events (Codex CLI, Claude Code): one scripted
    step per model request, from the scripted server's record. Tool and argument only, already path-free."""
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            row = json.loads(line)
        except ValueError:
            continue
        if isinstance(row, dict) and row.get("status") == 200 and row.get("op"):
            yield row


def slot_of(attempt: str) -> int | None:
    m = ATTEMPT.match(attempt)
    return int(m.group(1)) if m else None


def _events(path: Path):
    try:
        lines = path.read_text(errors="replace").splitlines()
    except OSError:
        return
    for line in lines:
        try:
            e = json.loads(line)
        except ValueError:
            continue
        ev = e.get("event")
        if isinstance(ev, dict) and len(ev) == 1:
            kind, body = next(iter(ev.items()))
            yield e.get("timestamp"), kind, body


def _arg(name: str, args: dict) -> str:
    """The one argument that says what a tool call did."""
    if not isinstance(args, dict):
        return ""
    for key in ("file_path", "path", "command", "pattern", "url", "prompt", "description"):
        if args.get(key):
            return clean(args[key], 120)
    first = next((v for v in args.values() if isinstance(v, str)), "")
    return clean(first, 120)


def _result(name: str, result) -> str:
    """A short, readable form of a tool result."""
    if not isinstance(result, dict):
        return clean(result if result is not None else "", 2000)
    if "Completed" in result and isinstance(result["Completed"], dict):  # Bash
        r = result["Completed"]
        out = (r.get("stdout") or "") + (("\n" + r["stderr"]) if r.get("stderr") else "")
        return clean(f"exit {r.get('exit_code')}\n{out}".rstrip(), 2000)
    text = result.get("file_type", {}).get("Text") if isinstance(result.get("file_type"), dict) else None
    if text:
        return clean("".join(text.get("lines", [])), 2000)
    if "message" in result:
        return clean(result["message"], 400)
    return clean(json.dumps(result), 600)


def _t(stamp) -> float | None:
    try:
        return datetime.fromisoformat(str(stamp).replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


class Tailer:
    """One per running run: turns new agent.log and admission lines into console events, shared by all viewers."""

    def __init__(self, bundle: Path, cap=6000):
        self.bundle, self.cap = bundle, cap
        self.events: list[dict] = []
        self.dropped = 0
        self.lock = threading.Lock()
        self.offsets: dict[Path, int] = {}
        self.last: dict[int, float] = {}  # latest event time per agent, so its result line lands after its tool calls
        self.start = time.time()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._loop, daemon=True)
        self.thread.start()

    def since(self, i: int, limit=400) -> tuple[list[dict], int]:
        with self.lock:
            i = max(i, self.dropped)
            chunk = self.events[i - self.dropped:i - self.dropped + limit]
            return chunk, i + len(chunk)

    def finish(self):
        self.stop.set()
        self.thread.join(5)
        self._scan()

    def _loop(self):
        while not self.stop.wait(0.4):
            self._scan()

    def _new_lines(self, path: Path) -> list[str]:
        try:
            with path.open("rb") as f:
                f.seek(self.offsets.get(path, 0))
                data = f.read()
        except OSError:
            return []
        end = data.rfind(b"\n")
        if end < 0:
            return []
        self.offsets[path] = self.offsets.get(path, 0) + end + 1
        return data[:end].decode(errors="replace").splitlines()

    def _push(self, batch: list[dict]):
        if not batch:
            return
        batch.sort(key=lambda e: e["t"])
        with self.lock:
            self.events.extend(batch)
            over = len(self.events) - self.cap
            if over > 0:
                del self.events[:over]
                self.dropped += over

    def _scan(self):
        batch = []
        rel = lambda stamp: round(max(0.0, (_t(stamp) or time.time()) - self.start), 2)
        for line in self._new_lines(self.bundle / "admission.jsonl"):
            try:
                a = json.loads(line)
            except ValueError:
                continue
            slot = slot_of(a.get("attempt", ""))
            if a.get("admitted") and slot is not None:
                t = round(max(0.0, a["t"] - self.start), 2) if isinstance(a.get("t"), (int, float)) else rel(None)
                batch.append({"t": t, "slot": slot, "k": "admit",
                              "mb": a.get("available_mb"), "p": a.get("pressure"), "w": a.get("wait_s")})
        logs = self.bundle / "logs"
        if logs.exists():
            for d in logs.iterdir():
                slot = slot_of(d.name)
                if slot is None:
                    continue
                if harness_of(d.name) != "ante":
                    for line in self._new_lines(d / "requests.live.jsonl"):
                        try:
                            row = json.loads(line)
                        except ValueError:
                            continue
                        if not isinstance(row, dict):
                            continue
                        if row.get("error") == "route_rejected":
                            continue  # a harness probing an unknown path, not a failed model request
                        if row.get("error"):
                            batch.append({"t": round(max(0.0, row.get("at", time.time()) - self.start), 2),
                                          "slot": slot, "k": "request_error", "status": row.get("status"),
                                          "text": clean(row["error"], 120)})
                            continue
                        if not row.get("op"):
                            continue
                        t = round(max(0.0, row["at"] - self.start), 2) if isinstance(row.get("at"), (int, float)) else rel(None)
                        if row["op"] == "final":
                            batch.append({"t": t, "slot": slot, "k": "msg", "text": clean(row.get("arg") or "", 160)})
                        else:
                            batch.append({"t": t, "slot": slot, "k": "tool", "tool": clean(row.get("tool") or "", 40),
                                          "arg": clean(row.get("arg") or "", 120)})
                    continue
                for line in self._new_lines(d / "agent.log"):
                    try:
                        e = json.loads(line)
                    except ValueError:
                        continue
                    ev = e.get("event")
                    if not (isinstance(ev, dict) and len(ev) == 1):
                        continue
                    kind, body = next(iter(ev.items()))
                    t = rel(e.get("timestamp"))
                    if kind == "ToolStart":
                        batch.append({"t": t, "slot": slot, "k": "tool", "tool": body.get("name"), "arg": _arg(body.get("name"), body.get("args"))})
                    elif kind == "ToolEnd" and body.get("status") != "Completed":
                        batch.append({"t": t, "slot": slot, "k": "toolerr", "tool": body.get("tool_name"), "arg": clean(body.get("status"), 80)})
                    elif kind == "AgentMessage":
                        batch.append({"t": t, "slot": slot, "k": "msg", "text": clean(body, 160)})
        for line in self._new_lines(self.bundle / "attempts.jsonl"):
            try:
                a = json.loads(line)
            except ValueError:
                continue
            slot = slot_of(a.get("attempt", ""))
            if slot is None:
                continue
            for e in batch:  # this scan's own events for the slot count too
                if e["slot"] == slot:
                    self.last[slot] = max(self.last.get(slot, 0), e["t"])
            agent = a.get("agent", {})
            batch.append({"t": round(max(rel(None), self.last.get(slot, 0) + 0.01), 2), "slot": slot, "k": "result", "ok": bool(a.get("passed")),
                          "wall": agent.get("wall_s"), "mb": rss_mb(agent),
                          "why": a.get("failure")})
        for e in batch:
            if e["k"] != "result":
                self.last[e["slot"]] = max(self.last.get(e["slot"], 0), e["t"])
        self._push(batch)


def transcript(bundle: Path, slot: int) -> dict | None:
    """Everything one agent did, cleaned for the public page."""
    record = next((a for a in read_jsonl(bundle / "attempts.jsonl") if slot_of(a.get("attempt", "")) == slot), None)
    logs = bundle / "logs"
    attempt = record["attempt"] if record else next((d.name for d in logs.iterdir() if slot_of(d.name) == slot), None) if logs.exists() else None
    if not attempt or not ATTEMPT.match(attempt):
        return None
    d = logs / attempt
    prompt, steps, messages, turn = "", [], [], None
    by_id = {}
    harness = harness_of(attempt) or "ante"
    events = _events(d / "agent.log") if harness == "ante" else ()
    for stamp, kind, body in events:
        if kind == "UserInput":
            prompt = clean(body, 3000)
        elif kind == "ToolStart":
            args = body.get("args") if isinstance(body.get("args"), dict) else {}
            step = {"tool": body.get("name"), "arg": _arg(body.get("name"), args),
                    "args": {k: clean(v, 2000) for k, v in args.items() if isinstance(v, (str, int, float, bool))}, "t": _t(stamp)}
            by_id[body.get("id")] = step
            steps.append(step)
        elif kind == "ToolEnd":
            step = by_id.get(body.get("tool_use_id"))
            if step is not None:
                step["status"] = body.get("status")
                step["result"] = _result(step["tool"], body.get("result_json"))
                end = _t(stamp)
                if end and step.get("t"):
                    step["ms"] = round((end - step["t"]) * 1000)
        elif kind == "AgentMessage":
            messages.append(clean(body, 2000))
        elif kind == "TurnEnd":
            turn = {"status": body.get("status"), "steps": body.get("steps")}
    for step in steps:
        step.pop("t", None)
    plan = json.loads((bundle / "plan.json").read_text()) if (bundle / "plan.json").exists() else {}
    task = next((t for t in plan.get("tasks", []) if t.get("id") == (record or {}).get("task")), None)
    if harness != "ante":
        # Codex CLI and Claude Code have no per-tool events in their log: show what the scripted server was asked
        # for and answered, one step per model request (tool and argument; results stay in the hidden check).
        for row in _steps(d / "requests.jsonl") if (d / "requests.jsonl").exists() else _steps(d / "requests.live.jsonl"):
            if row["op"] == "final":
                messages.append(clean(row.get("arg") or "", 2000))
            else:
                steps.append({"tool": clean(row.get("tool") or "", 40), "arg": clean(row.get("arg") or "", 120),
                              "args": {}, "status": "Served", "source": "scripted_model"})
        try:
            prompt = clean((Path(task["path"]) / "instruction.md").read_text(errors="replace"), 3000) if task else ""
        except (OSError, KeyError):
            prompt = ""
        turn = {"status": ("Completed" if record.get("passed") else "Failed") if record else None, "steps": len(steps)}
    try:
        check_log = clean((d / "check.log").read_text(errors="replace"), 2000)
    except OSError:
        check_log = ""
    try:
        test_script = clean((Path(task["path"]) / "tests" / "test.sh").read_text(errors="replace"), 3000) if task else ""
    except (OSError, KeyError):
        test_script = ""
    model = plan.get("spec", {}).get("model", {})
    command = harnesses.get(harness).argv(Path(harness if harness != "claude-code" else "claude"), "<prompt below>", Path("work"),
                                          model.get("id", "swarm-scripted-1"), model.get("effort", "medium"))
    observed = lifecycle(d)
    agent = (record or {}).get("agent") or observed.get("agent") or {}
    request_path = d / "requests.jsonl" if (d / "requests.jsonl").exists() else d / "requests.live.jsonl"
    request_errors = [{"status": row.get("status"), "error": clean(row["error"], 120)}
                      for row in read_jsonl(request_path) if row.get("error")][-8:]
    return {
        "slot": slot, "attempt": attempt, "harness": harness, "task": (record or {}).get("task"), "state": "done" if record else "running",
        "command": command, "prompt": prompt, "steps": steps, "messages": messages, "turn": turn,
        "passed": (record or {}).get("passed"), "failure": (record or {}).get("failure"),
        "check": {"log": check_log, "script": test_script, "changed": ((record or {}).get("check") or {}).get("changed", []),
                  "off_target": ((record or {}).get("check") or {}).get("off_target", [])},
        "agent": {k: agent.get(k) for k in ("wall_s", "cpu_s", "exit_code", "termination")} | {"rss_mb": rss_mb(agent)},
        "requests": (record or {}).get("requests", {}).get("count"),
        "failure_detail": failure_detail(record or {}),
        "request_errors": request_errors,
        "lifecycle": observed,
    }
