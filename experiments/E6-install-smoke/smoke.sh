#!/usr/bin/env bash
# smoke.sh — E6 live install + end-to-end smoke for the `conductor` plugin.
#
# Deterministic + headless. Installs/updates conductor into the REAL ~/.claude (scope
# user) from the LOCAL working tree, then validates the installed cluster end to end.
# NO GitHub mutation, NO nested `claude -p`, NO cron. Per the dogfood policy it does
# NOT uninstall — conductor stays installed afterward. Runnable in CI / unattended.
#
# Run:
#   bash experiments/E6-install-smoke/smoke.sh
#
# Pass markers (each printed PASS/FAIL; the script exits non-zero if any fail):
#   [P1] INSTALL/UPDATE   idempotent marketplace add(local)/update + plugin install/update
#   [P2] PLUGINS PRESENT  `claude plugin list` shows conductor AND spec-craft (dependency)
#   [P3] CLI REACHABLE    resolve the installed bin from the plugin cache (NOT via PATH)
#   [P4] PREFLIGHT        installed `<bin> preflight` exits 0 (conducted skill stack resolves)
#   [P5] MACHINERY        installed `<bin> assert run` goes RED(1) -> GREEN(0) (real done-gate)
#
# Side effects: the install into ~/.claude (intentional, kept) and a self-cleaned
# mktemp dir. The installed runner writes assertions/run/results.json inside the plugin
# cache dir during [P5] (harmless). The repo and all git remotes are left untouched.

set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# shellcheck source=../lib/install_conductor.sh disable=SC1091
source "$REPO_ROOT/experiments/lib/install_conductor.sh"

# Temp dirs to clean on exit. The install itself is intentionally NOT cleaned.
CLEANUP_DIRS=()
# shellcheck disable=SC2329  # invoked indirectly via 'trap cleanup EXIT' below
cleanup() {
  local d
  for d in "${CLEANUP_DIRS[@]:-}"; do
    [ -n "$d" ] && rm -rf "$d"
  done
}
trap cleanup EXIT

P1_OK=0; P2_OK=0; P3_OK=0; P4_OK=0; P5_OK=0
BIN=""

fail() { echo "[FAIL] $*" >&2; }
status() { if [ "$1" -eq 1 ]; then echo "PASS"; else echo "FAIL"; fi; }

echo "[E6 SMOKE] REPO_ROOT=$REPO_ROOT"

# ---------------------------------------------------------------------------
# [P1] INSTALL / UPDATE — idempotent marketplace add(local) + plugin install/update.
# ---------------------------------------------------------------------------
echo ""
echo "=== [P1] INSTALL/UPDATE ==="
if install_conductor; then
  P1_OK=1
  echo "[P1] PASS: marketplace added/updated + conductor installed/updated"
else
  fail "[P1] install/update of conductor failed"
  echo ""
  echo "=== E6 SUMMARY ==="
  echo "[P1] INSTALL/UPDATE : FAIL"
  echo "[P2] PLUGINS PRESENT: SKIP"
  echo "[P3] CLI REACHABLE  : SKIP"
  echo "[P4] PREFLIGHT      : SKIP"
  echo "[P5] MACHINERY      : SKIP"
  echo "[E6 SMOKE] FAILED — install/update did not complete; nothing else can be checked."
  exit 1
fi

# ---------------------------------------------------------------------------
# [P2] PLUGINS PRESENT — `claude plugin list` shows conductor AND spec-craft.
# Output is captured to a variable and matched via here-strings (no pipes), so
# `grep`/SIGPIPE cannot interact with `set -o pipefail`.
# ---------------------------------------------------------------------------
echo ""
echo "=== [P2] PLUGINS PRESENT ==="
PLUGIN_LIST="$(claude plugin list 2>&1 || true)"
P2_OK=1
COND_LINE="$(grep -m1 'conductor@' <<<"$PLUGIN_LIST" || true)"
if [ -n "$COND_LINE" ]; then
  echo "[P2] found conductor:$(printf '%s' "$COND_LINE" | tr -s ' ')"
else
  fail "[P2] conductor NOT present in 'claude plugin list'"
  P2_OK=0
fi
SPEC_LINE="$(grep -m1 'spec-craft@' <<<"$PLUGIN_LIST" || true)"
if [ -n "$SPEC_LINE" ]; then
  echo "[P2] found spec-craft:$(printf '%s' "$SPEC_LINE" | tr -s ' ')"
else
  fail "[P2] spec-craft (dependency) NOT present in 'claude plugin list'"
  P2_OK=0
fi
if [ "$P2_OK" -eq 1 ]; then
  echo "[P2] PASS: conductor + spec-craft both present"
fi

# ---------------------------------------------------------------------------
# [P3] CLI REACHABLE — installed plugins are NOT on PATH. Resolve the bin from the
# plugin cache: cache/<marketplace>/conductor/<version>/bin/conductor. Newest version
# directory wins via `sort -V`.
# ---------------------------------------------------------------------------
echo ""
echo "=== [P3] CLI REACHABLE ==="
shopt -s nullglob
# Pin to OUR marketplace so a stale/different marketplace's cached conductor can't false-green.
_MKT="${CONDUCTOR_MARKETPLACE_NAME:-automateintelligence}"
declare -a BINS=( "$HOME"/.claude/plugins/cache/"$_MKT"/conductor/*/bin/conductor )
shopt -u nullglob
if [ "${#BINS[@]}" -eq 0 ]; then
  fail "[P3] no installed conductor bin under ~/.claude/plugins/cache/$_MKT/conductor/*/bin/conductor"
  P3_OK=0
else
  BIN="$(printf '%s\n' "${BINS[@]}" | sort -V | tail -n1)"
  if [ -x "$BIN" ]; then
    echo "[P3] PASS: resolved executable bin: $BIN"
    P3_OK=1
  else
    fail "[P3] resolved path is not executable: $BIN"
    P3_OK=0
  fi
fi

# ---------------------------------------------------------------------------
# [P4] PREFLIGHT — the core install-cluster validation. `<bin> preflight` exits 0 only
# when the conducted skill stack (conductor + spec-craft + superpowers + bare cmds)
# resolves. On failure, surface its MISSING: lines.
# ---------------------------------------------------------------------------
echo ""
echo "=== [P4] PREFLIGHT ==="
if [ "$P3_OK" -eq 1 ]; then
  if PREFLIGHT_OUT="$("$BIN" preflight 2>&1)"; then
    echo "[P4] PASS: preflight exits 0 (conducted skill stack resolves)"
    P4_OK=1
  else
    fail "[P4] preflight exited non-zero — skill stack does not resolve:"
    grep '^MISSING:' <<<"$PREFLIGHT_OUT" || printf '%s\n' "$PREFLIGHT_OUT"
    P4_OK=0
  fi
else
  echo "[P4] SKIP: no installed bin resolved in [P3]"
fi

# ---------------------------------------------------------------------------
# [P5] MACHINERY RED->GREEN — prove the INSTALLED CLI runs the real done-gate.
# The installed runner executes assertion commands with cwd = the plugin cache dir, so
# module resolution is via PYTHONPATH (matches tests/conductor/test_e2e.py), NOT cwd.
# CONDUCTOR_FREEZE_BASELINE points at a nonexistent path so a frozen baseline shipped in
# the installed plugin (if any) cannot turn this into an exit-6 tamper result.
# ---------------------------------------------------------------------------
echo ""
echo "=== [P5] MACHINERY RED->GREEN (installed CLI) ==="
if [ "$P3_OK" -eq 1 ]; then
  P5_DIR="$(mktemp -d)"
  CLEANUP_DIRS+=("$P5_DIR")
  cat > "$P5_DIR/manifest.yaml" <<'P5_EOF'
assertions:
  - id: hello
    claim: "hello() returns HELLO"
    command: "python3 -c \"from hello import hello; assert hello() == 'HELLO'\""
    level: spec
    kind: example
P5_EOF

  run_gate() {
    ( cd "$P5_DIR" \
      && CONDUCTOR_MANIFEST="$P5_DIR/manifest.yaml" \
         PYTHONPATH="$P5_DIR" \
         CONDUCTOR_FREEZE_BASELINE="$P5_DIR/.no-baseline" \
         "$BIN" assert run --level spec )
  }

  # RED: hello.py absent -> import error -> assertion red -> runner exit 1.
  RED_RC=0
  run_gate || RED_RC=$?
  echo "[P5] RED run exit=$RED_RC (expect 1)"

  if [ "$RED_RC" -ne 1 ]; then
    fail "[P5] expected RED exit 1, got $RED_RC"
    P5_OK=0
  else
    printf 'def hello():\n    return "HELLO"\n' > "$P5_DIR/hello.py"
    GREEN_RC=0
    run_gate || GREEN_RC=$?
    echo "[P5] GREEN run exit=$GREEN_RC (expect 0)"
    if [ "$GREEN_RC" -eq 0 ]; then
      echo "[P5] PASS: installed CLI ran RED(1) -> GREEN(0) done-gate"
      P5_OK=1
    else
      fail "[P5] expected GREEN exit 0, got $GREEN_RC"
      P5_OK=0
    fi
  fi
else
  echo "[P5] SKIP: no installed bin resolved in [P3]"
fi

# ---------------------------------------------------------------------------
# Summary
# ---------------------------------------------------------------------------
echo ""
echo "=== E6 SUMMARY ==="
echo "[P1] INSTALL/UPDATE : $(status "$P1_OK")"
echo "[P2] PLUGINS PRESENT: $(status "$P2_OK")"
echo "[P3] CLI REACHABLE  : $(status "$P3_OK")"
echo "[P4] PREFLIGHT      : $(status "$P4_OK")"
echo "[P5] MACHINERY      : $(status "$P5_OK")"

if [ "$P1_OK" -eq 1 ] && [ "$P2_OK" -eq 1 ] && [ "$P3_OK" -eq 1 ] \
   && [ "$P4_OK" -eq 1 ] && [ "$P5_OK" -eq 1 ]; then
  echo "[E6 SMOKE] DONE — all pass markers green. conductor remains installed."
  exit 0
fi
echo "[E6 SMOKE] FAILED — one or more pass markers red (see above)."
exit 1
