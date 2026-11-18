#!/bin/bash
set -euo pipefail
test "$(cat note.txt)" = 'hello from agent' && test "$(cat result.txt)" = done && echo PASS
