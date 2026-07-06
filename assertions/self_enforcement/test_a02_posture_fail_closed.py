"""A2 — unknown-mode-resolves-least-privileged (property).

Contract pinned: `conductor.authority.resolve_posture(mode)` maps a detected Claude Code
session permission mode to one of the driver's posture labels
{"full-bypass", "scoped", "supervised"} and is FAIL-CLOSED: any input that is not an
affirmatively-recognized bypass mode resolves to a non-bypass posture; anything unknown,
empty, unreadable (None), or ambiguous resolves to exactly "supervised".

Red-team notes: a constant `return "supervised"` fails the recognized-bypass case; a
constant `return "full-bypass"` (or substring matching like `"bypassPermissions" in mode`)
fails the garbage sweep, which includes near-misses and a token-embedded variant.
"""

import importlib
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

BYPASS = "bypassPermissions"

# Deliberately-garbage / ambiguous inputs: near-misses, empty, embedded token, nonsense.
GARBAGE = [
    "",
    "wibble",
    "bypas",
    "bypassPermission",  # singular near-miss
    "permissions",
    "full",
    "allow-all",
    "0",
    "null",
    "None",
    "sudo",
    "trust",
    "yes",
    "plan?",
    "bypassPermissions extra",  # embedded token: ambiguous, must not over-grant
]


def _resolve_posture():
    # importlib (not a static import) so pyright stays green while the module is
    # unimplemented; at runtime a missing module still fails this test (RED).
    return importlib.import_module("conductor.authority").resolve_posture


def test_recognized_modes_resolve_to_their_posture():
    resolve_posture = _resolve_posture()
    assert resolve_posture(BYPASS) == "full-bypass"
    assert resolve_posture("default") == "supervised"
    for mode in ("acceptEdits", "plan"):
        p = resolve_posture(mode)
        assert p in ("scoped", "supervised"), (mode, p)


def test_unknown_or_unreadable_modes_fail_closed_to_supervised():
    resolve_posture = _resolve_posture()
    for mode in GARBAGE + [None]:
        p = resolve_posture(mode)
        assert p == "supervised", (mode, p)
        # must-not: no non-affirmative input ever arms a bypass posture
        assert "bypass" not in str(p)


def test_posture_vocabulary_is_closed():
    resolve_posture = _resolve_posture()
    for mode in [BYPASS, "default", "acceptEdits", "plan"] + GARBAGE:
        assert resolve_posture(mode) in ("full-bypass", "scoped", "supervised")
