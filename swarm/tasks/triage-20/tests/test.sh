#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import json
got = json.load(open('answer.json'))
assert got == {'file': 'node-07.log', 'errors': 49}, f'answer.json is {got}'
print('PASS')
PY
