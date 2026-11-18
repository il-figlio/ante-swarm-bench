#!/bin/bash
# One minimal Anthropic Messages request through the v3 relay (the real code path, same forced thinking/effort mapping),
# to settle (1) that the model gateway serves the configured Claude model id and (2) that it accepts adaptive thinking + output_config.effort.
# Exactly one paid request, max_tokens 16 (relay override for the probe only). Run at gate time, not before.
#   CLAUDE_MODEL=anthropic/claude-sonnet-5-5 ./probe-claude.sh     # retry with another spelling if the first says model not found
# Env: CLAUDE_MODEL (default: protocol.json cohorts.claude.models[0]), ROOT (default /var/lib/ante-bench), PROBE_MAX_TOKENS (16),
#      PROBE_UPSTREAM (TEST ONLY: fake upstream URL), PROBE_PORT (8766).
set -u
HERE=$(cd "$(dirname "$0")" && pwd)
ROOT=${ROOT:-/var/lib/ante-bench}
OUT=$ROOT/probe/claude-$(date -u +%Y%m%dT%H%M%SZ)
mkdir -p "$OUT/runs/r000" || { echo "cannot create $OUT"; exit 2; }
HERE="$HERE" OUT="$OUT" python3 - <<'PY'
import json, os, sys, urllib.request, urllib.error
sys.path.insert(0, os.environ['HERE'])
from pathlib import Path
from model_gate import Gate
proto = json.load(open(Path(os.environ['HERE']) / 'protocol.json'))['cohorts']['claude']
model = os.environ.get('CLAUDE_MODEL') or proto['models'][0]
port = int(os.environ.get('PROBE_PORT', '8766'))
mt = int(os.environ.get('PROBE_MAX_TOKENS', '16'))
out = Path(os.environ['OUT'])
gate = Gate(out / 'runs' / 'r000', 'r000', model, 'medium', upstream=os.environ.get('PROBE_UPSTREAM') or os.environ.get('BENCH_MODEL_UPSTREAM') or 'https://model-gateway.invalid', port=port, max_tokens=mt)
body = {'model': model, 'max_tokens': 4096, 'messages': [{'role': 'user', 'content': 'Reply with the single word: ok'}]}
req = urllib.request.Request(f'http://127.0.0.1:{port}/r000/probe/v1/messages', data=json.dumps(body).encode(), headers={'Content-Type': 'application/json'})
status, payload = None, b''
try:
    with urllib.request.urlopen(req, timeout=120) as r:
        status, payload = r.status, r.read()
except urllib.error.HTTPError as e:
    status, payload = e.code, e.read()
gate.close()
row = [json.loads(l) for l in (out / 'runs' / 'r000' / 'request-audit.jsonl').read_text().splitlines()][-1]
print('probe model id sent      :', model)
print('relay forced             :', {'max_tokens': row.get('max_tokens'), 'thinking': row.get('thinking'), 'effort': row.get('effort')})
print('HTTP status              :', status)
try:
    data = json.loads(payload)
except ValueError:
    data = {}
if status == 200:
    text = ''.join(b.get('text', '') for b in data.get('content', []) if b.get('type') == 'text')
    print('model echoed by gateway  :', data.get('model'))
    print('stop_reason              :', data.get('stop_reason'))
    print('usage                    :', json.dumps(data.get('usage')))
    print('text                     :', repr(text[:80]))
    print('block types              :', [b.get('type') for b in data.get('content', [])])
    ok = data.get('model') is not None
    print('RESULT: OK' if ok else 'RESULT: 200 without a model echo, inspect', '(record the echoed id; if it differs from the id sent, set cohorts.claude.models in protocol.json to the id the gateway accepts)')
    sys.exit(0 if ok else 1)
print('error body               :', json.dumps(data.get('error', data))[:500] if data else payload[:300])
print('RESULT: FAILED. Not-found/invalid model: rerun with CLAUDE_MODEL=<other spelling>, e.g. anthropic/claude-sonnet-5-5. 400 mentioning thinking/effort: the gateway rejects the medium mapping, stop and report.')
sys.exit(1)
PY
rc=$?
echo "audit: $OUT/runs/r000/request-audit.jsonl"
exit $rc
