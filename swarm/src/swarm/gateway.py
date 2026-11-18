"""swarm gateway: provider adapters for subscriptions a plain HTTPS LLM proxy cannot carry.

Lanes' relays call this service instead of a provider. It holds the provider credentials (never the lanes), and
each client (one per lane) presents its own token, which grants only the routes listed for it:

- claude-oauth: a Claude sign-in token (`claude setup-token`) to api.anthropic.com. In the shipped configuration
  only the Claude Code lane is given this route.
- bearer / anthropic-key: an API key or subscription key sent as the provider expects (OpenCode Go serves
  OpenAI-style routes with a bearer token and Anthropic-style routes with x-api-key).

A request is routed by its API (responses, messages, count_tokens, chat) and model; bodies pass unchanged and
replies stream through. Every request is logged with its token usage, and each client has a daily token cap.
"""
import hashlib
import hmac
import json
import os
import threading
import time
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .relay import SSEUsage, usage_of
from .upstream import Pool

APIS = {"/v1/responses": "responses", "/v1/messages": "messages", "/v1/messages/count_tokens": "count_tokens",
        "/v1/chat/completions": "chat"}
PASS_HEADERS = ("content-type", "accept", "anthropic-version", "anthropic-beta", "openai-beta")
OAUTH_BETA = "oauth-2025-04-20"
MAX_BODY = 8 * 1024 * 1024
# integration: the upstream is an HTTP proxy that injects the provider key itself (the key never reaches this
# machine); the gateway adds no auth header.
AUTHS = {"claude-oauth", "bearer", "anthropic-key", "integration"}


def token_hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def chat_usage(usage: dict | None) -> dict:
    usage = usage if isinstance(usage, dict) else {}
    details = usage.get("prompt_tokens_details") if isinstance(usage.get("prompt_tokens_details"), dict) else {}
    num = lambda v: v if isinstance(v, int) and not isinstance(v, bool) else 0
    return {"input_tokens": num(usage.get("prompt_tokens")), "cached_input_tokens": num(details.get("cached_tokens")),
            "output_tokens": num(usage.get("completion_tokens"))}


class ChatSSEUsage(SSEUsage):
    """Chat Completions streams carry usage on a final chunk (stream_options.include_usage)."""

    def feed(self, chunk: bytes):
        self.buffer += chunk
        while b"\n" in self.buffer:
            line, self.buffer = self.buffer.split(b"\n", 1)
            line = line.strip()
            if not line.startswith(b"data:") or line[5:].strip() == b"[DONE]":
                continue
            try:
                event = json.loads(line[5:].strip())
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(event.get("usage"), dict):
                self.usage = chat_usage(event["usage"])


class Gateway:
    def __init__(self, config_path: Path, log_path: Path, env=None, timeout_s=300):
        env = os.environ if env is None else env
        doc = json.loads(Path(config_path).read_text())
        self.routes = {}
        self.disabled = []  # routes whose secret is not set yet: off until it is set, never fatal
        for name, r in doc["routes"].items():
            if r.get("auth") not in AUTHS:
                raise ValueError(f"route {name}: auth must be one of {sorted(AUTHS)}")
            if not str(r.get("upstream", "")).startswith("https://") and not str(r.get("upstream", "")).startswith("http://127.0.0.1"):
                raise ValueError(f"route {name}: upstream must be https://")
            secret = "" if r["auth"] == "integration" else env.get(r["secret_env"], "")
            if not secret and r["auth"] != "integration":
                self.disabled.append(name)
                continue
            apis = set(r.get("apis", []))
            if not apis or apis - set(APIS.values()):
                raise ValueError(f"route {name}: apis must be some of {sorted(APIS.values())}")
            models = [m for m in r.get("models", []) if isinstance(m, str)]
            if not models:
                raise ValueError(f"route {name}: list its models")
            self.routes[name] = {"upstream": r["upstream"].rstrip("/"), "auth": r["auth"], "secret": secret, "apis": apis,
                                 "models": models}
        # trust_source_vm: behind exe.dev's VM-to-VM integration, the platform sets X-Exedev-Source-Vm (callers cannot
        # forge it), so a lane is known by its VM name and holds no token at all. Only for a gateway reachable that way.
        self.trust_source_vm = bool(doc.get("trust_source_vm"))
        self.by_vm = {}
        self.clients = {}
        for c in doc["clients"]:
            unknown = set(c["routes"]) - set(self.routes) - set(self.disabled)
            if unknown:
                raise ValueError(f"client {c['name']}: unknown routes {sorted(unknown)}")
            entry = {"name": c["name"], "routes": [r for r in c["routes"] if r in self.routes], "max_tokens_per_day": int(c.get("max_tokens_per_day", 5_000_000))}
            if c.get("token_sha256"):
                self.clients[c["token_sha256"]] = entry
            if c.get("source_vm"):
                self.by_vm[c["source_vm"]] = entry
        self.log_path = Path(log_path)
        self.timeout_s = timeout_s
        self.pools: dict[str, Pool] = {}  # one kept-alive pool per upstream
        self.pools_lock = threading.Lock()
        self.lock = threading.Lock()
        self.spent: dict[tuple[str, str], int] = {}  # (client, UTC day) -> tokens
        self._load_spent()

    def _load_spent(self):
        day = time.strftime("%Y-%m-%d", time.gmtime())
        try:
            for line in self.log_path.read_text().splitlines():
                row = json.loads(line)
                if row.get("day") == day:
                    key = (row["client"], day)
                    self.spent[key] = self.spent.get(key, 0) + row.get("input_tokens", 0) + row.get("output_tokens", 0)
        except (OSError, ValueError, KeyError):
            pass

    def pool(self, upstream: str) -> Pool:
        with self.pools_lock:
            if upstream not in self.pools:
                self.pools[upstream] = Pool(upstream, self.timeout_s)
            return self.pools[upstream]

    def client(self, headers) -> dict | None:
        if self.trust_source_vm:
            return self.by_vm.get(headers.get("X-Exedev-Source-Vm", ""))
        auth = headers.get("Authorization", "")
        token = auth[7:].strip() if auth.lower().startswith("bearer ") else headers.get("x-api-key", "")
        if not token:
            return None
        digest = token_hash(token)
        for known, c in self.clients.items():
            if hmac.compare_digest(known, digest):
                return c
        return None

    def route(self, client: dict, api: str, model) -> tuple[str, dict] | None:
        for name in client["routes"]:
            r = self.routes[name]
            if api in r["apis"] and model in r["models"]:
                return name, r
        return None

    def models(self, client: dict) -> list[str]:
        return sorted({m for name in client["routes"] for m in self.routes[name]["models"]})

    def over_cap(self, client: dict) -> bool:
        day = time.strftime("%Y-%m-%d", time.gmtime())
        with self.lock:
            return self.spent.get((client["name"], day), 0) >= client["max_tokens_per_day"]

    def record(self, row: dict):
        with self.lock:
            key = (row["client"], row["day"])
            self.spent[key] = self.spent.get(key, 0) + row.get("input_tokens", 0) + row.get("output_tokens", 0)
            self.log_path.parent.mkdir(parents=True, exist_ok=True)
            with open(self.log_path, "a") as f:
                f.write(json.dumps(row, sort_keys=True) + "\n")

    @staticmethod
    def upstream_headers(route: dict, incoming) -> dict:
        headers = {k.lower(): v for k, v in incoming.items() if k.lower() in PASS_HEADERS}
        headers.setdefault("content-type", "application/json")
        secret = route["secret"]
        if route["auth"] == "claude-oauth":
            headers["authorization"] = f"Bearer {secret}"
            betas = [b.strip() for b in headers.pop("anthropic-beta", "").split(",") if b.strip()]
            headers["anthropic-beta"] = ",".join(dict.fromkeys(betas + [OAUTH_BETA]))
            headers.setdefault("anthropic-version", "2023-06-01")
        elif route["auth"] == "integration":
            pass
        elif route["auth"] == "anthropic-key":
            headers["x-api-key"] = secret
            headers.setdefault("anthropic-version", "2023-06-01")
        else:
            headers["authorization"] = f"Bearer {secret}"
        return headers


def handler_for(gw: Gateway):
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"
        timeout = 60

        def log_message(self, *a):
            pass

        def _send(self, code, body):
            payload = json.dumps(body).encode()
            self.send_response(code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            path = self.path.split("?", 1)[0]
            if path == "/healthz":
                return self._send(200, {"ok": True})
            client = gw.client(self.headers)
            if client is None:
                return self._send(401, {"error": {"message": "unknown client"}})
            if path == "/v1/models":
                return self._send(200, {"object": "list", "data": [{"id": m, "object": "model", "owned_by": "swarm-gateway"} for m in gw.models(client)]})
            return self._send(404, {"error": {"message": "not found"}})

        def do_POST(self):
            started = time.monotonic()
            api = APIS.get(self.path.split("?", 1)[0])
            client = gw.client(self.headers)
            if client is None:
                return self._send(401, {"error": {"message": "unknown client"}})
            if api is None:
                return self._send(404, {"error": {"message": "unknown route"}})
            try:
                length = int(self.headers.get("Content-Length", 0))
            except ValueError:
                length = -1
            if not 0 < length <= MAX_BODY:
                return self._send(413, {"error": {"message": "bad length"}})
            raw = self.rfile.read(length)
            try:
                body = json.loads(raw)
                assert isinstance(body, dict)
            except (ValueError, AssertionError):
                return self._send(400, {"error": {"message": "invalid json"}})
            found = gw.route(client, api, body.get("model"))
            if found is None:
                return self._send(403, {"error": {"message": f"{client['name']} may not use {body.get('model')!r} over {api}"}})
            name, route = found
            if api != "count_tokens" and gw.over_cap(client):
                return self._send(429, {"error": {"message": "daily token cap reached", "type": "rate_limit_error"}})
            path = {"responses": "/v1/responses", "messages": "/v1/messages", "count_tokens": "/v1/messages/count_tokens",
                    "chat": "/v1/chat/completions"}[api]
            row = {"client": client["name"], "route": name, "api": api, "model": body.get("model"),
                   "day": time.strftime("%Y-%m-%d", time.gmtime()), "at": round(time.time(), 3)}
            try:
                conn, reply = gw.pool(route["upstream"]).post(path, raw, gw.upstream_headers(route, self.headers))
                status = reply.status
            except (OSError, http.client.HTTPException) as error:
                gw.record(row | {"status": 502, "error": str(error)[:200]})
                return self._send(502, {"error": {"message": "upstream unreachable"}})
            complete = False
            try:
                ctype = reply.getheader("Content-Type", "application/json")
                if "text/event-stream" in ctype:
                    reader = ChatSSEUsage("chat") if api == "chat" else SSEUsage(api)
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Cache-Control", "no-cache")
                    self.send_header("Connection", "close")
                    self.close_connection = True
                    self.end_headers()
                    while True:
                        chunk = reply.read1(65536)
                        if not chunk:
                            break
                        reader.feed(chunk)
                        try:
                            self.wfile.write(chunk)
                            self.wfile.flush()
                        except OSError:
                            pass  # the relay hung up; keep reading so usage still counts
                    usage = reader.usage
                    complete = True
                else:
                    payload = reply.read()
                    complete = True
                    try:
                        data = json.loads(payload)
                        usage = chat_usage(data.get("usage")) if api == "chat" else usage_of(api, data.get("usage")) if api != "count_tokens" else {}
                    except (ValueError, AttributeError):
                        usage = {}
                    self.send_response(status)
                    self.send_header("Content-Type", ctype)
                    self.send_header("Content-Length", str(len(payload)))
                    self.end_headers()
                    self.wfile.write(payload)
            finally:
                gw.pool(route["upstream"]).done(conn, reply, complete)
            gw.record(row | {"status": status, "elapsed_s": round(time.monotonic() - started, 3), **(usage or {})})

    return Handler


def serve_gateway(config: Path, log: Path, host="127.0.0.1", port=9300):
    gw = Gateway(config, log)

    class Server(ThreadingHTTPServer):
        daemon_threads = True
        request_queue_size = 512

    srv = Server((host, port), handler_for(gw))
    off = f" · off until their secret is set: {', '.join(gw.disabled)}" if gw.disabled else ""
    print(f"swarm gateway on {host}:{port} · routes {', '.join(sorted(gw.routes)) or 'none'} · {len(gw.clients) + len(gw.by_vm)} clients{off}", flush=True)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass
