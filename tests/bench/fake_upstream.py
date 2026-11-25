#!/usr/bin/env python3
"""Fake managed model gateway for local relay tests only. No network, no model calls.

POST /v1/messages (stream or not), /v1/messages/count_tokens, /v1/responses (stream or not).
Scripted exchange for toolbox task t01 (slugify): turn 1 = one tool call that writes the solution and runs the
test; once a tool result is present in the request, turn 2 = end_turn / completed with a short text.
Every request (headers minus auth values, key request fields) is appended to --log as JSONL so tests can assert
what the relay forwarded."""
import argparse, json, sys, threading, time, uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

SOLUTIONS = {
    'slugify': ('tests.test_01_slugify', '''import re
def slugify(title: str) -> str:
    """Return a URL slug: lowercase, words joined by single hyphens, only [a-z0-9-]."""
    return "-".join(re.findall(r"[a-z0-9]+", title.lower()))
'''),
    'wordfreq': ('tests.test_02_wordfreq', '''import re
def word_frequencies(text: str) -> dict[str, int]:
    out = {}
    for w in re.findall(r"[a-z0-9]+(?:'[a-z0-9]+)*", text.lower()):
        out[w] = out.get(w, 0) + 1
    return out
'''),
    'duration': ('tests.test_03_duration', '''import re
def parse_duration(spec: str) -> int:
    m = re.fullmatch(r"(?:(\\d+)h)?(?:(\\d+)m)?(?:(\\d+)s)?", spec)
    if not spec or not m:
        raise ValueError(spec)
    h, mi, s = (int(x) if x else 0 for x in m.groups())
    return h * 3600 + mi * 60 + s
'''),
    'median': ('tests.test_04_median', '''import statistics
def running_median(values: list[float]) -> list[float]:
    return [statistics.median(values[:i + 1]) for i in range(len(values))]
'''),
}
import re as _re


def command_for(data):
    """Pick the scripted solution from the task prompt ("Implement X in toolbox/<module>.py")."""
    m = _re.search(r'Implement \w+ in toolbox/(\w+)\.py', json.dumps(data.get('messages') or data.get('input') or ''))
    module = m.group(1) if m else 'slugify'
    test, code = SOLUTIONS[module]
    return f"cat > toolbox/{module}.py <<'PYEOF'\n" + code + f"PYEOF\npython3 -m unittest {test} -q"


SHELL_NAMES = ['Bash', 'bash', 'terminal', 'shell_command', 'exec_command', 'shell', 'execute_bash']
ARGS = None
LOCK = threading.Lock()


def pick_tool(tools):
    names = {}
    for t in tools or []:
        name = t.get('name') or (t.get('function') or {}).get('name')
        if name:
            names[name] = t
    for n in SHELL_NAMES:
        if n in names:
            return n, names[n]
    return None, None


def tool_args(name, tool, command):
    schema = tool.get('input_schema') or tool.get('parameters') or (tool.get('function') or {}).get('parameters') or {}
    props = schema.get('properties', {})
    if 'cmd' in props:
        args = {'cmd': command}
    elif 'command' in props and (props['command'].get('type') == 'array'):
        args = {'command': ['bash', '-lc', command]}
    else:
        args = {'command': command}
    if 'description' in props:
        args['description'] = 'write slugify and run its test'
    if 'timeout' in props and name in ('terminal',):
        pass
    return args


class H(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, *a):
        pass

    def sse(self, events):
        self.send_response(200)
        self.send_header('Content-Type', 'text/event-stream')
        self.send_header('Cache-Control', 'no-cache')
        self.send_header('Transfer-Encoding', 'chunked')
        self.end_headers()
        for name, obj in events:
            payload = f'event: {name}\ndata: {json.dumps(obj)}\n\n'.encode()
            self.wfile.write(f'{len(payload):x}\r\n'.encode() + payload + b'\r\n')
            self.wfile.flush()
            time.sleep(0.03)
        self.wfile.write(b'0\r\n\r\n')
        self.close_connection = True

    def json_reply(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header('Content-Type', 'application/json')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        path = self.path.partition('?')[0]
        n = int(self.headers.get('Content-Length', '0'))
        data = json.loads(self.rfile.read(n) or b'{}')
        headers = {k.lower(): ('<present>' if k.lower() in ('authorization', 'x-api-key', 'cookie') else v) for k, v in self.headers.items()}
        entry = {'path': self.path, 'headers': headers, 'model': data.get('model'), 'max_tokens': data.get('max_tokens'),
                 'max_output_tokens': data.get('max_output_tokens'), 'thinking': data.get('thinking'), 'output_config': data.get('output_config'),
                 'reasoning': data.get('reasoning'), 'sampling': {k: data[k] for k in ('temperature', 'top_p', 'top_k') if k in data},
                 'stream': data.get('stream'), 'n_tools': len(data.get('tools') or [])}
        with LOCK, open(ARGS.log, 'a') as f:
            f.write(json.dumps(entry) + '\n')
        if ARGS.dump:
            with LOCK, open(ARGS.dump, 'a') as f:
                f.write(json.dumps({'path': self.path, 'body': data}) + '\n')
        if path == '/v1/messages/count_tokens':
            return self.json_reply({'input_tokens': 42})
        if path == '/v1/messages':
            return self.handle_messages(data)
        if path == '/v1/responses':
            return self.handle_responses(data)
        self.json_reply({'error': {'type': 'not_found_error', 'message': path}}, 404)

    def handle_messages(self, data):
        model = data.get('model')
        has_result = any(isinstance(m.get('content'), list) and any(b.get('type') == 'tool_result' for b in m['content']) for m in data.get('messages', []) if m.get('role') == 'user')
        name, tool = pick_tool(data.get('tools'))
        mid = 'msg_' + uuid.uuid4().hex[:12]
        usage = {'input_tokens': 120, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 30, 'output_tokens': 1}
        if has_result or not tool:
            blocks = [{'type': 'text', 'text': 'Done. slugify implemented and tests pass.'}]
            stop = 'end_turn'
        else:
            blocks = [{'type': 'text', 'text': 'Writing the solution and running the test.'},
                      {'type': 'tool_use', 'id': 'toolu_' + uuid.uuid4().hex[:12], 'name': name, 'input': tool_args(name, tool, command_for(data))}]
            stop = 'tool_use'
        if not data.get('stream'):
            return self.json_reply({'id': mid, 'type': 'message', 'role': 'assistant', 'model': model, 'content': blocks,
                                    'stop_reason': stop, 'stop_sequence': None, 'usage': dict(usage, output_tokens=25)})
        ev = [('message_start', {'type': 'message_start', 'message': {'id': mid, 'type': 'message', 'role': 'assistant', 'model': model, 'content': [], 'stop_reason': None, 'stop_sequence': None, 'usage': usage}})]
        for i, b in enumerate(blocks):
            if b['type'] == 'text':
                ev.append(('content_block_start', {'type': 'content_block_start', 'index': i, 'content_block': {'type': 'text', 'text': ''}}))
                half = len(b['text']) // 2
                for part in (b['text'][:half], b['text'][half:]):
                    ev.append(('content_block_delta', {'type': 'content_block_delta', 'index': i, 'delta': {'type': 'text_delta', 'text': part}}))
            else:
                ev.append(('content_block_start', {'type': 'content_block_start', 'index': i, 'content_block': {'type': 'tool_use', 'id': b['id'], 'name': b['name'], 'input': {}}}))
                raw = json.dumps(b['input'])
                for j in range(0, len(raw), 40):
                    ev.append(('content_block_delta', {'type': 'content_block_delta', 'index': i, 'delta': {'type': 'input_json_delta', 'partial_json': raw[j:j + 40]}}))
            ev.append(('content_block_stop', {'type': 'content_block_stop', 'index': i}))
        ev.append(('message_delta', {'type': 'message_delta', 'delta': {'stop_reason': stop, 'stop_sequence': None}, 'usage': {'output_tokens': 25}}))
        ev.append(('message_stop', {'type': 'message_stop'}))
        self.sse(ev)

    def handle_responses(self, data):
        model = data.get('model')
        inp = data.get('input')
        items = inp if isinstance(inp, list) else []
        has_result = any(isinstance(i, dict) and i.get('type') in ('function_call_output', 'custom_tool_call_output', 'local_shell_call_output') for i in items)
        name, tool = pick_tool(data.get('tools'))
        # Codex >= 0.165 code mode: tools arrive as an additional_tools input item and a custom JavaScript `exec` tool orchestrates them.
        code_exec = any(isinstance(i, dict) and i.get('type') == 'additional_tools' and any(t.get('name') == 'exec' and t.get('type') == 'custom' for ns in i.get('tools', []) for t in ns.get('tools', [])) for i in items)
        rid = 'resp_' + uuid.uuid4().hex[:12]
        usage = {'input_tokens': 150, 'input_tokens_details': {'cached_tokens': 40}, 'output_tokens': 30, 'output_tokens_details': {'reasoning_tokens': 5}, 'total_tokens': 180}
        if code_exec and not has_result:
            js = 'const r = await tools.exec_command({cmd: ' + json.dumps(command_for(data)) + '}); text(r.output);'
            out = [{'id': 'ctc_1', 'type': 'custom_tool_call', 'status': 'completed', 'call_id': 'call_' + uuid.uuid4().hex[:10], 'name': 'exec', 'input': js}]
        elif has_result or not tool:
            out = [{'id': 'msg_1', 'type': 'message', 'role': 'assistant', 'status': 'completed', 'content': [{'type': 'output_text', 'text': 'Done. slugify implemented and tests pass.', 'annotations': []}]}]
        else:
            out = [{'id': 'fc_1', 'type': 'function_call', 'status': 'completed', 'call_id': 'call_' + uuid.uuid4().hex[:10], 'name': name, 'arguments': json.dumps(tool_args(name, tool, command_for(data)))}]
        final = {'id': rid, 'object': 'response', 'created_at': int(time.time()), 'status': 'completed', 'model': model, 'output': out, 'usage': usage}
        if not data.get('stream'):
            return self.json_reply(final)
        ev = [('response.created', {'type': 'response.created', 'response': dict(final, status='in_progress', output=[], usage=None)}),
              ('response.in_progress', {'type': 'response.in_progress', 'response': dict(final, status='in_progress', output=[], usage=None)})]
        for i, item in enumerate(out):
            extra = {'arguments': ''} if item['type'] == 'function_call' else {'input': ''} if item['type'] == 'custom_tool_call' else {'content': []}
            ev.append(('response.output_item.added', {'type': 'response.output_item.added', 'output_index': i, 'item': dict(item, status='in_progress', **extra)}))
            if item['type'] == 'custom_tool_call':
                ev.append(('response.custom_tool_call_input.delta', {'type': 'response.custom_tool_call_input.delta', 'item_id': item['id'], 'output_index': i, 'delta': item['input']}))
                ev.append(('response.custom_tool_call_input.done', {'type': 'response.custom_tool_call_input.done', 'item_id': item['id'], 'output_index': i, 'input': item['input']}))
            elif item['type'] == 'function_call':
                ev.append(('response.function_call_arguments.delta', {'type': 'response.function_call_arguments.delta', 'item_id': item['id'], 'output_index': i, 'delta': item['arguments']}))
                ev.append(('response.function_call_arguments.done', {'type': 'response.function_call_arguments.done', 'item_id': item['id'], 'output_index': i, 'arguments': item['arguments']}))
            else:
                part = item['content'][0]
                ev.append(('response.content_part.added', {'type': 'response.content_part.added', 'item_id': item['id'], 'output_index': i, 'content_index': 0, 'part': dict(part, text='')}))
                ev.append(('response.output_text.delta', {'type': 'response.output_text.delta', 'item_id': item['id'], 'output_index': i, 'content_index': 0, 'delta': part['text']}))
                ev.append(('response.output_text.done', {'type': 'response.output_text.done', 'item_id': item['id'], 'output_index': i, 'content_index': 0, 'text': part['text']}))
                ev.append(('response.content_part.done', {'type': 'response.content_part.done', 'item_id': item['id'], 'output_index': i, 'content_index': 0, 'part': part}))
            ev.append(('response.output_item.done', {'type': 'response.output_item.done', 'output_index': i, 'item': item}))
        ev.append(('response.completed', {'type': 'response.completed', 'response': final}))
        self.sse(ev)


class Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64


if __name__ == '__main__':
    ap = argparse.ArgumentParser()
    ap.add_argument('--port', type=int, default=8791)
    ap.add_argument('--log', required=True)
    ap.add_argument('--dump', default='', help='also write full request bodies here (debugging only)')
    ARGS = ap.parse_args()
    print(f'fake upstream on 127.0.0.1:{ARGS.port}', flush=True)
    Server(('127.0.0.1', ARGS.port), H).serve_forever()
