#!/bin/bash
set -euo pipefail
[ "$(cat status.txt)" = "ready" ] || { echo "status.txt is not ready"; exit 1; }
echo PASS
