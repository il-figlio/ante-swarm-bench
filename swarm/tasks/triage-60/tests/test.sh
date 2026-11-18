#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import json
got = json.load(open('answer.json'))
assert got == {'file': 'node-21.log', 'errors': 43}, f'answer.json is {got}'
print('PASS')
PY
