"""shared-v3 loopback relay in front of a managed model gateway (the VM platform's LLM integration in the published runs).

Routes (per run and task, loopback only):
  /<run>/<task>/v1/responses               OpenAI Responses (same behaviour as shared-v2)
  /<run>/<task>/v1/messages                Anthropic Messages
  /<run>/<task>/v1/messages/count_tokens   forwarded with the model pinned (404 from upstream is passed through)

The relay pins the exact model (422 otherwise, no fallback) and forces the effort controls and the output cap.
Responses: reasoning.effort and max_output_tokens.
Messages: max_tokens, thinking={"type":"adaptive"}, output_config.effort; temperature/top_p/top_k are removed
(Claude Sonnet 5.5 rejects non-default sampling values) and the removal is audited.
Client credentials are never forwarded: only an allowlist of non-auth headers goes upstream (the integration
authenticates the VM). Audits record model, forced settings, status and usage, never prompts or responses.
"""
import json, os, re, threading, time, urllib.request, urllib.error, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MAX_TOKENS = 16384
# The gateway authenticates the host itself, so no credential is configured here. Set BENCH_MODEL_UPSTREAM.
UPSTREAM = os.environ.get('BENCH_MODEL_UPSTREAM', 'https://model-gateway.invalid')
ANTHROPIC_VERSION = '2023-06-01'
MESSAGES_THINKING = {'type': 'adaptive'}
SAMPLING_KEYS = ('temperature', 'top_p', 'top_k')
FORWARD_HEADERS = ('anthropic-version', 'anthropic-beta')  # plus content-type/accept, set explicitly below
QUOTA_PREFIXES = ('x-ratelimit-', 'x-codex-', 'anthropic-ratelimit-')
QUOTA_WORDS = ('remaining', 'limit', 'reset', 'used-percent', 'window')


class Gate:
    def __init__(self, run_dir, run_id, model, effort, upstream=UPSTREAM, port=8766, max_tokens=MAX_TOKENS):
        self.rows = []
        self.lock = threading.Lock()
        outer = self
        route_re = re.compile(r'/' + re.escape(run_id) + r'/([a-z0-9-]+)/v1/(responses|messages/count_tokens|messages)')

        class H(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def do_POST(self):
                bare, _, query = self.path.partition('?')
                match = route_re.fullmatch(bare)
                if not match:
                    self.send_error(404)
                    return
                route = {'responses': 'responses', 'messages': 'messages', 'messages/count_tokens': 'count_tokens'}[match[2]]
                n = int(self.headers.get('Content-Length', '0'))
                if not 0 < n <= 4 * 1024 * 1024:
                    self.send_error(413)
                    return
                try:
                    data = json.loads(self.rfile.read(n))
                except ValueError:
                    self.send_error(400)
                    return
                if not isinstance(data, dict):
                    self.send_error(400)
                    return
                row = {'request_id': uuid.uuid4().hex, 'received_at': time.time(), 'task': match[1], 'route': route,
                       'model': data.get('model'), 'accepted': data.get('model') == model, 'status': None}
                if route == 'responses':
                    row.update(requested_effort=(data.get('reasoning') or {}).get('effort'), effort=effort,
                               requested_max_output_tokens=data.get('max_output_tokens'), max_output_tokens=max_tokens)
                elif route == 'messages':
                    cfg = data.get('output_config') if isinstance(data.get('output_config'), dict) else {}
                    row.update(requested_effort=cfg.get('effort'), effort=effort, requested_thinking=data.get('thinking'),
                               thinking=MESSAGES_THINKING, requested_max_tokens=data.get('max_tokens'), max_tokens=max_tokens,
                               stripped_sampling=sorted(k for k in SAMPLING_KEYS if k in data),
                               tool_choice=(data.get('tool_choice') or {}).get('type') if isinstance(data.get('tool_choice'), dict) else None,
                               anthropic_beta=self.headers.get('anthropic-beta'))
                if not row['accepted']:
                    row['status'] = 422
                    outer.save(row)
                    self.send_error(422, 'Benchmark requires the exact configured model and reasoning effort')
                    return
                outer.save(row)
                if route == 'responses':
                    data.setdefault('reasoning', {})['effort'] = effort
                    data['max_output_tokens'] = max_tokens
                elif route == 'messages':
                    data['max_tokens'] = max_tokens
                    data['thinking'] = dict(MESSAGES_THINKING)
                    data.setdefault('output_config', {})
                    if not isinstance(data['output_config'], dict):
                        data['output_config'] = {}
                    data['output_config']['effort'] = effort
                    for key in SAMPLING_KEYS:
                        data.pop(key, None)
                body = json.dumps(data).encode()
                headers = {'Content-Type': 'application/json', 'Accept': self.headers.get('Accept', 'application/json')}
                if route != 'responses':
                    headers['anthropic-version'] = self.headers.get('anthropic-version') or ANTHROPIC_VERSION
                    if self.headers.get('anthropic-beta'):
                        headers['anthropic-beta'] = self.headers['anthropic-beta']
                url = upstream.rstrip('/') + '/v1/' + match[2] + ('?' + query if query else '')
                req = urllib.request.Request(url, data=body, headers=headers)
                try:
                    with urllib.request.urlopen(req, timeout=180) as response:
                        row['status'] = response.status
                        ctype = response.headers.get('Content-Type', 'application/json')
                        is_sse = ctype.startswith('text/event-stream')
                        if is_sse:
                            row['stream_complete'] = False
                        outer.save(row)
                        # Quota headers are admin-only and never added to public request rows.
                        quota = {k: v for k, v in response.headers.items() if k.lower().startswith(QUOTA_PREFIXES) and any(x in k.lower() for x in QUOTA_WORDS)}
                        if quota:
                            admin = run_dir.parent.parent / 'admin'
                            admin.mkdir(exist_ok=True)
                            with (admin / 'quota-headers.jsonl').open('a') as f:
                                f.write(json.dumps({'run_id': run_id, 'observed_at': time.time(), 'headers': quota}) + '\n')
                        self.send_response(response.status)
                        self.send_header('Content-Type', ctype)
                        if is_sse:
                            self.send_header('Cache-Control', 'no-cache')
                        self.end_headers()
                        buffer = b''
                        json_body = bytearray()
                        is_json = ctype.startswith('application/json')
                        while chunk := response.read1(65536):
                            buffer += chunk
                            if is_json and len(json_body) + len(chunk) <= 2 * 1024 * 1024:
                                json_body.extend(chunk)
                            while b'\n' in buffer:
                                line, buffer = buffer.split(b'\n', 1)
                                if line.startswith(b'data: '):
                                    try:
                                        event = json.loads(line[6:])
                                    except ValueError:
                                        continue
                                    outer.observe(row, route, event)
                            self.wfile.write(chunk)  # streamed through immediately, never buffered for the client
                            self.wfile.flush()
                        if is_json and json_body:
                            try:
                                payload = json.loads(json_body)
                                if route == 'responses':
                                    row['usage'] = payload.get('usage') or (payload.get('response') or {}).get('usage')
                                    row['response_model'] = payload.get('model')
                                elif route == 'messages':
                                    row['usage'] = payload.get('usage')
                                    row['response_model'] = payload.get('model')
                                    row['stop_reason'] = payload.get('stop_reason')
                                else:
                                    row['input_tokens_counted'] = payload.get('input_tokens')
                                row['stream_complete'] = route != 'responses' or payload.get('status') == 'completed'
                            except (ValueError, AttributeError):
                                row['stream_complete'] = False
                        elif is_json:
                            row['stream_complete'] = False
                        outer.save(row)
                except urllib.error.HTTPError as e:
                    payload = e.read()
                    row['status'] = e.code
                    try:
                        err = json.loads(payload).get('error')
                        row['error'] = (err.get('type'), str(err.get('message'))[:300]) if isinstance(err, dict) else str(err)[:300]
                    except (ValueError, AttributeError):
                        pass
                    outer.save(row)
                    self.send_response(e.code)
                    self.send_header('Content-Type', 'application/json')
                    self.end_headers()
                    self.wfile.write(payload)
                except (BrokenPipeError, ConnectionResetError):
                    if row.get('stream_complete') is not True:
                        row['stream_complete'] = False
                        outer.save(row)
                except Exception:
                    if row['status'] is None:
                        row['status'] = 502
                        outer.save(row)
                        self.send_error(502, 'Managed model request failed')
                    elif row.get('stream_complete') is not True:
                        row['stream_complete'] = False
                        outer.save(row)

        class BenchHTTPServer(ThreadingHTTPServer):
            request_queue_size = 128

        self.path = run_dir / 'request-audit.jsonl'
        self.server = BenchHTTPServer(('127.0.0.1', port), H)
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def observe(self, row, route, event):
        kind = event.get('type')
        if route == 'responses':
            if kind == 'response.completed':
                response = event.get('response', {})
                row['usage'] = response.get('usage')
                row['response_model'] = response.get('model')
                row['stream_complete'] = True
                self.save(row)
        elif route == 'messages':
            if kind == 'message_start':
                message = event.get('message', {})
                row['response_model'] = message.get('model')
                row['usage'] = dict(message.get('usage') or {})
            elif kind == 'message_delta':
                usage = row.setdefault('usage', {})
                usage.update({k: v for k, v in (event.get('usage') or {}).items() if v is not None})
                row['stop_reason'] = (event.get('delta') or {}).get('stop_reason')
                self.save(row)
            elif kind == 'message_stop':
                row['stream_complete'] = True
                self.save(row)

    def save(self, row):
        with self.lock:
            if not any(x is row for x in self.rows):
                self.rows.append(row)
            with self.path.open('a') as f:
                f.write(json.dumps(row) + '\n')

    def close(self):
        self.server.shutdown()
        self.server.server_close()
