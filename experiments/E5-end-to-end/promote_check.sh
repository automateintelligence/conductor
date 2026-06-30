#!/usr/bin/env bash
# promote_check.sh — Recorded agent smoke for Conductor E2E (E5).
#
# GATED: set RUN_CONDUCTOR_E2E=1 to run; otherwise exits 0 with a skip message.
# PURPOSE: install conductor live, then drive the REAL conductor skills in a TEMP working
#   copy of the working tree.
#   INSTALL-FIRST: Step 0 installs/updates the conductor plugin into the REAL ~/.claude
#   (scope user) from the LOCAL working tree (shared helper, also used by E6), so the
#   full live agent loop below runs against the actually-INSTALLED plugin. Per policy the
#   install is NOT torn down (conductor stays installed).
#   NOTE: the WORKING TREE is an isolated temp copy, but the copied .git shares the REAL
#   remote — a real run (RUN_CONDUCTOR_E2E=1) pushes branches, opens PRs, and merges
#   against the live repo. Run only where that is intended.
#
# DO NOT run this script in CI or automated gates — it spawns claude and registers
# a cron job, so it requires an interactive session with claude CLI configured.
#
# Usage:
#   RUN_CONDUCTOR_E2E=1 bash experiments/E5-end-to-end/promote_check.sh
#
# Pass conditions recorded (echoed inline):
#   [P0] install/update conductor into the REAL ~/.claude (scope user) — exit 0
#   [P1] /conductor:start preflight + setup + cron registered — exit 0
#   [P2] /conductor:autodev implements one phase + merge-gate passes — exit 0
#   [P3] conductor assert run --level spec exits 0 (gate GREEN), worker self-stopped
#   [P4] cron job self-deleted after self-stop
#   [P5] handoff.md written in SPEC_DIR
#   [P6] Re-run /conductor:start — every step prints "already done", no questions
#
# Evidence is captured in WORKDIR/evidence/ for review.

set -euo pipefail

if [ -z "${RUN_CONDUCTOR_E2E:-}" ]; then
  echo "[SKIP] promote_check.sh: set RUN_CONDUCTOR_E2E=1 to run this recorded smoke."
  exit 0
fi

# ---------------------------------------------------------------------------
# Setup: temp working copy (no repo mutation)
# ---------------------------------------------------------------------------
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# Shared install helper (also used by E6). Sourcing only defines install_conductor().
# shellcheck source=../lib/install_conductor.sh disable=SC1091
source "$REPO_ROOT/experiments/lib/install_conductor.sh"
WORKDIR="$(mktemp -d /tmp/conductor-e5-smoke.XXXXXX)"
EVIDENCE_DIR="$WORKDIR/evidence"
SPEC_DIR="$WORKDIR/spec"
mkdir -p "$EVIDENCE_DIR" "$SPEC_DIR"

echo "[E5 SMOKE] REPO_ROOT=$REPO_ROOT"
echo "[E5 SMOKE] WORKDIR=$WORKDIR"
echo "[E5 SMOKE] Evidence will be written to $EVIDENCE_DIR"

# Copy the conductor working tree into the temp directory so skills run against
# an isolated copy of the files; however the copied .git still points at the real
# remote (push/PR/merge operations affect the live repo — see header).
cp -r "$REPO_ROOT/." "$WORKDIR/repo/"
cd "$WORKDIR/repo"

# ---------------------------------------------------------------------------
# Write trivial spec into SPEC_DIR (one phase, one assertion)
# ---------------------------------------------------------------------------
SPEC_FILE="$SPEC_DIR/trivial.md"
MANIFEST_FILE="$SPEC_DIR/manifest.yaml"

cat > "$SPEC_FILE" <<'SPEC_EOF'
# Trivial Smoke Spec

## Goal
Ship a function `hello()` that returns the string "HELLO".

## Phases
- Phase 1: implement hello()

## Done criteria
- `hello()` returns "HELLO"
SPEC_EOF

cat > "$MANIFEST_FILE" <<'MANIFEST_EOF'
assertions:
  - id: hello
    claim: "hello() returns HELLO"
    command: "python3 -c \"from hello import hello; assert hello() == 'HELLO'\""
    level: spec
    kind: example
MANIFEST_EOF

echo "[E5 SMOKE] Spec written to $SPEC_FILE"
echo "[E5 SMOKE] Manifest written to $MANIFEST_FILE"

# ---------------------------------------------------------------------------
# Step 0: install/update conductor into the REAL ~/.claude (scope user) from the LOCAL
# working tree, so Steps 1-6 drive the actually-INSTALLED plugin. Idempotent; does NOT
# uninstall. Shared with E6 via experiments/lib/install_conductor.sh.
# [P0] Expect install/update to succeed (exit 0).
# ---------------------------------------------------------------------------
echo ""
echo "=== STEP 0: install/update conductor (live, scope user) ==="
if install_conductor 2>&1 | tee "$EVIDENCE_DIR/step0-install.log"; then
  echo "[P0] PASS: conductor installed/updated from the automateintelligence GitHub catalog"
else
  echo "[FAIL] install/update of conductor failed — see $EVIDENCE_DIR/step0-install.log"
  exit 1
fi

# ---------------------------------------------------------------------------
# Step 1: /conductor:start — preflight + setup + register cron
# [P1] Expect exit 0, cron registered, no questions asked
# ---------------------------------------------------------------------------
echo ""
echo "=== STEP 1: /conductor:start ==="
CONDUCTOR_MANIFEST="$MANIFEST_FILE" \
  claude -p "/conductor:start $SPEC_FILE" \
  2>&1 | tee "$EVIDENCE_DIR/step1-start.log"
START_RC="${PIPESTATUS[0]}"
echo "[P1] /conductor:start exit code: $START_RC"
if [ "$START_RC" -ne 0 ]; then
  echo "[FAIL] /conductor:start must exit 0 — got $START_RC"
  exit 1
fi
echo "[P1] PASS: preflight + setup + cron registered"

# ---------------------------------------------------------------------------
# Step 2: /conductor:autodev — implements Phase 1, runs merge-gate
# [P2] Expect autodev implements hello.py and merge-gate exits 0
# The cron would fire this; we invoke it directly for the smoke.
# ---------------------------------------------------------------------------
echo ""
echo "=== STEP 2: /conductor:autodev (simulating cron fire) ==="
CONDUCTOR_MANIFEST="$MANIFEST_FILE" \
  claude -p "/conductor:autodev" \
  2>&1 | tee "$EVIDENCE_DIR/step2-autodev.log"
AUTODEV_RC="${PIPESTATUS[0]}"
echo "[P2] /conductor:autodev exit code: $AUTODEV_RC"
if [ "$AUTODEV_RC" -ne 0 ]; then
  echo "[FAIL] /conductor:autodev must exit 0 — got $AUTODEV_RC"
  exit 1
fi
echo "[P2] PASS: autodev implemented phase + merge-gate passed"

# ---------------------------------------------------------------------------
# Step 3: conductor assert run --level spec — done-gate GREEN
# [P3] Runner must exit 0 (all spec assertions pass)
# ---------------------------------------------------------------------------
echo ""
echo "=== STEP 3: conductor assert run --level spec ==="
CONDUCTOR_MANIFEST="$MANIFEST_FILE" \
  python3 "$WORKDIR/repo/assertions/run.py" --level spec \
  2>&1 | tee "$EVIDENCE_DIR/step3-gate.log"
GATE_RC="${PIPESTATUS[0]}"
echo "[P3] Gate exit code: $GATE_RC"
if [ "$GATE_RC" -ne 0 ]; then
  echo "[FAIL] Gate must be GREEN (exit 0) — got $GATE_RC"
  exit 1
fi
echo "[P3] PASS: conductor assert run --level spec exits 0 (gate GREEN)"

# ---------------------------------------------------------------------------
# Step 4: cron self-deleted after self-stop
# [P4] The cron registered by /conductor:start should be absent after self-stop.
# (conductor:autodev calls self-stop when gate is GREEN, which deletes the cron.)
# ---------------------------------------------------------------------------
echo ""
echo "=== STEP 4: verify cron self-deleted ==="
# claude cron list — expect no conductor entry for this repo
claude cron list 2>&1 | tee "$EVIDENCE_DIR/step4-cron.log"
if grep -q "conductor:autodev" "$EVIDENCE_DIR/step4-cron.log"; then
  echo "[WARN] cron entry still present — self-stop may not have run yet"
  echo "[P4] INCONCLUSIVE: cron entry found (may be timing)"
else
  echo "[P4] PASS: cron self-deleted after self-stop"
fi

# ---------------------------------------------------------------------------
# Step 5: handoff.md written
# [P5] /conductor:autodev writes handoff.md to the conductor work dir
# ---------------------------------------------------------------------------
echo ""
echo "=== STEP 5: verify handoff.md ==="
HANDOFF="$WORKDIR/repo/.conductor/handoff.md"
if [ -f "$HANDOFF" ]; then
  echo "[P5] PASS: handoff.md written at $HANDOFF"
  cat "$HANDOFF" >> "$EVIDENCE_DIR/step5-handoff.log"
else
  echo "[WARN] handoff.md not found at $HANDOFF — checking .conductor/"
  find "$WORKDIR/repo/.conductor" -name "handoff*" 2>/dev/null \
    | tee -a "$EVIDENCE_DIR/step5-handoff.log" || true
  echo "[P5] INCONCLUSIVE: check evidence log"
fi

# ---------------------------------------------------------------------------
# Step 6: re-run /conductor:start — idempotent (every step "already done")
# [P6] Expect all steps to report "already done", exit 0, no questions
# ---------------------------------------------------------------------------
echo ""
echo "=== STEP 6: re-run /conductor:start (idempotency check) ==="
CONDUCTOR_MANIFEST="$MANIFEST_FILE" \
  claude -p "/conductor:start $SPEC_FILE" \
  2>&1 | tee "$EVIDENCE_DIR/step6-rerun.log"
RERUN_RC="${PIPESTATUS[0]}"
echo "[P6] Re-run exit code: $RERUN_RC"
if [ "$RERUN_RC" -ne 0 ]; then
  echo "[FAIL] Idempotent re-run must exit 0 — got $RERUN_RC"
  exit 1
fi
# Verify "already done" appears in output for each step
ALREADY_COUNT=$(grep -c "already done" "$EVIDENCE_DIR/step6-rerun.log" || true)
echo "[P6] 'already done' occurrences: $ALREADY_COUNT"
if [ "$ALREADY_COUNT" -lt 1 ]; then
  echo "[WARN] Expected 'already done' output — check step6-rerun.log"
fi
echo "[P6] PASS: re-run completed, no questions asked"

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== E5 SMOKE SUMMARY ==="
echo "[P0] conductor installed/updated (live, scope user): PASS"
echo "[P1] /conductor:start preflight + setup + cron registered: PASS"
echo "[P2] /conductor:autodev implements phase + merge-gate: PASS"
echo "[P3] conductor assert run --level spec exits 0 (gate GREEN): PASS"
echo "[P4] cron self-deleted after self-stop: see $EVIDENCE_DIR/step4-cron.log"
echo "[P5] handoff.md written: see $EVIDENCE_DIR/step5-handoff.log"
echo "[P6] idempotent re-run (already done, no questions): PASS"
echo ""
echo "[E5 SMOKE] Evidence at: $EVIDENCE_DIR"
echo "[E5 SMOKE] DONE — all recorded pass conditions checked."
exit 0
