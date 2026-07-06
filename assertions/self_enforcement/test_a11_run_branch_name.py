"""A11 — run-branch-name-deterministic (property).

Contract pinned: `conductor run-branch name <spec>` prints exactly one line of the form
`conductor/run-<slug>`, byte-identical across invocations for the same spec, derived from
the spec's identity (the slug carries the spec filename's stem), and different specs get
different names — so start and autodev binding to the same spec can never diverge, and
two concurrent runs can never collide.
"""

import re
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONDUCTOR = str(ROOT / "bin" / "conductor")
SPEC = "docs/specs/2026-07-05-self-enforcement.md"
NAME_RE = re.compile(r"conductor/run-[a-z0-9][a-z0-9._-]*\Z")


def _name(spec: str) -> str:
    proc = subprocess.run(
        [CONDUCTOR, "run-branch", "name", spec],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout.strip()
    assert out != ""
    assert "\n" not in out
    return out


def test_same_spec_yields_byte_identical_canonical_name():
    first = _name(SPEC)
    second = _name(SPEC)
    assert first == second
    assert NAME_RE.fullmatch(first), first
    # the slug tracks the spec's identity, not a constant
    assert "self-enforcement" in first
    # must-not: no whitespace inside the emitted ref name
    assert " " not in first


def test_different_specs_yield_different_names(tmp_path):
    other = tmp_path / "2099-01-01-other-thing.md"
    other.write_text("# other spec\n")
    a = _name(SPEC)
    b = _name(str(other))
    assert NAME_RE.fullmatch(b), b
    assert a != b, "two different specs must not share a run branch"
