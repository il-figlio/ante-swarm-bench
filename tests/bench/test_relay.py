#!/usr/bin/env python3
"""Relay checks against the fake upstream. No network beyond loopback, no model calls.  Run: python3 tests/test_relay.py"""
import http.client, json, sys, tempfile, threading, time, unittest
from pathlib import Path
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "bench" / "protocol")); sys.path.insert(0, str(HERE))
import fake_upstream as fu
from model_gate import Gate

UP, GATE, RUN = 8792, 8793, 'r999'
MODEL_C, MODEL_G = 'claude-sonnet-5-5', 'openai/gpt-6.1-sol'


def post(path, body, headers=None, port=GATE):
    c = http.client.HTTPConnection('127.0.0.1', port, timeout=20)
    c.request('POST', path, json.dumps(body), dict({'Content-Type': 'application/json'}, **(headers or {})))
    r = c.getresponse()
    return r, c


class RelayTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix='relay-test-'))
        fu.ARGS = type('A', (), {'log': str(cls.tmp / 'up.jsonl'), 'dump': ''})()
        cls.up = fu.Server(('127.0.0.1', UP), fu.H)
        threading.Thread(target=cls.up.serve_forever, daemon=True).start()
        (cls.tmp / 'runs' / RUN).mkdir(parents=True)
        cls.run_dir = cls.tmp / 'runs' / RUN
        cls.gate = Gate(cls.run_dir, RUN, MODEL_C, 'medium', upstream=f'http://127.0.0.1:{UP}', port=GATE)
        cls.gate_g = Gate(cls.run_dir, RUN, MODEL_G, 'medium', upstream=f'http://127.0.0.1:{UP}', port=GATE + 1)

    @classmethod
    def tearDownClass(cls):
        cls.gate.close(); cls.gate_g.close(); cls.up.shutdown()

    def upstream_rows(self):
        p = self.tmp / 'up.jsonl'
        return [json.loads(l) for l in p.read_text().splitlines()] if p.exists() else []

    def test_1_wrong_model_is_422_and_never_forwarded(self):
        n = len(self.upstream_rows())
        r, _ = post(f'/{RUN}/t01-01/v1/messages', {'model': 'claude-haiku-5-5', 'max_tokens': 5, 'messages': []})
        self.assertEqual(r.status, 422)
        r, _ = post(f'/{RUN}/t01-01/v1/responses', {'model': 'openai/gpt-6.1-terra', 'input': 'x'}, port=GATE + 1)
        self.assertEqual(r.status, 422)
        r, _ = post(f'/{RUN}/t01-01/v1/messages/count_tokens', {'model': 'other', 'messages': []})
        self.assertEqual(r.status, 422)
        self.assertEqual(len(self.upstream_rows()), n)
        self.assertTrue(any(x['status'] == 422 and x['accepted'] is False for x in self.gate.rows))

    def test_2_messages_forced_settings_and_header_hygiene(self):
        body = {'model': MODEL_C, 'max_tokens': 99999, 'temperature': 0.2, 'top_p': 0.9, 'top_k': 5, 'stream': False,
                'thinking': {'type': 'enabled', 'budget_tokens': 8192}, 'output_config': {'effort': 'high', 'format': {'type': 'json_schema'}},
                'messages': [{'role': 'user', 'content': 'hi'}]}
        r, _ = post(f'/{RUN}/t02-01/v1/messages?beta=true', body, {'x-api-key': 'sk-secret-client', 'Authorization': 'Bearer secret-client', 'anthropic-beta': 'interleaved-thinking-2025-05-14'})
        data = json.loads(r.read())
        self.assertEqual(r.status, 200); self.assertEqual(data['model'], MODEL_C)
        up = self.upstream_rows()[-1]
        self.assertEqual(up['path'], '/v1/messages?beta=true')
        self.assertEqual(up['max_tokens'], 16384)
        self.assertEqual(up['thinking'], {'type': 'adaptive'})
        self.assertEqual(up['output_config'], {'effort': 'medium', 'format': {'type': 'json_schema'}})
        self.assertEqual(up['sampling'], {})
        self.assertNotIn('x-api-key', up['headers']); self.assertNotIn('authorization', up['headers'])
        self.assertEqual(up['headers']['anthropic-version'], '2023-06-01')
        self.assertEqual(up['headers']['anthropic-beta'], 'interleaved-thinking-2025-05-14')
        row = [x for x in self.gate.rows if x['task'] == 't02-01'][-1]
        self.assertEqual((row['requested_max_tokens'], row['max_tokens']), (99999, 16384))
        self.assertEqual(row['requested_thinking'], {'type': 'enabled', 'budget_tokens': 8192}); self.assertEqual(row['thinking'], {'type': 'adaptive'})
        self.assertEqual((row['requested_effort'], row['effort']), ('high', 'medium'))
        self.assertEqual(row['stripped_sampling'], ['temperature', 'top_k', 'top_p'])
        self.assertEqual(row['usage']['output_tokens'], 25); self.assertEqual(row['response_model'], MODEL_C); self.assertEqual(row['status'], 200)

    def test_3_client_version_kept_and_sse_streams_through(self):
        body = {'model': MODEL_C, 'max_tokens': 10, 'stream': True, 'messages': [{'role': 'user', 'content': 'hi'}]}
        t0 = time.monotonic()
        r, c = post(f'/{RUN}/t03-01/v1/messages', body, {'anthropic-version': '2023-06-01'})
        self.assertTrue(r.getheader('Content-Type').startswith('text/event-stream'))
        first = r.fp.readline(); t_first = time.monotonic() - t0
        rest = r.read(); t_all = time.monotonic() - t0
        self.assertTrue(first.startswith(b'event: message_start'))
        self.assertLess(t_first, t_all - 0.15, 'first event must arrive well before the stream ends (no buffering)')
        self.assertIn(b'message_stop', rest)
        row = [x for x in self.gate.rows if x['task'] == 't03-01'][-1]
        self.assertEqual(row['usage'], {'input_tokens': 120, 'cache_creation_input_tokens': 0, 'cache_read_input_tokens': 30, 'output_tokens': 25})
        self.assertEqual(row['stop_reason'], 'end_turn'); self.assertEqual(row['response_model'], MODEL_C)

    def test_4_responses_forced_effort_and_cap(self):
        body = {'model': MODEL_G, 'input': 'x', 'stream': True, 'reasoning': {'effort': 'xhigh'}, 'max_output_tokens': 999999}
        r, _ = post(f'/{RUN}/t04-01/v1/responses', body, {'Authorization': 'Bearer client'}, port=GATE + 1)
        raw = r.read()
        self.assertEqual(r.status, 200); self.assertIn(b'response.completed', raw)
        up = self.upstream_rows()[-1]
        self.assertEqual((up['reasoning'], up['max_output_tokens']), ({'effort': 'medium'}, 16384))
        self.assertNotIn('authorization', up['headers'])
        row = [x for x in self.gate_g.rows if x['task'] == 't04-01'][-1]
        self.assertEqual((row['requested_effort'], row['effort']), ('xhigh', 'medium')); self.assertEqual(row['usage']['input_tokens'], 150)

    def test_5_count_tokens_forwarded_and_unknown_routes_404(self):
        r, _ = post(f'/{RUN}/t05-01/v1/messages/count_tokens', {'model': MODEL_C, 'messages': [{'role': 'user', 'content': 'hi'}]})
        self.assertEqual(r.status, 200); self.assertEqual(json.loads(r.read())['input_tokens'], 42)
        up = self.upstream_rows()[-1]; self.assertEqual(up['path'], '/v1/messages/count_tokens'); self.assertIsNone(up['max_tokens'])
        self.assertEqual([x for x in self.gate.rows if x['task'] == 't05-01'][-1]['input_tokens_counted'], 42)
        for path in (f'/{RUN}/t05-01/v1/models', f'/other/t05-01/v1/messages', f'/{RUN}/T05/v1/messages'):
            r, _ = post(path, {'model': MODEL_C}); self.assertEqual(r.status, 404, path)

    def test_6_audit_file_written_without_prompts(self):
        lines = (self.run_dir / 'request-audit.jsonl').read_text().splitlines()
        self.assertGreater(len(lines), 5)
        self.assertNotIn('"hi"', ''.join(lines)); self.assertNotIn('sk-secret-client', ''.join(lines))


if __name__ == '__main__':
    unittest.main(verbosity=2)
