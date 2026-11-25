#!/usr/bin/env bash
# Gate: 4 toolbox tasks at x1 for every harness x model pair of both cohorts (Claude: 4 pairs, Codex: 2 pairs = 6 runs, 24 attempts).
# Paid model calls. Run only after install-v3.sh and a successful probe-claude.sh.
# Usage: gate-v3.sh [claude|codex|both]   (default both). Exit 0 only if every run passed 4/4 with model_verified.
# Env overrides (tests only): C, ROOT, WORKER_EXTRA, MATRIX_SUFFIX (e.g. -test with --test-upstream in WORKER_EXTRA).
WHICH=${1:-both}
case "$WHICH" in
 claude|codex|both) ;;
 *) echo "usage: gate-v3.sh [claude|codex|both]" >&2; exit 2 ;;
esac
C=${C:-/opt/ante-bench/campaign-20261031}; ROOT=${ROOT:-/var/lib/ante-bench}
next_id(){ local m=209 n; for d in $ROOT/runs/r*; do n=${d##*/r}; [[ $n =~ ^[0-9]+$ ]] && (( n > m )) && m=$n; done; echo r$((m+1)); }
fail=0
ran=0
for cohort in claude codex; do
 [[ $WHICH == both || $WHICH == $cohort ]] || continue
 HARNESSES=(); while IFS= read -r x; do HARNESSES+=("$x"); done < <(python3 -c "import json;print('\n'.join(json.load(open('$C/protocol.json'))['cohorts']['$cohort']['harnesses']))")
 MODELS=(); while IFS= read -r x; do MODELS+=("$x"); done < <(python3 -c "import json;print('\n'.join(json.load(open('$C/protocol.json'))['cohorts']['$cohort']['models']))")
 GATE=$(python3 -c "import json;print(json.load(open('$C/protocol.json'))['cohorts']['$cohort']['gate_id'])")${MATRIX_SUFFIX:-}
 for model in "${MODELS[@]}"; do
  for h in "${HARNESSES[@]}"; do
   ran=$((ran + 1))
   free=$(python3 -c "import shutil,sys;print(shutil.disk_usage(sys.argv[1]).free)" $ROOT)
   (( free < 6*1024*1024*1024 )) && { echo "STOP low disk: $free bytes free"; exit 3; }
   id=$(next_id)
   python3 $C/worker.py --run-id $id --protocol shared-v3-$cohort --matrix-id $GATE --audit --harness $h --instances 1 --task-count 4 --timeout 600 --model $model --repeat 1 --root $ROOT --code $C $WORKER_EXTRA > /tmp/$id.gate.out 2>&1
   rc=$?
   python3 $C/summarize_run.py $ROOT $id
   ok=$(python3 - $ROOT/runs/$id/receipts.json <<'PY'
import json,sys
try:
    r=json.load(open(sys.argv[1]));print('yes' if r['passed']==4 and r['attempted']==4 and r['model_verified'] else 'no')
except Exception:print('no')
PY
)
   if (( rc != 0 )) || [[ $ok != yes ]]; then fail=1; echo "  GATE FAIL $id rc=$rc: $(tail -c 300 /tmp/$id.gate.out | tr '\n' ' ')"; fi
   rm -f /tmp/$id.gate.out
  done
 done
done
if (( ran == 0 )); then fail=1; echo "GATE FAILED ($WHICH): zero gate runs executed"; fi
(( fail == 0 )) && echo "GATE PASS ($WHICH)" || echo "GATE FAILED ($WHICH): do not launch the matrix"
exit $fail
