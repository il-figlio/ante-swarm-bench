#!/bin/bash
set -euo pipefail
expected=$'region,total\neast,100\nnorth,12\nwest,150'
actual=$(cat totals.csv | tr -d '\r')
[ "$actual" = "$expected" ] || { echo "totals.csv mismatch"; echo "$actual"; exit 1; }
rm totals.csv
python3 report.py
[ "$(cat totals.csv | tr -d '\r')" = "$expected" ] || { echo "report.py does not regenerate totals.csv"; exit 1; }
echo PASS
