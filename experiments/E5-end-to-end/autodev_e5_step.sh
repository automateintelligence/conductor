#!/usr/bin/env bash
# `/autodev` (E5 stub) — ONE fire: reconcile-first SPEC-DONE gate, else implement the
# next unit, commit, write a handoff. Composes E4 (the real done-gate) + the §6 recipe.
# Prints "E5 STATE=GREEN" (loop should self-stop) or "E5 STATE=IMPLEMENTED" (continue).
set -uo pipefail
ROOT=/home/danie906/.claude/conductor
cd "$ROOT"
R=experiments/E5-end-to-end/run
mkdir -p "$R"
GIT=(git -c user.name="Jeffrey A. Daniels" -c user.email="jeff@automateintelligence.ai")

# SPEC-DONE GATE: run the real E4 assertion runner over the spec's assertions.
if ./bin/conductor assert run > "$R/gate.out" 2>&1; then
  echo "E5 STATE=GREEN ($(tail -1 "$R/gate.out"))"
  exit 0
fi

# RED -> pick the next eligible unit: implement the failing 'feature-shipped' assertion.
mkdir -p assertions/feature
printf 'def feature():\n    return "SHIPPED"\n' > assertions/feature/feature.py
"${GIT[@]}" add assertions/feature/feature.py
"${GIT[@]}" commit -q \
  -m "E5: implement feature() -> SHIPPED" \
  -m "assertions/feature/feature.py — close the 'feature-shipped' assertion (autodev fire)" \
  -m "Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>" || true

# Handoff every iteration (design §4).
printf 'E5 handoff: implemented feature() -> SHIPPED; next fire re-checks the done-gate.\n' > "$R/handoff.md"
echo "E5 STATE=IMPLEMENTED (gate was red; created feature.py + committed; re-fire to re-check the gate)"
exit 0
