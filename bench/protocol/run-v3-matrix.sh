#!/usr/bin/env bash
# shared-v3 matrix, serial, detached-unit ready like shared-v2's. Usage: run-v3-matrix.sh claude|codex
#   claude: 4 harnesses x 1 model x 2 levels x 3 repeats = 24 cells    codex: 1 harness x 2 models x 2 levels x 3 repeats = 12 cells
# Each cell = 32 attempts (4 toolbox tasks x 8) at x1 or x8 instances. Start only after a passing gate-v3.sh.
# Detached launch (as in shared-v2, runs as user ante-bench):
#   sudo systemd-run --unit ante-bench-shared-v3-claude --uid ante-bench /opt/ante-bench/campaign-20261031/run-v3-matrix.sh claude
# Env overrides (tests only): C (campaign dir), ROOT, WORKER_EXTRA (extra worker args), MIN_FREE_GIB (default 6), MATRIX_SUFFIX (e.g. -test with --test-upstream in WORKER_EXTRA).
COHORT=${1:?usage: run-v3-matrix.sh claude|codex}
C=${C:-/opt/ante-bench/campaign-20261031}; ROOT=${ROOT:-/var/lib/ante-bench}; LOG=$C/progress-v3-$COHORT.log; MIN_FREE_GIB=${MIN_FREE_GIB:-6}
note(){ echo "$(date -u +%FT%TZ) $*" >> $LOG; }
# rotation: keep the previous log of this cohort, never overwrite it
if [[ -s $LOG ]]; then mv "$LOG" "$LOG.$(date -u +%Y%m%dT%H%M%SZ)"; fi
HARNESSES=(); while IFS= read -r x; do HARNESSES+=("$x"); done < <(python3 -c "import json,sys;print('\n'.join(json.load(open('$C/protocol.json'))['cohorts']['$COHORT']['harnesses']))")
MODELS=(); while IFS= read -r x; do MODELS+=("$x"); done < <(python3 -c "import json,sys;print('\n'.join(json.load(open('$C/protocol.json'))['cohorts']['$COHORT']['models']))")
MATRIX=$(python3 -c "import json;print(json.load(open('$C/protocol.json'))['cohorts']['$COHORT']['matrix_id'])")${MATRIX_SUFFIX:-}
(( ${#HARNESSES[@]} > 0 && ${#MODELS[@]} > 0 )) || { echo "no harnesses/models for cohort $COHORT"; exit 2; }
N=${#HARNESSES[@]}; CELLS=$(( 3 * ${#MODELS[@]} * 2 * N ))
next_id(){ local m=209 n; for d in $ROOT/runs/r*; do n=${d##*/r}; [[ $n =~ ^[0-9]+$ ]] && (( n > m )) && m=$n; done; echo r$((m+1)); }
note "START $MATRIX $CELLS cells harnesses=${HARNESSES[*]} models=${MODELS[*]}"
for repeat in 1 2 3; do
 for model in "${MODELS[@]}"; do
  for level in 1 8; do
   for (( i=0; i<N; i++ )); do
    h=${HARNESSES[$(( (i + repeat - 1) % N ))]}
    free=$(python3 -c "import shutil,sys;print(shutil.disk_usage(sys.argv[1]).free)" $ROOT)
    if (( free < MIN_FREE_GIB*1024*1024*1024 )); then note "STOP low disk: $free bytes free before repeat=$repeat $model x$level $h"; exit 3; fi
    id=$(next_id)
    python3 $C/worker.py --run-id $id --protocol shared-v3-$COHORT --matrix-id $MATRIX --audit --harness $h --instances $level --task-count 32 --timeout 600 --model $model --repeat $repeat --root $ROOT --code $C $WORKER_EXTRA > /tmp/$id.worker.out 2>&1
    rc=$?
    res=$(python3 - "$ROOT/runs/$id/receipts.json" <<'PY'
import json,sys
try:
    r=json.load(open(sys.argv[1]));print(f"{r['passed']}/{r['attempted']} {r['total_secs']}")
except Exception as e:print("no-receipt -")
PY
)
    note "$id model=$model harness=$h level=x$level repeat=$repeat passed/attempted=${res% *} total_secs=${res#* } exit=$rc"
    (( rc != 0 )) && note "$id FAILED rc=$rc: $(tail -c 300 /tmp/$id.worker.out | tr '\n' ' ')"
    rm -f /tmp/$id.worker.out
   done
  done
 done
done
note "DONE $MATRIX"
