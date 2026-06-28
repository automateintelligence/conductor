#!/usr/bin/env bash
# E0 stub `/autodev`: one fire = increment counter, flip the machine gate after
# THRESHOLD fires, then evaluate the gate. Exit code mirrors the §5.2 done-gate:
#   exit 0 = gate GREEN (done -> loop should self-stop)
#   exit 1 = gate RED   (not done -> loop should keep firing)
set -euo pipefail
cd "$(dirname "$0")"
mkdir -p run
C=run/counter.txt
G=run/gate.txt
prev=0; [ -f "$C" ] && prev=$(<"$C")
n=$((prev + 1))
printf '%s\n' "$n" > "$C"

# The "spec" becomes done after THRESHOLD fires (this is what flips the gate).
THRESHOLD="${E0_THRESHOLD:-3}"
if [ "$n" -ge "$THRESHOLD" ]; then printf 'DONE\n' > "$G"; fi

# The machine gate: a one-line assertion (grep DONE), exit-code semantics.
if grep -q DONE "$G" 2>/dev/null; then
  printf 'E0-LOOP-SELFSTOP fire=%s gate=GREEN\n' "$n"
  exit 0
else
  printf 'E0-LOOP-SELFSTOP fire=%s gate=RED\n' "$n"
  exit 1
fi
