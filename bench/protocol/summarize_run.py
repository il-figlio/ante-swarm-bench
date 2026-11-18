#!/usr/bin/env python3
"""Print a one-line receipt summary plus relay audit facts for a run: summarize_run.py <root> <run_id>. Exit 0 always; gate scripts decide."""
import json, sys
from collections import Counter
root, rid = sys.argv[1:3]
try:
    r = json.load(open(f'{root}/runs/{rid}/receipts.json'))
except (OSError, ValueError):
    print(f'{rid} NO-RECEIPT'); sys.exit(0)
rows = [a for t in r['results'] for a in t.get('request_audit') or []]
st = Counter(a.get('status') for a in rows)
models = sorted({str(a.get('response_model')) for a in rows})
thinking = sorted({json.dumps(a.get('thinking')) for a in rows if a.get('route') == 'messages'})
eff = sorted({str(a.get('effort')) for a in rows})
print(f"{rid} {r['harness']} {r['model']} x{r['cells']} passed={r['passed']}/{r['attempted']} secs={r['total_secs']} model_verified={r['model_verified']} "
      f"requests={len(rows)} status={dict(st)} response_models={models} effort={eff} thinking={thinking} tokens={r.get('tokens')} version={r['harness_version']!r}")
bad = [t['task'] + ':' + t['failure'] for t in r['results'] if not t['passed']]
if bad:
    print('  failures:', ' '.join(bad))
errs = sorted({json.dumps(a.get('error')) for a in rows if a.get('error')})
if errs:
    print('  upstream errors:', errs[:3])
