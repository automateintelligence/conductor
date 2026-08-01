"""Mechanical lint of a plan.md before issue-sync.

Dogfood findings: the plan dictates every phase yet was the least-reviewed setup artifact —
the live plan carried zero spec binding (workers "cooked from the ingredients list") and
dropped the per-phase recipe (worker skipped PR + codex review). Prompt instructions decay;
this lint is the enforcement. Exit 0 clean; exit 1 with one reason per line on stderr;
exit 2 unreadable plan.

Warnings (`warn:` prefix) are printed to stderr too but never affect the exit status.
"""

from __future__ import annotations

import argparse
import os
import re
import sys
from collections.abc import Iterator

from ledger import sync

_NORMATIVE = re.compile(r"(?im)^\s*(?:[>*-]\s*)*\*{0,2}normative spec\*{0,2}\s*:")
# Accepts both the minimal pointer ("**Spec:** §6; §7") and the annotated form real plans
# grew organically ("**Spec intent — REQUIRED READING (build to these, not just A6):**").
_SPEC_POINTER = re.compile(r"(?im)^\s*(?:[>*-]\s*)*\*{0,2}spec\b[^:\n]*:")
# Exactly parallel to _SPEC_POINTER, and required for the same reason: an architectural
# decision that lives only in an ADR reaches no worker. The plan's spec pointer carries the
# spec to the phase; nothing carried the DECISIONS, so a later phase could relitigate a
# closed one with the done-gate still green (live finding 2026-08-01: two decisions —
# "SUMO case roles are not adopted" and "extraction closes over the relation taxonomy, not
# the synonym map" — existed only in ADRs and appeared in neither spec nor plan).
# The value is captured so the referenced ids can be checked for well-formedness.
_ADR_POINTER = re.compile(
    r"(?im)^[ \t]*(?:[>*-][ \t]*)*\*{0,2}adrs?\b[^:\n]*:[ \t]*(.*)$"
)
# A reference is an ADR id ("ADR-012", "adr 7") or a path to a decision doc
# ("docs/adr/0001-drift-to-mop-query-path.md" — repos that number without the prefix).
_ADR_ID = re.compile(r"(?i)\badr[-_ ]?\d+\b")
_ADR_PATH = re.compile(r"(?i)\b[\w./-]+\.md\b")
# `none` is a VALID, explicit answer — the whole point of requiring the line is that
# silence must not be indistinguishable from "considered, and none apply".
_ADR_NONE = re.compile(r"(?i)^none\b")
# Markdown emphasis around the value: "**ADRs:** ADR-1" captures "** ADR-1".
_EMPHASIS = " \t*`_"
# Where ADRs conventionally live; used ONLY for the cheap dangling-reference warning.
_ADR_DIRS = ("docs/adr", "docs/ADR", "docs/adrs", "docs/decisions")


def _phase_sections(text: str) -> Iterator[tuple[tuple[str, str, list[str]], str]]:
    """Yield ((title, status, assertion-ids), section-body) per phase heading."""
    headings = list(sync._H2_ANY.finditer(text))
    for i, m in enumerate(headings):
        parsed = sync._phase_heading(m.group(1))
        if parsed is None:
            continue
        end = headings[i + 1].start() if i + 1 < len(headings) else len(text)
        yield parsed, text[m.end() : end]


def _adr_value(section: str) -> str | None:
    """The phase's `**ADRs:**` value, or None when the line is absent entirely."""
    m = _ADR_POINTER.search(section)
    return None if m is None else m.group(1).strip(_EMPHASIS)


def _adr_refs(value: str) -> tuple[list[str], list[str]]:
    """(well-formed references, unparsable fragments) for an `**ADRs:**` value.

    Semicolon-separated, matching the `**Spec:** §6 Metrics; §7 Scoring` convention, so a
    comma inside an ADR's title stays inside its fragment."""
    if _ADR_NONE.match(value):
        return [], []
    refs: list[str] = []
    bad: list[str] = []
    for raw in value.split(";"):
        frag = raw.strip(_EMPHASIS)
        if not frag:
            continue
        # Paths first, then ids from what is LEFT — `docs/adr/ADR-012-spine.md` is one
        # reference, not a path plus the id embedded in its filename.
        found = _ADR_PATH.findall(frag) + _ADR_ID.findall(_ADR_PATH.sub(" ", frag))
        if found:
            refs.extend(found)
        else:
            bad.append(frag)
    return refs, bad


# For phase-no-tasks the CHECKED state must count too — a completed phase's boxes are all
# [x] and it still has tasks (live-run finding: the unchecked-only regex red-flagged done
# phases forever). issue-sync's parser stays unchecked-only by design (done work must not
# respawn sub-issues); only the lint uses this broader form.
_TASK_ANY = re.compile(r"^- \[[ xX]\] .+$", re.MULTILINE)
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

    found_phase = False
    for parsed, section in _phase_sections(text):
        found_phase = True
        title = parsed[0]
        if not _TASK_ANY.search(section):
            reasons.append(f"phase-no-tasks:{title}")
        if not _SPEC_POINTER.search(section):
            reasons.append(f"phase-no-spec-pointer:{title}")
        # The decisions leg of the same binding. `**ADRs:** none` passes; a MISSING line
        # is a failure, because "nobody checked" must not look like "none apply".
        adr_value = _adr_value(section)
        if adr_value is None:
            reasons.append(f"phase-no-adr-pointer:{title}")
        elif not adr_value:
            reasons.append(f"phase-adr-empty:{title}")
        else:
            for frag in _adr_refs(adr_value)[1]:
                reasons.append(f"phase-adr-malformed:{title}:{frag}")
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


def _id_matches(ref: str, filename: str) -> bool:
    """`ADR-12` matches `adr-012-foo.md` and `0012-foo.md`, never `120-foo.md`."""
    digits = re.search(r"\d+", ref)
    if digits is None:
        return False
    return re.search(rf"(?<!\d)0*{int(digits.group(0))}(?!\d)", filename) is not None


def _adr_index(root: str) -> list[str] | None:
    """Lowercased filenames across root's conventional ADR dirs, or None when the repo
    has no ADR dir at all — an EMPTY dir is checkable ([]), a missing one is not."""
    names: list[str] = []
    found = False
    for d in _ADR_DIRS:
        p = os.path.join(root, d)
        if os.path.isdir(p):
            found = True
            names.extend(n.lower() for n in os.listdir(p))
    return names if found else None


def adr_warnings(text: str, root: str) -> list[str]:
    """Cheap dangling-`**ADRs:**`-reference warnings — NEVER lint failures.

    Plans are routinely written before the ADRs they cite land, so an unresolvable
    reference is a nudge, not a defect. Silent when there is nothing to check against:
    a path whose parent dir is absent, or an id when the repo has no ADR dir at all."""
    warns: list[str] = []
    index = _adr_index(root)
    for (title, _status, _ids), section in _phase_sections(text):
        value = _adr_value(section)
        if not value:
            continue
        for ref in _adr_refs(value)[0]:
            if ref.lower().endswith(".md"):
                target = os.path.join(root, ref)
                if os.path.isdir(os.path.dirname(target)) and not os.path.exists(
                    target
                ):
                    warns.append(f"warn:phase-adr-dangling:{title}:{ref}")
            elif index is not None and not any(_id_matches(ref, n) for n in index):
                warns.append(f"warn:phase-adr-dangling:{title}:{ref}")
    return warns


def _project_root(plan_md: str) -> str:
    """The repo the plan lives in — used only to resolve ADR references. Walks up for a
    `.git` (a FILE in a worktree, hence exists() not isdir()) or a conventional ADR dir;
    falls back to cwd, which is where the skills document running plan-lint from."""
    d = os.path.dirname(os.path.abspath(plan_md))
    while True:
        if os.path.exists(os.path.join(d, ".git")) or any(
            os.path.isdir(os.path.join(d, a)) for a in _ADR_DIRS
        ):
            return d
        parent = os.path.dirname(d)
        if parent == d:
            return os.getcwd()
        d = parent


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="conductor plan-lint",
        description="Fail-closed plan lint: spec binding + per-phase tasks/Spec/ADRs "
        "pointers + recipe markers must all be present before issue-sync "
        "(`**ADRs:** none` is a valid, explicit answer; a missing line is not)",
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
    for warning in adr_warnings(text, _project_root(args.plan_md)):
        print(warning, file=sys.stderr)
    reasons = lint(text, spec_path=args.spec)
    for reason in reasons:
        print(reason, file=sys.stderr)
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
