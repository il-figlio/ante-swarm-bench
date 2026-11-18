#!/bin/bash
set -euo pipefail
expected='{"api": 2, "billing": 3, "search": 1}'
python3 - "$expected" <<'PY'
import json, sys
want = json.loads(sys.argv[1])
got = json.load(open("errors.json"))
assert got == want and list(got) == sorted(got), f"errors.json is {got}"
PY
rm errors.json && python3 triage.py && python3 -c "import json; assert json.load(open('errors.json')) == $expected" && echo PASS
