#!/bin/bash
set -euo pipefail
test "$(cat README.txt)" = 'Workspace for the startup task.' && echo PASS
