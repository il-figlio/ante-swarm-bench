#!/bin/bash
set -euo pipefail
python3 -c "import sys; got = open('ticks.txt').read().split(); sys.exit(0 if got == [str(n) for n in range(1, 50 + 1)] else 'ticks.txt is wrong')" && echo PASS
