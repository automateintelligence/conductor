"""Mechanical lint of a plan.md before issue-sync.

Dogfood findings: the plan dictates every phase yet was the least-reviewed setup artifact —
the live plan carried zero spec binding (workers "cooked from the ingredients list") and
dropped the per-phase recipe (worker skipped PR + codex review). Prompt instructions decay;
this lint is the enforcement. Exit 0 clean; exit 1 with one reason per line on stderr;
exit 2 unreadable plan.
"""

from __future__ import annotations

import argparse
import os
import re
import sys

from ledger import sync

_NORMATIVE = re.compile(r"(?im)^\s*(?:[>*-]\s*)*\*{0,2}normative spec\*{0,2}\s*:")
# Accepts both the minimal pointer ("**Spec:** §6; §7") and the annotated form real plans
# grew organically ("**Spec intent — REQUIRED READING (build to these, not just A6):**").
_SPEC_POINTER = re.compile(r"(?im)^\s*(?:[>*-]\s*)*\*{0,2}spec\b[^:\n]*:")
# For phase-no-tasks the CHECKED state must count too — a completed phase's boxes are all
# [x] and it still has tasks (live-run finding: the unchecked-only regex red-flagged done
# phases forever). issue-sync's parser stays unchecked-only by design (done work must not
# respawn sub-issues); only the lint uses this broader form.
_TASK_ANY = re.compile(r"^- \[[ xX]\] ", re.MULTILINE)
# The per-phase recipe's load-bearing markers: self-review per task, codex review of the PR,
# the merge gate, and the PR<->phase-issue link. Substring, case-insensitive.
_RECIPE_NEEDLES = ("/code-review", "codex", "merge-gate", "closes #")


def lint(text: str, spec_path: str | None = None) -> list[str]:
    reasons: list[str] = []
    if not _NORMATIVE.search(text):
        reasons.append("normative-spec-missing")
    if spec_path:
        spec_name = os.path.basename(spec_path)
        if spec_name not in text:
            reasons.append(f"spec-not-referenced:{spec_name}")

    headings = list(sync._H2_ANY.finditer(text))
    found_phase = False
    for i, m in enumerate(headings):
        parsed = sync._phase_heading(m.group(1))
        if parsed is None:
            continue
        found_phase = True
        title = parsed[0]
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        section = text[m.end() : end]
        if not _TASK_ANY.search(section):
            reasons.append(f"phase-no-tasks:{title}")
        if not _SPEC_POINTER.search(section):
            reasons.append(f"phase-no-spec-pointer:{title}")
        # A phase without assertion ids can't be gate-verified downstream (--from-gate /
        # phase-done fail closed on a missing marker) — gatelessness must be deliberate,
        # declared with a literal `gate: none` in the phase section (codex PR-28 #1).
        if not parsed[2] and "gate: none" not in section.lower():
            reasons.append(f"phase-no-assertions:{title}")
    if not found_phase:
        reasons.append("no-phases")

    lowered = text.lower()
    for needle in _RECIPE_NEEDLES:
        if needle not in lowered:
            reasons.append(f"recipe-missing:{needle}")
    return reasons


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="conductor plan-lint",
        description="Fail-closed plan lint: spec binding + per-phase tasks/Spec pointers "
        "+ recipe markers must all be present before issue-sync",
    )
    p.add_argument("plan_md", metavar="plan.md")
    p.add_argument(
        "--spec",
        default=None,
        metavar="PATH",
        help="Normative spec file the plan must reference by name",
    )
    args = p.parse_args(argv)
    try:
        with open(args.plan_md, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        print(f"plan-unreadable: {exc}", file=sys.stderr)
        return 2
    reasons = lint(text, spec_path=args.spec)
    for reason in reasons:
        print(reason, file=sys.stderr)
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
