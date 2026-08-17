# tests/conductor/test_skill_outputs.py
"""Contract tests for the conducted skills — needle PLUS the step that must contain it.

These asserted `needle in body` against a whole lowercased SKILL.md, which proves a phrase
exists somewhere in the file and nothing more. A refactor that moves an instruction out of
the step a worker executes and into trailing prose keeps every needle green while the
behavior is gone (issue #81; demonstrated on prepare's `**ADRs:**` backfill paragraph).

So each needle now names the region that must contain it. Regions are declared as an ORDERED
list of `(region_id, start_anchor)` per file; each region ends where the next one starts, and
the last runs to EOF. Only the START is in the data, never the successor — otherwise
inserting a step edits the key of the step before it, and every renumber churns unrelated
entries. Renumbering a step is a one-line anchor edit; renaming one fails loudly, which is a
real semantic change worth failing on.

Reserved ids: `@frontmatter`, `@preamble` (standing rules before step 1, always resident).
A needle anchored to `@preamble` also fails if it gets PROMOTED into a step — placement is
checked in both directions.
"""

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Ordered (region_id, start_anchor). Anchors are lowercase literals matched against the RAW
# lowercased text — line structure is load-bearing, so they are NOT whitespace-normalized.
# Every anchor is the full bolded step heading on purpose: short prefixes like `"3. "` are
# not unique (autodev's step 6 contains a nested recipe renumbered 0.-8.).
_REGIONS: dict[str, list[tuple[str, str]]] = {
    "skills/autodev/SKILL.md": [
        ("@frontmatter", "name: autodev"),
        ("@preamble", "# /conductor:autodev — one phase per fire (§8)"),
        ("1-reload-goal", "1. **re-load goal (fresh context).**"),
        (
            "1b-run-branch-current",
            "1b. **keep the run branch current (every fire, before anything else builds).**",
        ),
        ("2-reconcile", "2. **reconcile (precedence git/tests > pr > label).**"),
        ("3-spec-done-gate", "3. **spec-done gate.**"),
        (
            "3a-final-owner-pr",
            "**3a. open the final owner pr (run topology only — skip when no run branch is "
            "configured).**",
        ),
        ("3b-teardown", "**3b.** mark done"),
        ("4-pick-phase", "4. **pick the next eligible phase**"),
        (
            "4b-decisions-precondition",
            "4b. **decisions precondition — fail-closed, before the claim.**",
        ),
        ("5-claim", "5. **claim.**"),
        ("6-execute", "6. **execute the phase in a fresh subagent**"),
        ("7-escalation", "7. **escalation (§9):**"),
        ("8-record", "8. **record — mechanical, one command.**"),
        ("9-handoff", "9. **write handoff (§4)**"),
    ],
    "skills/start/SKILL.md": [
        (
            "@frontmatter",
            "description: start (or resume) an autonomous conductor run",
        ),
        ("@preamble", "# /conductor:start — preflight + set up + launch"),
        ("0-preflight", "0. **preflight (`conductor preflight`).**"),
        ("1-detect-spec", "1. **detect spec source**"),
        ("2-assertions-present", "2. **precondition — assertion specs present?**"),
        ("3-gate-dir", "3. **resolve the per-spec gate dir first:**"),
        ("4-plan", "4. **plan exists?**"),
        ("4b-lint-and-review", "4b. **lint + opposite-host review of the plan**"),
        ("5-issue-sync", "5. **issue-sync**"),
        ("5b-run-topology", "5b. **run topology (0.5.0 default):"),
        ("6-record-goal", "6. **record `/goal`**"),
        ("7-phase-2", "7. **(phase 2 only)**"),
        ("@trailer", "a restart = re-invoke `/conductor:start`"),
    ],
    "skills/assertions-to-tests/SKILL.md": [
        ("@preamble", "# /conductor:assertions-to-tests"),
        # 1 and 2 carry no needles today; they are declared anyway because every top-level
        # step MUST be, or its content gets absorbed into the region above it unchecked.
        ("1-pick-id", "1. **pick a stable `id`**"),
        ("2-write-test", "2. **write the test (it stays red).**"),
        ("3-red-team", "3. **red-team the test"),
        ("4-wire-in", "4. **wire it into the run"),
        ("5-verify-red", "5. **verify the gate sees it red"),
    ],
    "skills/prepare/SKILL.md": [
        ("@frontmatter", "---\nname: prepare"),
        (
            "@preamble",
            "# /conductor:prepare — brownfield alignment (owner-supervised)",
        ),
        ("0-inventory", "0. **inventory.**"),
        ("1-gate-integrity", "1. **gate integrity.**"),
        ("2-plan-evaluation", "2. **plan evaluation.**"),
        ("3-ledger-alignment", "3. **ledger alignment"),
        ("4-status-truth", "4. **status truth.**"),
        ("5-run-topology", "5. **run topology.**"),
        ("6-report", "6. **report — ready for `/conductor:start`.**"),
    ],
}

# region_id -> the needles that region must contain. A needle appearing in several regions is
# anchored to the LOAD-BEARING one: where a worker executing the skill has to read it, not
# where it is merely cross-referenced. Frontmatter `description:` never counts — no worker
# executes a description.
_CONTRACT: dict[str, dict[str, list[str]]] = {
    "skills/autodev/SKILL.md": {
        "@preamble": [
            "one phase",
            "ask no questions",
            "owner-owned",
            # A1: see the same needle on start — a worker that cannot resolve the CLI path
            # cannot do anything else in this file.
            "the directory this `skill.md` lives in",
        ],
        "1-reload-goal": [
            "re-load goal",
            "conductor remote",
            "run_branch",
        ],
        "1b-run-branch-current": [
            "keep the run branch current",
            "merge, never rebase",
        ],
        "2-reconcile": [
            "reconcile",
            "--from-gate",
            "no-progress-cap-exceeded",
        ],
        "3-spec-done-gate": ["assert run --level spec"],
        "3a-final-owner-pr": [
            "run-packet",
            "not with --admin, not at all",
        ],
        "3b-teardown": [
            "crondelete",
            "# conductor-autodev",
            "grep -f -v",
            # the equivalence prose above is not the instruction; pin the command
            "conductor resume-script uninstall-cron",
        ],
        "4b-decisions-precondition": [
            # the command itself and what a non-zero exit MUST do — vocabulary alone
            # can survive as negated or historical prose
            "conductor plan-lint <plan.md> --phase -",
            "do not claim, do not implement",
        ],
        "6-execute": [
            "fresh subagent",
            "normative spec",
            "never sufficient",
            "environment-provided",
            "reconcile-within-phase",
            "wip: reclaimed partial work",
            "base = the run branch",
            "conductor merge-gate",
            "conductor merge <pr>",
            # A1: the review is defined by INDEPENDENCE, not by a host name. These four pin
            # the whole leg: who reviews, that it is not you, that the marker default moves
            # with the host, and that the worker must read it rather than assume it. The old
            # needle was the bare phrase "codex review", which a Codex-hosted run satisfies
            # by reviewing itself.
            "opposite-host review",
            "the host you are not",
            "conductor_review_marker",
            "never assume the default",
            "usage-limit fallback",
            # the decisions leg: the ADRs must reach the worker, and bind it
            "**adrs:**",
            "an adr binds the phase exactly as its",
            "never quietly build against a closed decision",
            # The per-task test scope. Load-bearing and not self-evident: the sub-skill this
            # step delegates to does not scope testing, so an unscoped implementer runs the whole
            # project suite once per task. The full suite runs exactly once a phase, mechanically,
            # inside merge-gate — never in a worker turn.
            "never the project suite",
            # ...and the matching prohibition at the point the instinct fires: a worker that has
            # just finished the phase wants to verify before opening the PR, seconds before
            # merge-gate does it for free. Pin the refusal and the gate-is-not-the-suite line.
            "do not run the project test suite here",
            "gate-green says nothing about the project's own tests",
        ],
        "7-escalation": [
            "escalate.file_followup",
            # an ADR written on escalation is only half the job until it is cited
            "cite that adr",
        ],
        "8-record": ["phase-done"],
        "9-handoff": ["handoff"],
    },
    "skills/start/SKILL.md": {
        "@preamble": [
            # the idempotence contract has no numbered-step home; it governs every step
            "idempotent",
            "already done",
            # A1: the CLI path must be resolvable on a host that publishes no plugin-root
            # variable. `$CLAUDE_PLUGIN_ROOT` alone left a Codex worker unable to run the CLI
            # at all, so the host-independent fallback is the load-bearing half.
            "the directory this `skill.md` lives in",
        ],
        "0-preflight": [
            "preflight",
            # A1: the required set is the CHECKER's to state, not prose's. Pin the refusal to
            # re-list it and the reason the list is host-dependent.
            "do not re-list the required commands",
            "in your host's own invocation form",
            "opposite-host review wrapper",
        ],
        "2-assertions-present": ["spec-craft:executable-assertions"],
        "3-gate-dir": [
            "conductor:assertions-to-tests",
            "start_probe.assertions_ready",
            "conductor gate lint",
            "conductor gate freeze",
        ],
        "4-plan": [
            "the plan builds to the spec",
            "done-floor",
            "normative spec:",
            # the plan MUST carry the decisions pointer, and `none` is the explicit answer
            "adr dir",
            "**adrs:**",
            "**adrs:** none",
            # A1: the recipe the plan must carry, and the two host-derived things in it. The
            # reviewer NAME is what `conductor plan-lint` greps for, so the instruction to
            # write it — and to write the OTHER host's — has to reach the plan author.
            "opposite-host review ×2",
            "write the reviewer's host name into the recipe",
            "recipe-missing:<host>",
            "opposite-host review judges their **substance**",
        ],
        "4b-lint-and-review": [
            "plan-lint",
            "opposite-host review",
            "on the host you are not",
        ],
        "5-issue-sync": ["issue-sync"],
        "5b-run-topology": [
            "reconcile-first",
            "run topology",
            "conductor run-branch name",
            "run_branch",
            "worktree",
            "conductor_allow_direct_main_merge=1",
            "base-mismatch",
        ],
        "6-record-goal": [
            "croncreate",
            "/conductor:autodev",
            "verify durability",
            "flock",
            "resume-script write",
            "conductor resume-script",
            "resume-env.sh",
            "resume-script verify",
            "# conductor-autodev",
        ],
    },
    "skills/assertions-to-tests/SKILL.md": {
        "@preamble": ["<spec>.assertions.md"],
        "3-red-team": [
            "red-team",
            "worse than none",
            "exists-but-unused",
        ],
        "4-wire-in": [
            "pytest_disable_plugin_autoload=1",
            "--noconftest",
            "no:cacheprovider",
            "self-contained",
        ],
    },
    "skills/prepare/SKILL.md": {
        "@preamble": [
            "brownfield",
            "owner-supervised",
            "the directory this `skill.md` lives in",
        ],
        "1-gate-integrity": ["gate verify"],
        "2-plan-evaluation": [
            "plan-lint",
            "opposite-host review",
            "on the host you are not",
            # the mechanical migration path for the required **ADRs:** line
            "backfill",
            "**adrs:** none",
            "phase-no-adr-pointer",
        ],
        "3-ledger-alignment": [
            "dry-run first",
            "assertion-id set",
            "conductor ledger align <plan.md> --apply",
            "conductor ledger convert <plan.md>",
            "never guess",
        ],
        "4-status-truth": [
            "--from-gate",
            "phase-done",
            "status:draft",
        ],
        "5-run-topology": ["run topology"],
        "6-report": ["ready for `/conductor:start`"],
    },
}


# Substrings that must NOT appear anywhere in the file, lowercased. The mirror image of
# _CONTRACT and necessary for the same reason: a positive needle proves the new instruction is
# present, but not that the old one is GONE, and both surviving is the worst outcome — a worker
# reading a file that tells it to do the host-specific thing two paragraphs after telling it to
# do the host-neutral one. Every entry below is the exact literal the pre-A1 text carried, so
# reverting any hunk restores it and fails here.
_FORBIDDEN: dict[str, list[str]] = {
    # `$CLAUDE_PLUGIN_ROOT/bin/conductor` was the ONLY way each of these four named the CLI. On
    # a host that publishes no such variable it expands to `/bin/conductor`, which either does
    # not exist or is not conductor.
    "skills/start/SKILL.md": [
        "$claude_plugin_root/bin/conductor",
        # the Claude slash form of the opposite-host wrapper, hardcoded in the preflight list
        # and in the plan's per-phase recipe. `/.codex/` does not contain `/codex`.
        "/codex",
        "codex-review",
    ],
    "skills/autodev/SKILL.md": [
        "$claude_plugin_root/bin/conductor",
        "/codex",
        "codex re-reviews",
        "codex-fallback review",
    ],
    "skills/prepare/SKILL.md": [
        "$claude_plugin_root/bin/conductor",
        "codex-review",
    ],
    "skills/issue-sync/SKILL.md": [
        "$claude_plugin_root/bin/conductor",
    ],
}


# A top-level step heading: `4.` / `4b.` at column 0. The executable spine of a skill.
# Column 0 is what distinguishes it from a SUB-step — autodev indents `**3a.`/`**3b.` and
# its whole step-6 recipe by three spaces, and those legitimately belong to their parent.
_TOP_STEP = re.compile(r"(?m)^\d+[a-z]?\.")


def _line_start(raw: str, i: int) -> bool:
    """True when only whitespace separates `i` from the start of its line.

    An anchor that stops being a heading — pulled into a blockquote, a code fence, or the
    middle of a sentence — must stop anchoring, or the region silently keeps its name while
    its boundary has moved."""
    return raw[raw.rfind("\n", 0, i) + 1 : i].strip() == ""


def _regions(path: str) -> dict[str, str]:
    """region_id -> that region's text, whitespace-normalized.

    Normalized because these files are hard-wrapped at ~100 columns, so any needle spanning
    a line break could never match otherwise (`an adr binds the phase exactly as its` in
    autodev is one wrap away from being unassertable). Anchors are located in the RAW text
    first, since they are line-anchored headings.

    The boundaries are cross-checked against the headings actually present in the file, not
    just against `_REGIONS`. Trusting the declaration alone reopens #81 from the other side:
    an UNDECLARED step is silently absorbed into the region above it, so an instruction can
    leave the executable step while staying inside its nominal slice. Codex demonstrated it
    on this very refactor — inserting `3a. **Historical compatibility note — do not
    execute.**` into start let the real `conductor gate freeze` operation be deleted from
    step 3 with every test still green."""
    raw = open(os.path.join(ROOT, path), encoding="utf-8").read().lower()
    starts: list[tuple[str, int]] = []
    for rid, anchor in _REGIONS[path]:
        count = raw.count(anchor)
        # A duplicated anchor would silently slice the wrong region via first-match-wins.
        assert count == 1, (
            f"{path}: anchor for {rid} occurs {count}x, need 1: {anchor!r}"
        )
        i = raw.index(anchor)
        assert _line_start(raw, i), (
            f"{path}: anchor for {rid} is no longer at the start of a line — it has stopped "
            f"being a heading: {anchor!r}"
        )
        starts.append((rid, i))
    for (a, i), (b, j) in zip(starts, starts[1:]):
        assert i < j, f"{path}: region {a} must come before {b}; _REGIONS is misordered"
    declared = {i for _, i in starts}
    undeclared = [
        f"line {raw[: m.start()].count(chr(10)) + 1}: {raw[m.start() : m.start() + 60]!r}"
        for m in _TOP_STEP.finditer(raw)
        if m.start() not in declared
    ]
    assert not undeclared, (
        f"{path}: top-level step(s) not declared in _REGIONS, so their content is being "
        f"absorbed into the region above and is unchecked — add them:\n    "
        + "\n    ".join(undeclared)
    )
    out: dict[str, str] = {}
    for k, (rid, i) in enumerate(starts):
        end = starts[k + 1][1] if k + 1 < len(starts) else len(raw)
        # The slice INCLUDES its own anchor — several needles are the step heading itself.
        out[rid] = re.sub(r"\s+", " ", raw[i:end])
    return out


def _assert_contract(path: str) -> None:
    """Two-stage: absent from the file and present-but-misplaced are different defects with
    different fixes, so they get different messages. Every failure for the file is collected
    and reported at once — one assert per needle hides the rest behind the first."""
    regions = _regions(path)
    failures: list[str] = []
    for rid, needles in _CONTRACT[path].items():
        assert rid in regions, f"{path}: _CONTRACT names unknown region {rid}"
        for needle in needles:
            if needle in regions[rid]:
                continue
            # Search each region separately, never a joined string: concatenation can
            # synthesize a match across a boundary and mislabel a MISSING needle MISPLACED.
            found = [r for r, text in regions.items() if needle in text]
            if not found:
                failures.append(f"MISSING  {needle!r} — not in {path} at all")
                continue
            failures.append(
                f"MISPLACED {needle!r} — expected in {rid}, found in {', '.join(found)}"
            )
    if failures:
        pytest.fail(f"{path}\n  " + "\n  ".join(failures), pytrace=False)


def test_autodev_skill_contract():
    _assert_contract("skills/autodev/SKILL.md")


def test_start_skill_contract():
    _assert_contract("skills/start/SKILL.md")
    # The frozen assertion a8-gate-freeze-needle-present pins this needle as a LITERAL
    # inside this function — it greps for a quoted `gate freeze` line in
    # `def test_start_skill_contract`'s body so the freeze step cannot rot out of the
    # skill. Its own file is digest-locked in assertions/.frozen, so the check is not
    # ours to relax; the restatement lives here instead. _CONTRACT remains the source of
    # truth and already covers this needle; scoping it to the region is strictly stronger
    # than the file-wide form A8 was written against. Do not delete: the gate runner
    # fail-closes when a frozen assertion's check goes missing.
    for needle in [
        "conductor gate freeze",
    ]:
        assert needle in _regions("skills/start/SKILL.md")["3-gate-dir"], needle


def test_assertions_to_tests_skill_contract():
    _assert_contract("skills/assertions-to-tests/SKILL.md")


def test_prepare_skill_contract():
    _assert_contract("skills/prepare/SKILL.md")


def test_every_declared_region_is_reachable():
    """_REGIONS is the load-bearing data — a stale anchor there silently mis-slices every
    needle downstream of it, so validate the whole declaration independently of any needle."""
    for path in _REGIONS:
        regions = _regions(path)
        assert [rid for rid, _ in _REGIONS[path]] == list(regions)
        # Measure the BODY, not the slice: every slice includes its own heading, so a
        # gutted step still clears a raw length check on its own markup alone. Applied to
        # numbered steps only — `@preamble` and `@trailer` are legitimately terse.
        anchors = dict(_REGIONS[path])
        empty = []
        for rid, text in regions.items():
            if rid.startswith("@"):
                assert text.strip(), f"{path}: {rid} sliced to nothing"
                continue
            body = text.replace(re.sub(r"\s+", " ", anchors[rid]), "", 1)
            if len(re.sub(r"[*`_#>\-\s]", "", body)) < 40:
                empty.append(rid)
        assert not empty, f"{path}: step(s) with no substantive body — {empty}"


def test_adr_precondition_lives_in_autodevs_pre_claim_step():
    # The one instruction that must run BEFORE the claim; misplacement here means a worker
    # starts a phase whose decisions nothing carries.
    precondition = _regions("skills/autodev/SKILL.md")["4b-decisions-precondition"]
    assert "--phase -" in precondition
    assert "before the claim" in precondition
    assert "/conductor:prepare" in precondition
    execute = _regions("skills/autodev/SKILL.md")["6-execute"]
    assert "build within the decisions" in execute


@pytest.mark.parametrize("path", sorted(_FORBIDDEN))
def test_no_skill_still_carries_a_pre_a1_host_specific_form(path):
    raw = open(os.path.join(ROOT, path), encoding="utf-8").read().lower()
    present = [needle for needle in _FORBIDDEN[path] if needle in raw]
    assert not present, (
        f"{path} still contains host-specific text A1 removed: {present}. A worker on the "
        f"other host cannot execute it."
    )


# issue-sync carries exactly one host-coupled line — the CLI path note — and no numbered
# executable steps (its `1.` / `2.` lines are enumerations inside prose sections, which the
# `_REGIONS` machinery would mis-read as undeclared steps). One targeted test is the
# proportionate pin; adding it to `_REGIONS` would mean declaring nine prose headings as
# executable steps to check a single blockquote.
def test_issue_syncs_cli_path_note_resolves_on_a_host_without_a_plugin_root_variable():
    raw = (
        open(os.path.join(ROOT, "skills/issue-sync/SKILL.md"), encoding="utf-8")
        .read()
        .lower()
    )
    note = raw[raw.index("**conductor cli path:**") :].split("\n\n", 1)[0]
    assert "the directory this `skill.md` lives in" in note
    assert "bin/conductor" in note


def test_adr_backfill_lives_in_prepares_plan_evaluation_step():
    evaluation = _regions("skills/prepare/SKILL.md")["2-plan-evaluation"]
    assert "backfill" in evaluation
    assert "**adrs:** none" in evaluation
    assert "dry-run" in evaluation
