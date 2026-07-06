"""A1 — authority-preview-covers-the-recipe's-op-set (property).

Contract pinned by this frozen test:
  * `conductor.authority.RECIPE_PRIVILEGED_OPS` is the ONE declared set of privileged
    operations the autodev recipe performs.
  * `conductor authority preview <plan.md>` prints every entry of that set verbatim.

Red-team notes: (a) a preview hard-coded independently of the set fails the
containment-against-the-imported-set check the moment the set and the literal diverge;
(b) the set itself must name each privileged verb the recipe performs, each by a DISTINCT
entry — a single mega-string op that mentions every verb cannot satisfy the
distinct-representative check below.
"""

import importlib
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONDUCTOR = str(ROOT / "bin" / "conductor")
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# The privileged verbs the autodev recipe actually names (spec Phase 1): create branch,
# git push, gh pr, conductor merge, docker (CONDUCTOR_MERGE_VERIFY), subagent spawn, writes.
CATEGORIES = {
    "branch": ("branch",),
    "push": ("push",),
    "gh-pr": ("gh pr",),
    "merge": ("merge",),
    "docker": ("docker", "conductor_merge_verify"),
    "subagent": ("subagent",),
    "writes": ("write",),
}

PLAN = """# Plan — representative fixture

**Normative spec:** docs/specs/fixture.md

## Phase 1 — One (a1-fixture)

**Spec:** §1
- [ ] a task

## Phase 2 — Two (a2-fixture)

**Spec:** §2
- [ ] another task
"""


def _ops() -> set:
    # importlib (not a static import) so pyright stays green while the module is unimplemented;
    # at runtime a missing module still fails this test (RED), which is the point.
    authority = importlib.import_module("conductor.authority")
    ops = set(str(o) for o in authority.RECIPE_PRIVILEGED_OPS)
    assert ops, "RECIPE_PRIVILEGED_OPS must be a non-empty set"
    return ops


def test_declared_set_tracks_the_recipe_verbs():
    ops = _ops()
    # every category must be covered by a DISTINCT op (no mega-string entry)
    matches = {
        cat: {op for op in ops if any(n in op.lower() for n in needles)}
        for cat, needles in CATEGORIES.items()
    }
    used: set = set()
    for cat in sorted(matches, key=lambda c: len(matches[c])):
        pick = next((op for op in sorted(matches[cat]) if op not in used), None)
        assert pick is not None, f"no distinct declared op covers: {cat}"
        used.add(pick)
    assert len(ops) >= len(CATEGORIES)


def test_preview_covers_every_declared_op(tmp_path):
    ops = _ops()
    plan = tmp_path / "plan.md"
    plan.write_text(PLAN)
    proc = subprocess.run(
        [CONDUCTOR, "authority", "preview", str(plan)],
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    out = proc.stdout
    assert out.strip() != ""
    for op in sorted(ops):
        assert op in out, f"preview omits declared privileged op: {op!r}"
    # must-not: the preview never references the removed grant command
    assert "grant --scoped" not in out
    assert "grant --full" not in out
