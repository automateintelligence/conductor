#!/usr/bin/env bash
# `/conductor` (E5 stub) — RECONCILE-FIRST, idempotent setup (design amendment B).
# Re-running after a restart/context-loss detects existing state and resumes from the
# first incomplete step instead of redoing or erroring.
set -uo pipefail
ROOT=/home/danie906/.claude/conductor
cd "$ROOT"
R=experiments/E5-end-to-end/run
mkdir -p "$R"

# Idempotent step: skip if the probe artifact already exists.
step () {  # step <name> <probe-file> <command...>
  local name="$1" probe="$2"; shift 2
  if [ -e "$probe" ]; then
    echo "[conductor] $name: already done -> skip (reconcile)"
  else
    echo "[conductor] $name: doing"
    "$@"
  fi
}

# 1. PRECONDITION: the spec's executable assertion exists in the manifest.
if grep -q 'feature-shipped' assertions/manifest.yaml; then
  echo "[conductor] precondition: assertion 'feature-shipped' present -> ok"
else
  echo "[conductor] precondition FAIL: assertion missing"; exit 1
fi

# 2. Record the goal (done-condition).
step "record-goal" "$R/goal.md" bash -c \
  'printf "GOAL: spec done when \`bin/conductor assert run\` exits 0 (all assertions green).\n" > '"$R"'/goal.md'

# 3. Author the plan index.
step "author-plan" "$R/plan.md" bash -c \
  'printf "# E5 micro-plan\n- Phase 1: implement feature() -> SHIPPED (assertion: feature-shipped)\n" > '"$R"'/plan.md'

# 4. Record that the driver is started (the cron /loop is registered by the harness).
step "mark-driver" "$R/DRIVER_STARTED" bash -c \
  'printf "driver = /loop /autodev-e5 (cron, every minute)\n" > '"$R"'/DRIVER_STARTED'

echo "[conductor] setup complete; goal/plan/driver recorded in $R"
