#!/usr/bin/env bash
# install_conductor.sh — shared install/update helper for the Conductor experiments.
#
# Defines install_conductor(): registers the LOCAL working tree as the
# `automateintelligence` marketplace and installs-or-updates the `conductor` plugin
# into the REAL ~/.claude config (scope user — the default). Per the dogfood policy
# this NEVER uninstalls: conductor stays installed afterward.
#
# Idempotent:
#   marketplace already configured -> `marketplace update` (re-reads the local tree)
#                       else        -> `marketplace add <abs repo root>`
#   plugin already installed        -> `plugin update conductor@automateintelligence`
#                       else        -> `plugin install conductor@automateintelligence`
#
# Safe to `source` from multiple scripts: defines a function + two constants only, has
# no side effects at source time, and guards against double-sourcing. It deliberately
# does NOT call `set` — it must not alter the caller's shell options.
#
# Usage:
#   source "<repo>/experiments/lib/install_conductor.sh"
#   install_conductor [repo_root]    # repo_root defaults to this file's repo root
#
# Mutates the user's plugin config (the install is intentional and kept). Requires the
# `claude` CLI on PATH.

# Double-source guard (per process). This file is meant to be `source`d; the redirect
# keeps it harmless if it is ever executed directly.
if [ -n "${_CONDUCTOR_INSTALL_LIB_SOURCED:-}" ]; then
  # shellcheck disable=SC2317  # reached only on re-source; return exits the sourced file
  return 0 2>/dev/null || true
fi
_CONDUCTOR_INSTALL_LIB_SOURCED=1

CONDUCTOR_MARKETPLACE_NAME="automateintelligence"
CONDUCTOR_PLUGIN_NAME="conductor"

# install_conductor [repo_root]
#   Returns 0 on success, 1 (with a clear message) on the first failing step.
install_conductor() {
  local repo_root="${1:-}"
  if [ -z "$repo_root" ]; then
    repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
  fi
  local mkt="$CONDUCTOR_MARKETPLACE_NAME"
  local plug="$CONDUCTOR_PLUGIN_NAME"

  if ! command -v claude >/dev/null 2>&1; then
    echo "[install_conductor] FAIL: 'claude' CLI not found on PATH" >&2
    return 1
  fi
  if [ ! -f "$repo_root/.claude-plugin/marketplace.json" ]; then
    echo "[install_conductor] FAIL: no .claude-plugin/marketplace.json under $repo_root" >&2
    return 1
  fi

  echo "[install_conductor] repo_root=$repo_root"
  echo "[install_conductor] marketplace=$mkt plugin=$plug (scope user, no teardown)"

  # 1) Marketplace: add the LOCAL working tree, or update it if already configured.
  #    Output is captured to a variable (not piped) so `grep`/SIGPIPE cannot interact
  #    with `set -o pipefail` in the caller.
  local mkt_list
  mkt_list="$(claude plugin marketplace list 2>/dev/null || true)"
  if [[ "$mkt_list" == *"$mkt"* ]]; then
    echo "[install_conductor] marketplace '$mkt' present -> update"
    claude plugin marketplace update "$mkt" \
      || { echo "[install_conductor] FAIL: 'claude plugin marketplace update $mkt'" >&2; return 1; }
  else
    echo "[install_conductor] marketplace '$mkt' absent -> add (local path)"
    claude plugin marketplace add "$repo_root" \
      || { echo "[install_conductor] FAIL: 'claude plugin marketplace add $repo_root'" >&2; return 1; }
  fi

  # 2) Plugin: update if already installed, else install from our marketplace. The
  #    spec-craft dependency is auto-installed by the install/update.
  local plug_list
  plug_list="$(claude plugin list 2>/dev/null || true)"
  if [[ "$plug_list" == *"${plug}@"* ]]; then
    echo "[install_conductor] plugin '$plug' present -> update"
    # `claude plugin update` requires the plugin@marketplace form (the bare name is "not found").
    claude plugin update "${plug}@${mkt}" \
      || { echo "[install_conductor] FAIL: 'claude plugin update ${plug}@${mkt}'" >&2; return 1; }
  else
    echo "[install_conductor] plugin '$plug' absent -> install '${plug}@${mkt}'"
    claude plugin install "${plug}@${mkt}" \
      || { echo "[install_conductor] FAIL: 'claude plugin install ${plug}@${mkt}'" >&2; return 1; }
  fi

  echo "[install_conductor] OK: marketplace '$mkt' + plugin '$plug' installed/updated"
  return 0
}
