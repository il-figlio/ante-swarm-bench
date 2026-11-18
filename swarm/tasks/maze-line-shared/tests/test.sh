#!/bin/bash
set -euo pipefail
out=$(python3 ./maze verdict)
echo "$out"
python3 -c 'import json,sys; sys.exit(0 if json.loads(sys.argv[1]).get("passed") is True else 1)' "$out"
