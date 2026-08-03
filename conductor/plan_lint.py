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
# `adrs?` accepts the singular `**ADR:**` deliberately — same leniency `_SPEC_POINTER` has,
# and a plural-only rule would fail a phase that correctly names its one decision.
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
# `\r` is in the set so a CRLF plan's empty line reports `phase-adr-empty`, not a
# `phase-adr-malformed:…:\r` (the capture is `(.*)$`, and `.` matches `\r`).
_EMPHASIS = " \t\r*`_"
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


def _adr_values(section: str) -> list[str]:
    """Every `**ADRs:**` value in the phase, in document order (empty when none).

    ALL of them, not just the first: a backfilled `**ADRs:** none` inserted after the
    `**Spec:**` line sits ABOVE any pointer the author already wrote further down, so
    first-match-wins would let the inserted line shadow — and silently validate — a
    malformed or contradictory one below it."""
    return [m.group(1).strip(_EMPHASIS) for m in _ADR_POINTER.finditer(section)]


def _adr_fragments(value: str) -> list[str]:
    """The value's non-empty semicolon-separated fragments.

    Semicolon-separated, matching the `**Spec:** §6 Metrics; §7 Scoring` convention, so a
    comma inside an ADR's title stays inside its fragment. Empty when the value carries no
    fragment at all (`;`, `;;`) — delimiters are not an answer."""
    return [f for f in (raw.strip(_EMPHASIS) for raw in value.split(";")) if f]


def _adr_refs(value: str) -> tuple[list[str], list[str]]:
    """(well-formed references, unparsable fragments) for an `**ADRs:**` value."""
    frags = _adr_fragments(value)
    # `none` is the explicit "none apply" ONLY as the whole value. `none; ADR-9` asserts
    # both that no decision applies and that one does, so it is parsed as an ordinary
    # fragment and reported malformed rather than silently truncating to `none`.
    if len(frags) == 1 and _ADR_NONE.match(frags[0]):
        return [], []
    refs: list[str] = []
    bad: list[str] = []
    for frag in frags:
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


def _adr_reasons(title: str, section: str) -> list[str]:
    """The decisions leg's failures for one phase section.

    Factored out because two callers need EXACTLY this rule and no other: the whole-plan
    `lint()`, and `lint_phase_adrs()` — autodev's pre-claim check."""
    reasons: list[str] = []
    values = _adr_values(section)
    if not values:
        reasons.append(f"phase-no-adr-pointer:{title}")
    # Two pointer lines in one phase leave the worker to guess which binds — the
    # likeliest source is a backfill or a merge landing beside an existing line.
    if len(values) > 1:
        reasons.append(f"phase-adr-duplicate:{title}")
    for value in values:
        # Delimiters or emphasis with nothing between them are silence wearing the
        # line's clothes, exactly like the wholly empty value.
        if not _adr_fragments(value):
            reasons.append(f"phase-adr-empty:{title}")
            continue
        for frag in _adr_refs(value)[1]:
            reasons.append(f"phase-adr-malformed:{title}:{frag}")
    return reasons


def lint_phase_adrs(text: str, phase_title: str) -> tuple[list[str], list[str]]:
    """(reasons, references) for ONE phase's `**ADRs:**` line, by phase title.

    autodev's fail-closed pre-claim check. Deliberately the decisions leg ALONE and not
    the whole `lint()`: a headless fire has no owner standing by, so it must be stoppable
    only by the defect that would make its own work unsafe — never by an unrelated plan
    problem elsewhere in the file. `phase_title` is the phase ISSUE's title, which equals
    the plan's phase heading (`convert`/`generate` create issues from those headings, and
    `phase-done` ticks checkboxes by the same equality).

    A phase absent from the plan is a failure, not a pass: nothing carries its decisions
    either, which is the condition being checked."""
    for (title, _status, _ids), section in _phase_sections(text):
        if title != phase_title:
            continue
        refs = [ref for value in _adr_values(section) for ref in _adr_refs(value)[0]]
        return _adr_reasons(title, section), refs
    return [f"phase-not-found:{phase_title}"], []


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
        reasons.extend(_adr_reasons(title, section))
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
    """`ADR-12` matches `adr-012-foo.md` and `0012-foo.md`, never `120-foo.md`.

    The number must occupy the filename's LEADING number slot — bare, or behind an `adr`
    prefix. Matching it anywhere would let `2026-08-12-unrelated.md` resolve `ADR-12` and
    silently swallow the dangling-reference warning."""
    digits = re.search(r"\d+", ref)
    if digits is None:
        return False
    n = int(digits.group(0))
    return re.match(rf"(?:adr[-_ ]?)?0*{n}(?!\d)", filename) is not None


def _adr_index(root: str) -> list[str] | None:
    """Lowercased filenames across root's conventional ADR dirs, or None when the repo
    has no ADR dir at all — an EMPTY dir is checkable ([]), a missing one is not.

    An unreadable dir reads as unindexable (None), never as an exception: this whole leg
    is advisory, and a warning must not be able to take down a lint that would pass."""
    names: list[str] = []
    found = False
    for d in _ADR_DIRS:
        p = os.path.join(root, d)
        if os.path.isdir(p):
            try:
                entries = os.listdir(p)
            except OSError:
                continue
            found = True
            names.extend(n.lower() for n in entries)
    return names if found else None


def adr_warnings(text: str, root: str) -> list[str]:
    """Cheap dangling-`**ADRs:**`-reference warnings — NEVER lint failures.

    Plans are routinely written before the ADRs they cite land, so an unresolvable
    reference is a nudge, not a defect. Silent when there is nothing to check against:
    a path whose parent dir is absent, or an id when the repo has no ADR dir at all."""
    warns: list[str] = []
    index = _adr_index(root)
    for (title, _status, _ids), section in _phase_sections(text):
        for value in _adr_values(section):
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
    p.add_argument(
        "--phase",
        default=None,
        metavar="TITLE",
        help="Check ONLY this phase's **ADRs:** line (the phase issue's title) and print "
        "its references to stdout. autodev's pre-claim check — scoped this narrowly on "
        "purpose, so an unrelated plan defect can never stop a headless fire.",
    )
    args = p.parse_args(argv)
    try:
        with open(args.plan_md, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        print(f"plan-unreadable: {exc}", file=sys.stderr)
        return 2
    if args.phase is not None:
        reasons, refs = lint_phase_adrs(text, args.phase)
        for ref in refs:
            print(ref)
        for reason in reasons:
            print(reason, file=sys.stderr)
        if reasons:
            print(
                f"{args.plan_md}: this phase carries no usable **ADRs:** line, so nothing "
                "would carry its architectural decisions to the worker. Run "
                "/conductor:prepare on this repo to backfill the 0.9.0 plan dialect, "
                "then re-fire.",
                file=sys.stderr,
            )
        return 1 if reasons else 0
    # Belt to _adr_index's braces: the warning leg is advisory end to end, so ANY failure
    # resolving it (unreadable dir, vanished path, permission change mid-walk) costs the
    # warnings and nothing else. The lint's verdict is never the warning leg's to change.
    try:
        warnings = adr_warnings(text, _project_root(args.plan_md))
    except OSError as exc:
        warnings = [f"warn:phase-adr-unresolvable: {exc}"]
    for warning in warnings:
        print(warning, file=sys.stderr)
    reasons = lint(text, spec_path=args.spec)
    for reason in reasons:
        print(reason, file=sys.stderr)
    return 1 if reasons else 0


if __name__ == "__main__":
    sys.exit(main())
