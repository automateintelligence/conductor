#!/usr/bin/env bash
# E3 simulated Phase-A test / assertion gate.
#   exit 1 = RED   (failing test)
#   exit 0 = GREEN (passing test)
#
# Per §7 ground-truth precedence this exit code (tests) OUTRANKS the issue
# status-label. It is flipped 1 -> 0 during the experiment to prove the
# precedence resolves correctly in BOTH directions.
exit 0
