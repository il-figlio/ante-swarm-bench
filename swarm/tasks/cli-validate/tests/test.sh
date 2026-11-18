#!/bin/bash
set -uo pipefail
out=$(python3 temp.py 21.5) || { echo "valid input failed"; exit 1; }
[ "$out" = "70.7" ] || { echo "wrong conversion: $out"; exit 1; }
for bad in "" "abc"; do
  if [ -z "$bad" ]; then err=$(python3 temp.py 2>&1 >/dev/null); code=$?; else err=$(python3 temp.py "$bad" 2>&1 >/dev/null); code=$?; fi
  [ $code -eq 2 ] || { echo "exit $code for '$bad'"; exit 1; }
  [ "$err" = "error: expected a number" ] || { echo "stderr '$err' for '$bad'"; exit 1; }
done
echo PASS
