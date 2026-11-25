"""Relay model source: real models behind the same per-attempt routes as the scripted model.

Each harness still talks to http://127.0.0.1:<port>/s/<token>/v1, so adapters do not change. Admission is the
scripted model's own (route, request budget, body size, planned model / effort / output cap); an admitted request
is forwarded unchanged to the upstream gateway (an LLM proxy or `swarm gateway`, which holds the credentials) and the
reply is passed back as it arrives. The relay never forwards the harness's own credentials, and it reads token usage
from the reply to keep a per-run token budget. Over budget it answers 429 without calling upstream.
"""
import json
import threading
import time
import http.client

from .scripted import ScriptedModel
from .upstream import Pool

# Request headers a harness may need the provider to see. Everything else, credentials above all, stays behind.
FORWARD_HEADERS = ("content-type", "accept", "anthropic-version", "anthropic-beta", "openai-beta")
UPSTREAM_PATHS = {"responses": "/v1/responses", "messages": "/v1/messages", "count_tokens": "/v1/messages/count_tokens",
                  "chat": "/v1/chat/completions"}


class RunBudget:
    """Input + output tokens across every attempt of one run."""

    def __init__(self, max_tokens: int | None):
        self.max_tokens = max_tokens
        self.used = 0
        self.lock = threading.Lock()

    def exhausted(self) -> bool:
        with self.lock:
            return self.max_tokens is not None and self.used >= self.max_tokens

    def add(self, tokens: int):
        with self.lock:
            self.used += max(0, int(tokens or 0))


def usage_of(kind: str, usage: dict | None) -> dict:
    """Normalise a provider usage block to input / cached / output tokens."""
    usage = usage if isinstance(usage, dict) else {}
    num = lambda v: v if isinstance(v, int) and not isinstance(v, bool) else 0
    if kind == "messages":
        cached = num(usage.get("cache_read_input_tokens"))
        written = usage.get("cache_creation_input_tokens")
        written = written if isinstance(written, int) and not isinstance(written, bool) else None  # unreported stays null
        return {"input_tokens": num(usage.get("input_tokens")) + cached + (written or 0), "cached_input_tokens": cached,
                "output_tokens": num(usage.get("output_tokens")), "cache_write_input_tokens": written}
    if kind == "chat":
        details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
        return {"input_tokens": num(usage.get("prompt_tokens")), "cached_input_tokens": num(details.get("cached_tokens")),
                "output_tokens": num(usage.get("completion_tokens")), "cache_write_input_tokens": None}
    details = usage.get("input_tokens_details") if isinstance(usage.get("input_tokens_details"), dict) else {}
    return {"input_tokens": num(usage.get("input_tokens")), "cached_input_tokens": num(details.get("cached_tokens")),
            "output_tokens": num(usage.get("output_tokens")), "cache_write_input_tokens": None}


def _id(item) -> str | None:
    value = item.get("id") if isinstance(item, dict) else None
    return value if isinstance(value, str) and value else None


class SSEUsage:
    """Reads usage, completion state, and response model out of a streamed reply (Responses, Messages, or Chat)."""

    def __init__(self, kind: str):
        self.kind, self.buffer, self.usage, self.id = kind, b"", {}, None
        self.done, self.model = False, None

    def feed(self, chunk: bytes):
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:"):
                continue
            raw_data = line[5:].strip()
            if self.kind == "chat" and raw_data == b"[DONE]":
                self.done = True
                continue
            try:
                event = json.loads(raw_data)
            except ValueError:
                continue
            if not isinstance(event, dict):
                continue
            kind = event.get("type")
            if self.kind == "chat":
                self.id = self.id or _id(event)
                if isinstance(event.get("model"), str) and event["model"]:
                    self.model = event["model"]
                if isinstance(event.get("usage"), dict):
                    self.usage = usage_of("chat", event["usage"])  # the last chunk carries it (stream_options.include_usage)
                continue
            if kind in ("response.created", "message_start"):
                obj = event.get("response") or event.get("message") or {}
                self.id = self.id or _id(obj)
                if isinstance(obj.get("model"), str) and obj["model"]:
                    self.model = obj["model"]
            if self.kind == "responses" and kind in ("response.completed", "response.incomplete", "response.failed"):
                resp = event.get("response") or {}
                if isinstance(resp.get("model"), str) and resp["model"]:
                    self.model = resp["model"]
                self.usage = usage_of("responses", resp.get("usage"))
                if kind == "response.completed":
                    self.done = True
            elif self.kind == "messages" and kind == "message_start":
                self.usage = usage_of("messages", (event.get("message") or {}).get("usage"))
            elif self.kind == "messages" and kind == "message_delta":
                out = (event.get("usage") or {}).get("output_tokens")
                if isinstance(out, int):
                    self.usage = dict(self.usage or usage_of("messages", {}), output_tokens=out)
            elif self.kind == "messages" and kind == "message_stop":
                self.done = True


class RelayModel(ScriptedModel):
    SERVES_CHAT = True

    def __init__(self, upstream: str, max_run_tokens: int | None = None, host="127.0.0.1", port=0, timeout_s=300,
                 upstream_key: str | None = None):
        self.upstream = upstream.rstrip("/")
        self.upstream_key = upstream_key  # the lane's own token for the swarm gateway; never the harness's
        self.budget = RunBudget(max_run_tokens)
        self.timeout_s = timeout_s
        self.pool = Pool(self.upstream, timeout_s)  # kept-alive connections: no TLS handshake per request
        super().__init__(host=host, port=port, strict=True)

    def close(self):
        super().close()
        self.pool.close()

    def _serve(self, handler, attempt, row, kind, body, raw, started):
        if kind != "count_tokens" and self.budget.exhausted():
            row.update(status=429, error="run_token_budget_exhausted")
            self._trace(attempt, row)
            handler._send(429, {"error": {"message": "run token budget exhausted", "type": "rate_limit_error"}})
            return
        if kind == "chat" and body.get("stream") and not (body.get("stream_options") or {}).get("include_usage"):
            # A streamed chat reply carries usage only when asked; ask, so the run token budget can count it.
            # Same for every harness on this wire; recorded on the row.
            body["stream_options"] = dict(body.get("stream_options") or {}, include_usage=True)
            raw = json.dumps(body).encode()
            row["usage_requested"] = True
        headers = {k: v for k, v in handler.headers.items() if k.lower() in FORWARD_HEADERS}
        headers.setdefault("Content-Type", "application/json")
        if kind in ("messages", "count_tokens"):
            # Ante 0.3.0 sends an empty anthropic-version, which Anthropic refuses (400). Fill in the stable version.
            for k in [k for k in headers if k.lower() == "anthropic-version" and not headers[k].strip()]:
                del headers[k]
            if not any(k.lower() == "anthropic-version" for k in headers):
                headers["anthropic-version"] = "2023-06-01"
        if self.upstream_key:
            headers["Authorization"] = f"Bearer {self.upstream_key}"
        row["at"] = round(time.time(), 3)
        try:
            conn, reply = self.pool.post(UPSTREAM_PATHS[kind], raw, headers)
            status = reply.status
        except (OSError, http.client.HTTPException) as error:
            row.update(status=502, error="upstream_unreachable", detail=str(error)[:200])
            self._trace(attempt, row)
            handler._send(502, {"error": {"message": "upstream unreachable", "type": "api_error"}})
            return
        content_type = reply.getheader("Content-Type", "application/json")
        streamed = "text/event-stream" in content_type
        complete = False
        try:
            if streamed:
                usage = self._pipe(handler, reply, status, content_type, kind, started, row)
            else:
                payload = reply.read()
                first = time.monotonic()
                try:
                    reply_json = json.loads(payload)
                    usage = usage_of(kind, reply_json.get("usage")) if kind != "count_tokens" else {}
                    row["upstream_id"] = _id(reply_json) if kind != "count_tokens" else None
                    if kind != "count_tokens" and isinstance(reply_json, dict):
                        if isinstance(reply_json.get("model"), str) and reply_json["model"]:
                            row["response_model"] = reply_json["model"]
                        if status < 400:
                            row["stream_complete"] = kind != "responses" or reply_json.get("status") == "completed"
                except (ValueError, AttributeError):
                    usage = {}
                    if status < 400 and kind != "count_tokens":
                        row["stream_complete"] = False
                complete = True
                if status >= 400:   # the provider's error message, so a refusal can be diagnosed from the row
                    row["detail"] = payload[:300].decode("utf-8", "replace")
                row["first_byte_s"] = round(first - started, 6)
                self._finish(attempt, row, status, usage, kind)
                handler._send(status, payload, content_type)
                return
            complete = True
        finally:
            self.pool.done(conn, reply, complete)
        self._finish(attempt, row, status, usage, kind)

    def _pipe(self, handler, reply, status, content_type, kind, started, row) -> dict:
        """Stream the upstream reply to the harness as it arrives; return the usage it carried."""
        reader = SSEUsage(kind)
        handler.send_response(status)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Cache-Control", "no-cache")
        handler.send_header("Connection", "close")  # the body ends when the connection does
        handler.close_connection = True
        handler.end_headers()
        first = None
        while True:
            chunk = reply.read1(65536)
            if not chunk:
                break
            if first is None:
                first = time.monotonic()
                row["first_byte_s"] = round(first - started, 6)
            reader.feed(chunk)
            try:
                handler.wfile.write(chunk)
                handler.wfile.flush()
            except OSError:
                row["client_gone"] = True  # the harness hung up; keep reading so the usage is still counted
        row["upstream_id"] = reader.id
        if status < 400:
            row["stream_complete"] = reader.done
            if reader.model:
                row["response_model"] = reader.model
        return reader.usage

    def _finish(self, attempt, row, status, usage, kind):
        sent = time.monotonic()
        tokens = (usage.get("input_tokens", 0) + usage.get("output_tokens", 0)) if usage else 0
        if kind != "count_tokens":
            self.budget.add(tokens)
        allowed_models = {m for m in (attempt.model, attempt.helper) if m}
        resp_model = row.get("response_model")
        model_mismatch = bool(status < 400 and kind != "count_tokens" and resp_model and allowed_models and resp_model not in allowed_models)
        stream_incomplete = bool(status < 400 and row.get("stream_complete") is False)
        completed = status < 400 and not stream_incomplete and not model_mismatch
        with self.lock:
            row.update(status=status, completed=completed, sent_mono=round(sent, 6),
                       elapsed_s=round(sent - row.get("recv_mono", sent), 6), **usage)
            if status >= 400:
                row["error"] = f"upstream_{status}"
            elif stream_incomplete:
                row["error"] = "stream_incomplete"
            elif model_mismatch:
                row["error"] = "response_model_mismatch"
            if kind == "count_tokens":
                row["kind"] = "count_tokens"
        self._trace(attempt, row)

    def _probe(self, handler):
        """A model list names only the planned model, so a harness cannot discover and switch to another."""
        attempt, route = self._attempt_for(handler)
        if attempt is None:
            handler._send(404, {"error": {"message": "unknown attempt"}})
            return
        with self.lock:
            attempt.rows.append({"index": len(attempt.rows), "request_id": f"{attempt.key}#{len(attempt.rows)}",
                                 "kind": "probe", "method": handler.command,
                                 "path": "/s/<token>/" + "/".join(route), "error": None, "status": 200, "completed": True})
        if route and route[-1] == "models":
            ids = [attempt.model] + ([attempt.helper] if attempt.helper else [])
            handler._send(200, {"object": "list", "data": [{"id": m, "object": "model", "created": 0, "owned_by": "swarm"} for m in ids]})
            return
        handler._send(200, {"ok": True})
