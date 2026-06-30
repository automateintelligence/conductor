#!/usr/bin/env bash
# install_conductor.sh — shared install/update helper for the Conductor experiments.
#
# Defines install_conductor(): registers the `automateintelligence` GitHub marketplace catalog
# and installs-or-updates the `conductor` plugin (which pulls `spec-craft` as a dependency) into
# the REAL ~/.claude config (scope user — the default). Per the dogfood policy this NEVER
# uninstalls: conductor stays installed afterward. It exercises the SAME published catalog real
# users add, so the smoke tests the real install path — not the local working tree (use
# `claude --plugin-dir ./conductor` + preflight for pre-push local checks).
#
# Idempotent:
#   marketplace already configured -> `marketplace update automateintelligence`
#                       else        -> `marketplace add automateintelligence/marketplace`
#   plugin already installed        -> `plugin update conductor@automateintelligence`
#                       else        -> `plugin install conductor@automateintelligence`
#
# Safe to `source` from multiple scripts: defines a function + constants only, has no side
# effects at source time, guards against double-sourcing, and does NOT call `set` (it must not
# alter the caller's shell options).
#
# Usage:
#   source "<repo>/experiments/lib/install_conductor.sh"
#   install_conductor
#
# Mutates the user's plugin config (the install is intentional and kept). Requires the `claude`
# CLI on PATH and network access to the GitHub catalog + plugin repos.

# Double-source guard (per process). This file is meant to be `source`d; the redirect
# keeps it harmless if it is ever executed directly.
if [ -n "${_CONDUCTOR_INSTALL_LIB_SOURCED:-}" ]; then
  # shellcheck disable=SC2317  # reached only on re-source; return exits the sourced file
  return 0 2>/dev/null || true
fi
_CONDUCTOR_INSTALL_LIB_SOURCED=1

CONDUCTOR_MARKETPLACE_NAME="automateintelligence"
CONDUCTOR_MARKETPLACE_SOURCE="automateintelligence/marketplace"  # GitHub owner/repo of the catalog
CONDUCTOR_PLUGIN_NAME="conductor"

# install_conductor
#   Returns 0 on success, 1 (with a clear message) on the first failing step.
install_conductor() {
  local mkt="$CONDUCTOR_MARKETPLACE_NAME"
  local src="$CONDUCTOR_MARKETPLACE_SOURCE"
  local plug="$CONDUCTOR_PLUGIN_NAME"

  if ! command -v claude >/dev/null 2>&1; then
    echo "[install_conductor] FAIL: 'claude' CLI not found on PATH" >&2
    return 1
  fi

  echo "[install_conductor] marketplace=$mkt source=$src plugin=$plug (scope user, no teardown)"

  # 1) Marketplace: add the GitHub catalog, or update it if already configured. Output is
  #    captured to a variable (not piped) so grep/SIGPIPE cannot interact with `set -o pipefail`
  #    in the caller.
  local mkt_list
  mkt_list="$(claude plugin marketplace list 2>/dev/null || true)"
  if [[ "$mkt_list" == *"$mkt"* ]]; then
    echo "[install_conductor] marketplace '$mkt' present -> update"
    claude plugin marketplace update "$mkt" \
      || { echo "[install_conductor] FAIL: 'claude plugin marketplace update $mkt'" >&2; return 1; }
  else
    echo "[install_conductor] marketplace '$mkt' absent -> add '$src'"
    claude plugin marketplace add "$src" \
      || { echo "[install_conductor] FAIL: 'claude plugin marketplace add $src'" >&2; return 1; }
  fi

  # 2) Plugin: update if already installed, else install. The spec-craft dependency is
  #    auto-installed by the install/update.
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
