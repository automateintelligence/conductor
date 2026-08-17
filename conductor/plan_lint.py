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

# CommonMark fence: 3+ backticks or tildes, indented up to 3 spaces, optional info string.
_FENCE = re.compile(r"^ {0,3}(?P<f>`{3,}|~{3,})(?P<info>.*)$")


def _fence_scan(text: str) -> Iterator[tuple[int, str, bool, str | None]]:
    """(line-start offset, line without its newline, inside-a-fenced-block, fence still open
    after this line) per line.

    THE fence state machine for this module — the section splitter and the per-line marker
    check both read it, so "what is fenced" cannot mean two things in one lint. A fence's own
    delimiter lines count as inside: they are markup, never plan content.

    A fence closes only on the SAME character, run at least as long, and nothing but
    whitespace after it — so a ``` inside a ~~~ block, and a ``` inside a ```` block, are
    both content. Plans document markdown, so nested fences are ordinary here.

    An UNTERMINATED fence runs to the end of `text`, which is what CommonMark does with an
    unclosed fence at the end of its container. It is also the safe direction for the marker
    check: that check is a hard failure, so a missed finding costs a warning nobody got, while
    a false one costs a legitimate plan its exit 0. The fourth element is what lets the
    splitter take the OPPOSITE reading — see `_closed_fence_offsets`.
    """
    fence: str | None = None
    pos = 0
    for line in text.split("\n"):
        m = _FENCE.match(line)
        if fence is None:
            inside = m is not None
            if m is not None:
                fence = m.group("f")
        else:
            inside = True
            if (
                m is not None
                and m.group("f")[0] == fence[0]
                and len(m.group("f")) >= len(fence)
                and not m.group("info").strip()
            ):
                fence = None
        yield pos, line, inside, fence
        pos += len(line) + 1


def _closed_fence_offsets(text: str) -> set[int]:
    """Line-start offsets of lines inside a CLOSED fenced block, delimiters included.

    Closed only, and that asymmetry against `_unfenced_lines` is the point. The splitter reads
    the WHOLE plan, so an unterminated fence would run past every following heading and merge
    the rest of the file into one phase — a forgotten ``` costing the plan its remaining
    phases, their pointers reported as duplicates of the phase that swallowed them. A heading
    is markup strong enough to bound the damage: an unterminated fence keeps suppressing the
    marker check to the end of ITS OWN section (`_unfenced_lines`, which never sees more than
    one section) while the headings after it stay headings."""
    out: set[int] = set()
    pending: list[int] = []
    for pos, _line, inside, still_open in _fence_scan(text):
        if inside:
            pending.append(pos)
        # Nothing open after this line, so any pending fence CLOSED on it.
        if still_open is None:
            out.update(pending)
            pending.clear()
    # A leftover `pending` is an unterminated fence — deliberately left unfenced.
    return out


def _unfenced_lines(section: str) -> Iterator[str]:
    """The section's lines with fenced code blocks removed, newlines stripped.

    Scope is one phase section — every caller iterates per section — so a fence someone
    forgot to close can silently swallow at most the rest of its own phase, never the file.
    """
    for _pos, line, inside, _open in _fence_scan(section):
        if not inside:
            yield line


def _phase_sections(text: str) -> Iterator[tuple[tuple[str, str, list[str]], str]]:
    """Yield ((title, status, assertion-ids), section-body) per phase heading.

    An H2 inside a CLOSED fenced block is EXAMPLE TEXT, not a heading, and is skipped. This
    happens during the split, not after it: filtering the lines of an already-split section
    (what `_unfenced_lines` does for the marker check) is too late — a fenced
    `## Phase example (A99)` had already become a section of its own, and every per-phase
    requirement it could not satisfy became a hard failure against a heading nobody wrote as
    a phase. A skipped heading does not end the enclosing phase's section either; the fence
    it sits in is part of that phase's body, where the marker check already ignores it."""
    fenced = _closed_fence_offsets(text)
    headings = [m for m in sync._H2_ANY.finditer(text) if m.start() not in fenced]
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
# A task-shaped line whose marker is neither ` ` nor `x`/`X` is counted as NOTHING: it is
# not a task (issue-sync's `_TASK` is `^- \[ \] (.+)$`, so no sub-issue is ever created for
# it), not done, and not tickable (phase-done's `_UNTICKED` rewrites `- [ ]` only, so the
# marker survives every phase-done forever). The work vanishes silently. `_TASK_ANY` only
# catches the case where a phase's tasks are ALL non-standard; one `[~]` beside one `[ ]`
# left nothing to fire at all, which is why this is a hard finding and not a warning.
# The fix is at lint time, deliberately: teaching `_TASK` to accept `[~]` would spawn
# sub-issues for half-done work, which is a different — and unrequested — behaviour.
# Anchored at column 0 exactly like `_TASK`/`_TASK_ANY`, so an INDENTED checkbox is out of
# scope here for the same reason it is out of scope there: it was never going to become a
# task, whatever its marker.
# Matched per UNFENCED line (`_unfenced_lines`), unlike `_TASK`/`_TASK_ANY`, which scan the
# raw section. That asymmetry is deliberate. Symmetry was the original argument — a column-0
# `- [ ] x` inside a fence really does become a sub-issue today, so a `[~]` there is a real
# inconsistency — but this check HARD-FAILS, and those do not. A plan documenting the
# rejected shape the obvious way (showing it in a fenced example) failed its own lint, which
# costs more than the misparse it mirrored: matching an existing SILENT bug does not justify
# a new BLOCKING one. Teaching `_TASK` about fences is a real fix with a different blast
# radius (it would stop creating sub-issues that today exist), so it is not made here.
_TASK_ODD_MARKER = re.compile(r"^- \[[^ xX\]\n]\] .+$")


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
        # The whole line, so the reason is greppable straight back to the source line.
        for line in _unfenced_lines(section):
            if _TASK_ODD_MARKER.match(line):
                reasons.append(f"phase-task-marker-unknown:{title}:{line.rstrip()}")
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
        "purpose, so an unrelated plan defect can never stop a headless fire. `-` reads "
        'the title from stdin, which is the form to use: real phase titles carry `"`, '
        "`|`, and backticks (this repo's own plan has two), and a model composing a "
        "shell command around one will eventually mis-quote it.",
    )
    args = p.parse_args(argv)
    try:
        with open(args.plan_md, encoding="utf-8") as f:
            text = f.read()
    except OSError as exc:
        print(f"plan-unreadable: {exc}", file=sys.stderr)
        return 2
    if args.phase is not None:
        # `-` = read the title from stdin. The ONLY form with no quoting failure mode,
        # which matters because the caller is a model composing a shell command around a
        # title it does not control. Trailing newline stripped; nothing else touched, so a
        # title is compared exactly as the issue and the heading spell it.
        phase = sys.stdin.read().rstrip("\r\n") if args.phase == "-" else args.phase
        reasons, refs = lint_phase_adrs(text, phase)
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
