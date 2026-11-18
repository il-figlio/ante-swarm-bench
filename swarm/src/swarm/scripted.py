"""Deterministic OpenAI Responses and Anthropic Messages compatible model for zero-cost runs.

Each attempt registers a script of abstract steps (write a file, run a command,
finish), or a policy: an object with `.next(last_output)` that picks each step from the text of the most recent
tool result (the maze explorer). A policy's steps map onto native tools exactly as a script's do. On every request the server picks the next step from the conversation
history (the number of tool results already returned), finds the harness's own
native tool for that step in the request's tool list and returns it as a
function call. Every harness therefore does the same work through its own
tools, and the model costs nothing and never varies.

Two wire formats are served at the same per-attempt URL: POST .../v1/responses
(Ante, Hermes, Codex CLI) and POST .../v1/messages (Claude Code), both streaming
(server-sent events) or not. Probes a client may send around its model calls
(HEAD/GET, /v1/messages/count_tokens) are answered harmlessly and recorded as
non-model rows; GET .../v1/models lists the attempt's one model (nanocode
refuses to start on an empty list).

Requests are checked like a strict relay: model, effort and output cap must
match the plan or the request is rejected (422) and recorded.
"""
import json
import secrets
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import anatomy

# Native tool name -> how to build its arguments. Order is preference.
WRITE_TOOLS = {
    "Write": lambda abs_path, rel, content: {"file_path": abs_path, "content": content},
    "write_file": lambda abs_path, rel, content: {"path": abs_path, "content": content},
    "write": lambda abs_path, rel, content: {"path": abs_path, "content": content},
}
READ_TOOLS = {
    "Read": lambda abs_path: {"file_path": abs_path},
    "read_file": lambda abs_path: {"path": abs_path},
    "read": lambda abs_path: {"path": abs_path},
}
BASH_TOOLS = {
    "Bash": lambda command: {"command": command, "description": "Run a scripted command"},
    "bash": lambda command: {"command": command},
    "terminal": lambda command: {"command": command},
    "shell": lambda command: {"command": command},
    # Codex CLI: one command string; yield_time_ms keeps a long command (sleep 8) in the same call.
    "exec_command": lambda command: {"cmd": command, "yield_time_ms": 30000},
}
EDIT_TOOLS = {
    "Edit": lambda abs_path, old, new: {"file_path": abs_path, "old_string": old, "new_string": new},
}


def code_mode_program(command: str) -> str:
    """nanocode's Code Mode: the only top-level tool is `exec`, a freeform tool whose input is a JavaScript
    program; the shell is reached as the nested tool `tools.exec_command`, the same tool and arguments Codex CLI
    gets directly. The pragma keeps the cell open as long as the nested call, so a long command (sleep 8)
    still finishes in one model turn. JSON is a valid JavaScript literal, so the command is byte-exact."""
    args = json.dumps({"cmd": command, "yield_time_ms": 30000})
    return ('// @exec: {"yield_time_ms": 30000}\n'
            f"const result = await tools.exec_command({args});\n"
            "text(result);\n")


# Code-execution tools: the program runs one shell command (read = cat, write = printf, as for Codex CLI).
# Last resort, used only when the client offers no native or shell tool; the call is a freeform
# (custom) tool call whose input is the program text, not JSON arguments.
CODE_TOOLS = {
    "exec": code_mode_program,
}


def sh_quote(text: str) -> str:
    """POSIX single-quote a string; every byte is preserved."""
    return "'" + str(text).replace("'", "'\\''") + "'"


# Harnesses whose only file access is their shell (Codex CLI 0.165 offers exec_command and no
# read or write tool for an unknown model): read becomes cat, write becomes an exact printf.
# Used only when no native read/write tool is offered.
SHELL_READ = lambda abs_path: f"cat -- {sh_quote(abs_path)}"
SHELL_WRITE = lambda abs_path, content: f"printf %s {sh_quote(content)} > {sh_quote(abs_path)}"
MAX_BODY = 4 * 1024 * 1024


def synthetic_tokens(raw: bytes) -> int:
    """A stable stand-in for prompt size: one token per four request bytes."""
    return max(1, len(raw) // 4)


class Attempt:
    def __init__(self, key, script, workdir, model, effort, max_output_tokens, max_requests, trace=None, helper=None,
                 instruction=None):
        self.key = key
        self.instruction = instruction  # the task's prompt, to find it in the first request (anatomy)
        self.trace = trace  # optional path: each completed request row is appended as it happens (live console)
        self.max_requests = max_requests
        self.script = script
        self.workdir = str(workdir)
        self.model = model
        self.helper = helper  # a sub-agent model also admitted (relay only), or None
        self.effort = effort
        self.max_output_tokens = max_output_tokens
        self.rows = []
        self.final_served = False
        self.token = secrets.token_hex(16)
        # A policy (not a list) picks steps as results arrive. Steps already chosen are kept by position, so a
        # client that repeats a request gets the same step and the policy is asked once per step.
        self.policy_steps: list[dict] = []
        self.policy_lock = threading.Lock()

    @property
    def is_policy(self) -> bool:
        return not isinstance(self.script, (list, tuple)) and hasattr(self.script, "next")


class ScriptedModel:
    SERVES_CHAT = False

    def __init__(self, host="127.0.0.1", port=0, strict=True):
        self.strict = strict
        self.attempts: dict[str, Attempt] = {}  # token -> attempt
        self.by_key: dict[str, str] = {}  # attempt key -> token
        self.lock = threading.Lock()
        outer = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"
            timeout = 30  # a client that stalls mid-request cannot hold a thread

            def log_message(self, *args):
                pass

            def do_GET(self):
                outer._probe(self)

            do_HEAD = do_GET

            def do_POST(self):
                outer._handle(self)

            def _send(self, status, body, content_type="application/json"):
                payload = body if isinstance(body, bytes) else json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", content_type)
                self.send_header("Content-Length", str(len(payload)))
                if status >= 400:
                    self.send_header("Connection", "close")
                    self.close_connection = True
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(payload)

        class Server(ThreadingHTTPServer):
            daemon_threads = True
            request_queue_size = 4096  # hundreds of agents connect at once

            def handle_error(self, request, client_address):
                pass  # client resets are the harness's business; requests are already recorded

        self.server = Server((host, port), Handler)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def port(self) -> int:
        return self.server.server_port

    def base_url(self, token: str) -> str:
        return f"http://127.0.0.1:{self.port}/s/{token}/v1"

    def register(self, key, script, workdir, model, effort, max_output_tokens, max_requests=40, trace=None, helper=None,
                 instruction: str | None = None) -> str:
        """Register an attempt. Its URL carries an unguessable token, so one
        attempt cannot reach another attempt's script."""
        attempt = Attempt(key, script, workdir, model, effort, max_output_tokens, max_requests, trace, helper, instruction)
        with self.lock:
            if key in self.by_key:
                raise ValueError(f"attempt key {key} already registered")
            self.attempts[attempt.token] = attempt
            self.by_key[key] = attempt.token
        return self.base_url(attempt.token)

    def release(self, key, settle_s: float = 2.0) -> tuple[list[dict], bool]:
        """Unregister an attempt; returns its request rows and whether the final
        step was served. Waits briefly for requests still being answered."""
        deadline = time.monotonic() + settle_s
        while True:
            with self.lock:
                token = self.by_key.get(key)
                attempt = self.attempts.get(token)
                in_flight = attempt is not None and any(r.get("status") is None for r in attempt.rows)
                if attempt is None or not in_flight or time.monotonic() > deadline:
                    if attempt is not None:
                        del self.attempts[token]
                        del self.by_key[key]
                        return [dict(r) for r in attempt.rows], attempt.final_served
                    return [], False
            time.sleep(0.05)

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    # --- request handling -------------------------------------------------

    def _attempt_for(self, handler):
        """(attempt, route parts after the token) for a /s/<token>/... path; attempt is None if unknown."""
        parts = handler.path.split("?", 1)[0].split("/")
        token = parts[2] if len(parts) > 2 and parts[1] == "s" else None
        with self.lock:
            return self.attempts.get(token), parts[3:]

    def _probe(self, handler):
        """HEAD/GET around a client's model calls (reachability checks). Answered 200 with no model content and
        recorded as a non-model row, so they never count as a request error or take a script step."""
        attempt, route = self._attempt_for(handler)
        if attempt is None:
            handler._send(404, {"error": {"message": "unknown attempt"}})
            return
        with self.lock:
            attempt.rows.append({"index": len(attempt.rows), "request_id": f"{attempt.key}#{len(attempt.rows)}",
                                 "kind": "probe", "method": handler.command,
                                 "path": "/s/<token>/" + "/".join(route), "error": None, "status": 200, "completed": True})
        if route and route[-1] == "models":
            # An OpenAI model list (nanocode reads <base>/models at startup and refuses an empty one).
            handler._send(200, {"object": "list", "data": [{"id": attempt.model, "object": "model", "created": 0,
                                                            "owned_by": "swarm"}]})
            return
        handler._send(200, {"ok": True})

    def _trace(self, attempt, row):
        if attempt.trace and row.get("status") is not None:
            try:
                with self.lock, open(attempt.trace, "a") as f:
                    f.write(json.dumps(dict(row, at=row.get("at", round(time.time(), 3))), sort_keys=True) + "\n")
            except OSError:
                pass

    def _handle(self, handler):
        admitted = self._admit(handler)
        if admitted is not None:
            self._serve(handler, *admitted)

    def _admit(self, handler):
        """Route, budget, body and settings checks shared by every model source. Answers and returns None when the
        request is refused; otherwise returns (attempt, row, kind, body, raw, started) for the source to serve."""
        started = time.monotonic()
        attempt, route = self._attempt_for(handler)
        # /s/<token>/v1/responses | /s/<token>/v1/messages | /s/<token>/v1/messages/count_tokens
        kind = {"v1/responses": "responses", "v1/messages": "messages", "v1/messages/count_tokens": "count_tokens",
                "v1/chat/completions": "chat"}.get("/".join(route))
        if kind == "chat" and not self.SERVES_CHAT:
            kind = None  # the scripted model answers Responses and Messages only
        if attempt is not None:
            with self.lock:
                # recv_mono: when the request arrived (this host's monotonic clock), for startup and step timing.
                row = {"index": len(attempt.rows), "request_id": f"{attempt.key}#{len(attempt.rows)}",
                       "user_agent": (handler.headers.get("User-Agent") or "")[:120] or None,
                       "prev_request_id": None, "prev_source": None, "upstream_id": None, "path": "/s/<token>/" + "/".join(route), "error": None, "status": None,
                       "recv_mono": round(started, 6)}
                if kind:
                    row["kind"] = kind
                # Probes are recorded in _probe and never reach here. count_tokens is a client helper, not a
                # model turn, so it does not spend the attempt's request budget.
                row["billable"] = kind != "count_tokens"
                attempt.rows.append(row)
                over_budget = sum(1 for r in attempt.rows if r.get("billable")) > attempt.max_requests
        if attempt is None:
            # Unknown tokens are never recorded against any attempt.
            handler._send(404, {"error": {"message": "unknown attempt"}})
            return
        if over_budget:
            # Every request takes a slot, valid or not; over budget is never served.
            row.update(status=429, error="request_budget_exhausted")
            self._trace(attempt, row)
            handler._send(429, {"error": {"message": "request budget exhausted", "type": "rate_limit_error"}})
            return
        if kind is None:
            row.update(status=404, error="route_rejected")
            self._trace(attempt, row)
            handler._send(404, {"error": {"message": "only /responses and /messages are served"}})
            return
        try:
            length = int(handler.headers.get("Content-Length", 0))
        except ValueError:
            length = -1
        if not 0 < length <= MAX_BODY:
            row.update(status=413, error="length_rejected")
            self._trace(attempt, row)
            handler._send(413, {"error": {"message": "bad length"}})
            return
        raw = handler.rfile.read(length)
        row["request_bytes"] = len(raw)
        try:
            body = json.loads(raw)
            assert isinstance(body, dict)
        except (ValueError, AssertionError):
            row.update(status=400, error="invalid_json")
            self._trace(attempt, row)
            handler._send(400, {"error": {"message": "invalid json"}})
            return
        if kind == "count_tokens":
            return attempt, row, kind, body, raw, started

        anthropic = kind == "messages"
        row["api"] = kind
        if anthropic:
            effort = (body.get("output_config") or {}).get("effort") if isinstance(body.get("output_config"), dict) else None
            cap = body.get("max_tokens")
        elif kind == "chat":
            effort = body.get("reasoning_effort")
            cap = body.get("max_completion_tokens", body.get("max_tokens"))
        else:
            reasoning = body.get("reasoning") if isinstance(body.get("reasoning"), dict) else {}
            effort = reasoning.get("effort")
            cap = body.get("max_output_tokens")
        row.update(
            model=body.get("model"),
            effort=effort,
            max_output_tokens=cap,
            stream=bool(body.get("stream")),
            tools_offered=offered_tools(body),
        )
        mismatches = []
        self._link(attempt, row, kind, body)
        helper = attempt.helper is not None and row["model"] == attempt.helper
        if helper:
            row["helper"] = True  # sub-agent call: counted with the run, listed apart
        elif row["model"] != attempt.model:
            mismatches.append("model")
        # Effort must be sent and match. An output cap is checked when sent (Codex has no setting for it and
        # sends none); what was sent is in the row either way. A sub-agent call may leave effort out (the
        # harness decides per model) but may not raise it.
        if effort != attempt.effort and not (helper and effort is None):
            mismatches.append("effort")
        # A cap above the plan's would buy the harness more output: rejected. A lower one is how a harness fits its
        # context window as a session grows (Ante 0.3 trims it near its context limit): served, flagged, counted.
        if attempt.max_output_tokens is not None and isinstance(cap, int) and not isinstance(cap, bool) and cap < attempt.max_output_tokens:
            row["cap_lowered"] = True
        elif attempt.max_output_tokens is not None and cap not in (None, attempt.max_output_tokens):
            mismatches.append("max_output_tokens")
        row["settings_mismatch"] = mismatches
        if mismatches and self.strict:
            row.update(status=422, error="settings_mismatch")
            self._trace(attempt, row)
            handler._send(422, {"error": {"message": "settings mismatch: " + ",".join(mismatches), "type": "invalid_request_error"}})
            return None
        return attempt, row, kind, body, raw, started

    def _link(self, attempt, row, kind, body):
        """The row's place in the attempt's chain, and the anatomy of the attempt's first model request. The
        previous row is the one whose response id the body names, else the latest completed one (same helper flag)."""
        helper = attempt.helper is not None and row["model"] == attempt.helper
        previous = body.get("previous_response_id")
        with self.lock:
            earlier = [r for r in attempt.rows if r is not row and r.get("api")]
            named = next((r for r in earlier if isinstance(previous, str) and r.get("upstream_id") == previous), None)
            done = [r for r in earlier if r.get("completed") and bool(r.get("helper")) == helper]
            if named is not None:
                row.update(prev_request_id=named["request_id"], prev_source="previous_response_id")
            elif done:
                row.update(prev_request_id=done[-1]["request_id"], prev_source="sequence")
            first = not earlier
        if first:
            try:  # anatomy never raises; this keeps the model server up if that ever changes
                row["anatomy"] = anatomy.first(kind, body, attempt.instruction, row["user_agent"])
            except Exception:
                pass

    def _serve(self, handler, attempt, row, kind, body, raw, started):
        """Answer an admitted request from the attempt's script."""
        if kind == "count_tokens":
            with self.lock:
                row.update(status=200, completed=True)
            handler._send(200, {"input_tokens": synthetic_tokens(raw)})
            return
        anthropic = kind == "messages"
        step_index = count_tool_results(body, anthropic)
        row["step"] = step_index
        if attempt.is_policy:
            try:
                step = self._policy_step(attempt, step_index, body, anthropic)
            except Exception as error:  # noqa: BLE001 - a failing policy is a recorded request error, never a hung client
                row.update(status=500, error="policy_error", policy_error=f"{type(error).__name__}: {error}"[:200])
                self._trace(attempt, row)
                handler._send(500, {"error": {"message": "policy failed", "type": "api_error"}})
                return
        elif step_index >= len(attempt.script):
            step = None
        else:
            step = attempt.script[step_index]
        if step is None:
            row.update(status=400, error="extra_request")
            self._trace(attempt, row)
            handler._send(400, {"error": {"message": "script already finished", "type": "invalid_request_error"}})
            return
        try:
            name, arguments, text = self.step_call(attempt, step, row["tools_offered"])
        except LookupError as missing:
            row.update(status=400, error="tool_unavailable", missing=str(missing))
            self._trace(attempt, row)
            handler._send(400, {"error": {"message": f"no native tool for {missing}", "type": "invalid_request_error"}})
            return
        row["tool"] = name
        row["upstream_id"] = f"msg_{attempt.key}_{row['index']}"[:64] if anthropic else f"resp_{attempt.key}_{row['index']}"
        row["op"] = step["op"]
        row["arg"] = step_arg(step)
        row["at"] = round(time.time(), 3)
        tokens = synthetic_tokens(raw)
        stream = row["stream"]
        if anthropic:
            message = anthropic_message(attempt, row["index"], step_index, name, arguments, text, tokens)
            payload = anthropic_sse(message) if stream else json.dumps(message).encode()
        else:
            output_item = response_item(attempt, step_index, name, arguments, text)
            usage = {"input_tokens": tokens, "output_tokens": 16, "total_tokens": tokens + 16,
                     "input_tokens_details": {"cached_tokens": 0}, "output_tokens_details": {"reasoning_tokens": 0}}
            response = {
                "id": f"resp_{attempt.key}_{row['index']}", "object": "response", "created_at": int(time.time()),
                "status": "completed", "model": attempt.model, "output": [output_item], "usage": usage,
                "parallel_tool_calls": False, "tool_choice": "auto", "tools": [],
            }
            payload = self._sse(response, output_item, text) if stream else json.dumps(response).encode()
        # Record before sending: once the agent has the reply it may exit at once.
        with self.lock:
            sent = time.monotonic()
            row.update(status=200, completed=True, elapsed_s=round(sent - started, 6), sent_mono=round(sent, 6))
            if step["op"] == "final":
                attempt.final_served = True
        self._trace(attempt, row)
        handler._send(200, payload, "text/event-stream" if stream else "application/json")

    @staticmethod
    def _policy_step(attempt, step_index, body, anthropic):
        """The policy's step for this position, or None when the run is already past its final step (or the client
        holds more results than steps were issued). The first request passes None; later ones the latest result."""
        with attempt.policy_lock:
            steps = attempt.policy_steps
            if step_index < len(steps):
                return steps[step_index]
            if step_index > len(steps) or (steps and steps[-1].get("op") == "final"):
                return None
            step = attempt.script.next(policy_input(latest_tool_result(body, anthropic)) if step_index else None)
            if not isinstance(step, dict) or step.get("op") not in ("bash", "final", "read", "write"):
                raise ValueError(f"policy returned an unusable step: {str(step)[:80]}")
            steps.append(step)
            return step

    def step_call(self, attempt, step, offered):
        """Map one script step onto the client's own tools: (tool name, arguments dict, final text)."""
        return map_step(attempt.workdir, step, offered)

    @staticmethod
    def _sse(response, item, text) -> bytes:
        in_progress = dict(response, status="in_progress", output=[])
        events = [("response.created", {"response": in_progress}),
                  ("response.in_progress", {"response": in_progress})]
        if item["type"] == "message":
            started = dict(item, status="in_progress", content=[])
            events += [
                ("response.output_item.added", {"output_index": 0, "item": started}),
                ("response.content_part.added", {"item_id": item["id"], "output_index": 0, "content_index": 0,
                                                 "part": {"type": "output_text", "text": "", "annotations": []}}),
                ("response.output_text.delta", {"item_id": item["id"], "output_index": 0, "content_index": 0, "delta": text}),
                ("response.output_text.done", {"item_id": item["id"], "output_index": 0, "content_index": 0, "text": text}),
                ("response.content_part.done", {"item_id": item["id"], "output_index": 0, "content_index": 0,
                                                "part": item["content"][0]}),
            ]
        elif item["type"] == "custom_tool_call":
            started = dict(item, status="in_progress", input="")
            events += [
                ("response.output_item.added", {"output_index": 0, "item": started}),
                ("response.custom_tool_call_input.delta", {"item_id": item["id"], "call_id": item["call_id"], "output_index": 0,
                                                           "delta": item["input"]}),
                ("response.custom_tool_call_input.done", {"item_id": item["id"], "call_id": item["call_id"], "output_index": 0,
                                                          "input": item["input"]}),
            ]
        else:
            started = dict(item, status="in_progress", arguments="")
            events += [
                ("response.output_item.added", {"output_index": 0, "item": started}),
                ("response.function_call_arguments.delta", {"item_id": item["id"], "output_index": 0, "delta": item["arguments"]}),
                ("response.function_call_arguments.done", {"item_id": item["id"], "output_index": 0, "arguments": item["arguments"]}),
            ]
        events += [("response.output_item.done", {"output_index": 0, "item": item}),
                   ("response.completed", {"response": response})]
        return "".join(
            f"event: {kind}\ndata: {json.dumps(dict(type=kind, sequence_number=n, **data))}\n\n"
            for n, (kind, data) in enumerate(events)
        ).encode()


# --- the step mapping: one script op onto whichever native tools the client offers ------------------------

def first_offered(table, offered):
    return next((n for n in table if n in offered), None)


def map_step(workdir, step, offered):
    """(tool name, arguments, text). Final steps have no tool. Raises LookupError(op) when no tool fits.

    Order of preference: a native tool for the op (Read/Write/Edit/Bash and their lowercase and
    snake_case spellings), then, for read and write only, the client's shell tool
    (cat / printf), which is how Codex CLI reads and writes files, then a code-execution tool
    (nanocode's `exec`) running that same shell command. A code tool's arguments are its program text (str).
    """
    op = step["op"]
    if op == "final":
        return None, None, step.get("text", "Done.")
    if op == "read":
        abs_path = f"{workdir}/{step['path']}"
        name = first_offered(READ_TOOLS, offered)
        if name:
            return name, READ_TOOLS[name](abs_path), None
        return shell_call(SHELL_READ(abs_path), offered, "read")
    if op == "write":
        abs_path = f"{workdir}/{step['path']}"
        name = first_offered(WRITE_TOOLS, offered)
        if name:
            return name, WRITE_TOOLS[name](abs_path, step["path"], step["content"]), None
        return shell_call(SHELL_WRITE(abs_path, step["content"]), offered, "write")
    if op == "edit":
        name = first_offered(EDIT_TOOLS, offered)
        if name is None:
            raise LookupError("edit")
        return name, EDIT_TOOLS[name](f"{workdir}/{step['path']}", step["old"], step["new"]), None
    return shell_call(step["command"], offered, "bash")


def shell_call(command, offered, op):
    """One shell command through the client's shell tool, else through its code-execution tool."""
    name = first_offered(BASH_TOOLS, offered)
    if name:
        return name, BASH_TOOLS[name](command), None
    name = first_offered(CODE_TOOLS, offered)
    if name:
        return name, CODE_TOOLS[name](command), None
    raise LookupError(op)


def offered_tools(body) -> list[str]:
    """Tool names the request offers: the `tools` list, plus tools nanocode sends as an `additional_tools`
    input item (its Code Mode `exec` and `wait`), sorted."""
    tools = [t for t in body.get("tools") or [] if isinstance(t, dict)]
    items = body.get("input") if isinstance(body.get("input"), list) else []
    for item in items:
        if isinstance(item, dict) and item.get("type") == "additional_tools" and isinstance(item.get("tools"), list):
            tools += [t for t in item["tools"] if isinstance(t, dict)]
    return sorted(t["name"] for t in tools if isinstance(t.get("name"), str) and t["name"])


def step_arg(step) -> str | None:
    """The one thing a step acts on, for the console: the relative path, the command or the final text (cut).
    Never a sandbox path."""
    if step["op"] in ("read", "write", "edit"):
        return step["path"][:200]
    if step["op"] == "bash":
        return step["command"][:200]
    return step.get("text", "Done.")[:200]


def count_tool_results(body, anthropic: bool) -> int:
    """How many tool results the conversation already holds: the script position."""
    if anthropic:
        n = 0
        for message in body.get("messages") or []:
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                n += sum(1 for block in content if isinstance(block, dict) and block.get("type") == "tool_result")
        return n
    items = body.get("input")
    items = items if isinstance(items, list) else []
    return sum(1 for item in items if isinstance(item, dict)
               and item.get("type") in ("function_call_output", "custom_tool_call_output"))


def _result_text(value) -> str:
    """A tool result's text: a string, or a list of text parts (Responses output parts, Messages text blocks)."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "".join(_result_text(part) for part in value)
    if isinstance(value, dict):
        text = value.get("text")
        if isinstance(text, str):
            return text
        if "content" in value:
            return _result_text(value["content"])
        if "output" in value:
            return _result_text(value["output"])
    return ""


def policy_input(text: str) -> str:
    """What the policy reads from a tool result. Harnesses wrap a command's stdout in their own framing (mini adds
    `exit=0`, Codex a header with the exit code and token count); a policy reads the command's one JSON line. When the
    whole text is JSON it is passed unchanged; otherwise the last line that is a JSON object; otherwise the text as is."""
    def is_object(candidate: str) -> bool:
        try:
            return isinstance(json.loads(candidate), dict)
        except ValueError:
            return False

    if is_object(text):
        return text
    for line in reversed(text.splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}") and is_object(line):
            return line
    return text


def latest_tool_result(body, anthropic: bool) -> str:
    """Text of the most recent tool result in the conversation (empty when it has none)."""
    if anthropic:
        for message in reversed(body.get("messages") or []):
            content = message.get("content") if isinstance(message, dict) else None
            if isinstance(content, list):
                for block in reversed(content):
                    if isinstance(block, dict) and block.get("type") == "tool_result":
                        return _result_text(block.get("content"))
        return ""
    items = body.get("input")
    for item in reversed(items if isinstance(items, list) else []):
        if isinstance(item, dict) and item.get("type") in ("function_call_output", "custom_tool_call_output"):
            return _result_text(item.get("output"))
    return ""


def response_item(attempt, index, name, arguments, text) -> dict:
    if name is None:
        return {"id": f"msg_{index}", "type": "message", "role": "assistant", "status": "completed",
                "content": [{"type": "output_text", "text": text, "annotations": []}]}
    if isinstance(arguments, str):
        # A freeform (custom) tool such as nanocode's Code Mode `exec`: the input is raw text, not JSON.
        return {"id": f"ctc_{index}", "type": "custom_tool_call", "status": "completed",
                "call_id": f"call_{attempt.key}_{index}", "name": name, "input": arguments}
    return {"id": f"fc_{index}", "type": "function_call", "status": "completed", "call_id": f"call_{attempt.key}_{index}",
            "name": name, "arguments": json.dumps(arguments)}


# --- Anthropic Messages --------------------------------------------------------------------------------

def anthropic_message(attempt, request_index, step_index, name, arguments, text, input_tokens) -> dict:
    if name is None:
        content, stop = [{"type": "text", "text": text}], "end_turn"
    else:
        content = [{"type": "tool_use", "id": f"toolu_{attempt.key}_{step_index}"[:64].replace("/", "_"), "name": name, "input": arguments}]
        stop = "tool_use"
    return {"id": f"msg_{attempt.key}_{request_index}"[:64], "type": "message", "role": "assistant", "model": attempt.model,
            "content": content, "stop_reason": stop, "stop_sequence": None,
            "usage": {"input_tokens": input_tokens, "output_tokens": 16, "cache_creation_input_tokens": 0,
                      "cache_read_input_tokens": 0}}


def anthropic_events(message) -> list[tuple[str, dict]]:
    """The documented streaming sequence: message_start, per block start/delta/stop, message_delta, message_stop."""
    usage = message["usage"]
    start = dict(message, content=[], stop_reason=None, usage=dict(usage, output_tokens=1))
    events = [("message_start", {"type": "message_start", "message": start}), ("ping", {"type": "ping"})]
    for i, block in enumerate(message["content"]):
        if block["type"] == "text":
            events += [("content_block_start", {"type": "content_block_start", "index": i, "content_block": {"type": "text", "text": ""}}),
                       ("content_block_delta", {"type": "content_block_delta", "index": i, "delta": {"type": "text_delta", "text": block["text"]}})]
        else:
            events += [("content_block_start", {"type": "content_block_start", "index": i,
                                                "content_block": {"type": "tool_use", "id": block["id"], "name": block["name"], "input": {}}}),
                       ("content_block_delta", {"type": "content_block_delta", "index": i,
                                                "delta": {"type": "input_json_delta", "partial_json": json.dumps(block["input"])}})]
        events.append(("content_block_stop", {"type": "content_block_stop", "index": i}))
    events += [("message_delta", {"type": "message_delta", "delta": {"stop_reason": message["stop_reason"], "stop_sequence": None},
                                  "usage": {"output_tokens": usage["output_tokens"]}}),
               ("message_stop", {"type": "message_stop"})]
    return events


def anthropic_sse(message) -> bytes:
    return "".join(f"event: {kind}\ndata: {json.dumps(data)}\n\n" for kind, data in anthropic_events(message)).encode()
