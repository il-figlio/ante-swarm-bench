#!/bin/bash
set -euo pipefail
python3 - <<'PY'
import json
want = {
    "api": {"name": "api", "port": 8080, "timeout_ms": 5000, "retries": 3},
    "billing": {"name": "billing", "port": 8081, "timeout_ms": 12000, "currency": "USD"},
    "search": {"name": "search", "port": 8082, "timeout_ms": 2500, "replicas": 2},
}
for name, expected in want.items():
    got = json.load(open(f"services/{name}.json"))
    assert got == expected, f"{name}.json is {got}"
PY
echo PASS
