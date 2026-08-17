import glob
import io
import os

import pytest

from conductor import plan_lint

GOOD_PLAN = """\
# Widget Harness — Implementation Plan

**Normative spec:** docs/specs/widget-spec.md
**Assertion specs:** docs/specs/widget-spec.assertions.md

Per-phase cycle: implement via subagent → /code-review per task → commit per task →
one PR per phase (`Closes #<phase-issue>`) → codex review ×2 → `conductor merge-gate`
→ merge → `conductor ledger phase-done`.

## Phase 1 — Scoring (A3, A4)

**Spec:** §6 Metrics; §7 Scoring & Decision Rule
**ADRs:** ADR-004 Scoring rule is frozen; ADR-011 No per-tenant weights

- [ ] Write failing tests
- [ ] Implement scoring

## Phase 2 — Reporting (A8)

**Spec:** §10 Sample Report
**ADRs:** none

- [ ] Implement report
"""


def test_good_plan_is_clean():
    assert plan_lint.lint(GOOD_PLAN) == []


def test_good_plan_clean_with_matching_spec_path():
    assert plan_lint.lint(GOOD_PLAN, spec_path="docs/specs/widget-spec.md") == []


def test_missing_normative_header():
    text = GOOD_PLAN.replace("**Normative spec:** docs/specs/widget-spec.md\n", "")
    assert "normative-spec-missing" in plan_lint.lint(text)


def test_named_spec_not_referenced():
    reasons = plan_lint.lint(GOOD_PLAN, spec_path="docs/specs/other-spec.md")
    assert "spec-not-referenced:other-spec.md" in reasons


def test_no_phases():
    text = "# T\n\n**Normative spec:** s.md\n\ncodex /code-review merge-gate closes #\n"
    assert "no-phases" in plan_lint.lint(text)


def test_phase_without_tasks_flagged():
    text = GOOD_PLAN.replace("- [ ] Implement report\n", "")
    reasons = plan_lint.lint(text)
    assert "phase-no-tasks:Phase 2 — Reporting (A8)" in reasons


def test_phase_without_spec_pointer_flagged():
    text = GOOD_PLAN.replace("**Spec:** §10 Sample Report\n", "")
    reasons = plan_lint.lint(text)
    assert "phase-no-spec-pointer:Phase 2 — Reporting (A8)" in reasons


def test_missing_recipe_needles_reported_individually():
    text = GOOD_PLAN.replace("codex review ×2", "peer review").replace(
        "`conductor merge-gate`\n", ""
    )
    reasons = plan_lint.lint(text)
    assert "recipe-missing:codex" in reasons
    assert "recipe-missing:merge-gate" in reasons
    assert "recipe-missing:/code-review" not in reasons


def test_recipe_needles_case_insensitive():
    text = GOOD_PLAN.replace("codex review ×2", "CODEX review ×2")
    assert all(not r.startswith("recipe-missing:codex") for r in plan_lint.lint(text))


def test_old_dialect_phase_headings_also_lint():
    text = (
        "# T\n\n**Normative spec:** s.md\n\n"
        "codex /code-review merge-gate closes #\n\n"
        "## Backend [ready]\n\n**Spec:** §2\n**ADRs:** none\n\ngate: none\n\n"
        "- [ ] build it\n"
    )
    assert plan_lint.lint(text) == []


def test_spec_intent_annotated_pointer_accepted():
    # The dialect that emerged in the first live run (ai-platform plan, commit 856ca61).
    text = GOOD_PLAN.replace(
        "**Spec:** §10 Sample Report",
        "**Spec intent — REQUIRED READING (build to these, not just A8):**",
    )
    assert plan_lint.lint(text) == []


def test_phase_without_assertion_ids_flagged():
    # codex PR-28 #1: a gateless phase breaks --from-gate/phase-done downstream; it must
    # be deliberate, not accidental.
    text = GOOD_PLAN.replace(" (A8)", "")
    reasons = plan_lint.lint(text)
    assert "phase-no-assertions:Phase 2 — Reporting" in reasons


def test_gate_none_escape_hatch_for_deliberate_gateless_phase():
    text = GOOD_PLAN.replace(" (A8)", "").replace(
        "**Spec:** §10 Sample Report",
        "**Spec:** §10 Sample Report\n\ngate: none",
    )
    assert plan_lint.lint(text) == []


def test_lint_is_a_presence_floor_not_a_position_check():
    # Pins the smoke-check semantics (codex PR-28 #4): needles anywhere satisfy the lint;
    # SUBSTANCE is the plan codex-review's job (start step 4b runs both).
    reordered = GOOD_PLAN.replace(
        "Per-phase cycle: implement via subagent → /code-review per task → commit per task →\n"
        "one PR per phase (`Closes #<phase-issue>`) → codex review ×2 → `conductor merge-gate`\n"
        "→ merge → `conductor ledger phase-done`.",
        "notes: /code-review, codex, merge-gate, Closes #",
    )
    assert plan_lint.lint(reordered) == []


def test_completed_phase_with_all_ticked_tasks_is_not_flagged():
    # Live-run finding (2026-07-02): phases 1-3 of the ai-platform plan are done, all
    # boxes [x] -> the lint fired phase-no-tasks forever on a legitimately in-progress
    # plan. A phase whose tasks are all checked HAS tasks.
    text = GOOD_PLAN.replace(
        "- [ ] Write failing tests", "- [x] Write failing tests"
    ).replace("- [ ] Implement scoring", "- [x] Implement scoring")
    reasons = plan_lint.lint(text)
    assert not any(r.startswith("phase-no-tasks:") for r in reasons)
    assert reasons == []


def test_blank_checkbox_line_is_not_a_task():
    # codex PR-30 #1: `- [x] ` with no text must not satisfy phase-no-tasks.
    text = GOOD_PLAN.replace("- [ ] Implement report", "- [x] ")
    reasons = plan_lint.lint(text)
    assert "phase-no-tasks:Phase 2 — Reporting (A8)" in reasons


# --- non-standard checkbox markers -----------------------------------------------------
# `- [~] something` is counted as NOTHING: issue-sync's `_TASK` (`^- \[ \] (.+)$`) never
# sees it, so no sub-issue is ever created for it, and phase-done's `_UNTICKED` rewrites
# `- [ ]` only, so it can never be ticked either. It vanishes silently. Rejecting it at
# lint time is the fix — teaching `_TASK` to accept `[~]` would spawn sub-issues for
# half-done work instead.

_MARKER = "phase-task-marker-unknown:"


def test_partial_marker_beside_a_valid_task_is_flagged():
    # The exact hole: `_TASK_ANY` still matches the SIBLING `- [ ]` line, so phase-no-tasks
    # stays quiet and the `[~]` line is dropped with nothing said about it.
    text = GOOD_PLAN.replace("- [ ] Implement scoring", "- [~] Implement scoring")
    reasons = plan_lint.lint(text)
    assert f"{_MARKER}Phase 1 — Scoring (A3, A4):- [~] Implement scoring" in reasons, (
        reasons
    )
    assert not any(r.startswith("phase-no-tasks:") for r in reasons)


def test_partial_marker_is_a_hard_finding_not_a_warning(tmp_path, capsys):
    # A silently-dropped task is a correctness problem: exit 1, on stderr, no `warn:`.
    plan = tmp_path / "plan.md"
    plan.write_text(
        GOOD_PLAN.replace("- [ ] Implement report", "- [~] Implement report")
    )
    assert plan_lint.main([str(plan)]) == 1
    err = capsys.readouterr().err
    assert f"{_MARKER}Phase 2 — Reporting (A8):- [~] Implement report" in err
    assert "warn:phase-task" not in err


def test_every_non_standard_marker_is_flagged_not_just_tilde():
    for marker in ("~", "-", ">", "?", "/"):
        text = GOOD_PLAN.replace(
            "- [ ] Implement report", f"- [{marker}] Implement report"
        )
        reasons = plan_lint.lint(text)
        assert (
            f"{_MARKER}Phase 2 — Reporting (A8):- [{marker}] Implement report"
            in reasons
        ), marker


def test_all_partial_phase_reports_no_tasks_once_plus_one_finding_per_line():
    # An all-`[~]` phase already fires phase-no-tasks; the marker findings must ADD the
    # line-level detail, not restate the phase-level one a second time.
    text = GOOD_PLAN.replace(
        "- [ ] Write failing tests", "- [~] Write failing tests"
    ).replace("- [ ] Implement scoring", "- [~] Implement scoring")
    reasons = plan_lint.lint(text)
    title = "Phase 1 — Scoring (A3, A4)"
    assert [r for r in reasons if r.startswith("phase-no-tasks:")] == [
        f"phase-no-tasks:{title}"
    ]
    assert [r for r in reasons if r.startswith(_MARKER)] == [
        f"{_MARKER}{title}:- [~] Write failing tests",
        f"{_MARKER}{title}:- [~] Implement scoring",
    ]


def test_standard_markers_and_checklist_refs_produce_no_marker_finding():
    # `[ ]`, `[x]`, `[X]`, and phase_done's `- [ ] #123` checklist refs are all legitimate.
    text = GOOD_PLAN.replace("- [ ] Write failing tests", "- [x] Write failing tests")
    text = text.replace(
        "- [ ] Implement scoring", "- [X] Implement scoring\n- [ ] #123"
    )
    assert plan_lint.lint(text) == []


def test_conductors_own_superpowers_plans_carry_no_marker_finding():
    # Guards against a check so broad it fails plans already committed to this repo.
    for name in (
        "2026-08-10-plan-01-run-identity-registry.md",
        "2026-08-10-plan-04-host-adapters.md",
    ):
        path = os.path.join(ROOT, "docs", "superpowers", "plans", name)
        text = open(path, encoding="utf-8").read()
        assert [r for r in plan_lint.lint(text) if r.startswith(_MARKER)] == [], name


# --- fenced code is not plan content (codex production review, finding 5) ---------------
# The marker check is a HARD failure, and a plan that documents the rejected shape by
# showing it — the obvious way to write "do not do this" — hard-failed on its own example.
# `_TASK_ANY`/`_TASK` are fence-blind and stay that way (a column-0 `- [ ]` inside a fence
# really does become a sub-issue today); matching an existing SILENT misparse does not
# justify a new BLOCKING false positive, so only the marker check learns fences here.


def _fenced(open_fence: str, close_fence: str, body: str = "- [~] documented example"):
    """GOOD_PLAN with a fenced example appended to its last phase (Phase 2)."""
    return (
        f"{GOOD_PLAN}\nReviewers must reject this shape:\n\n"
        f"{open_fence}\n{body}\n{close_fence}\n"
    )


def test_odd_marker_inside_a_backtick_fence_is_not_flagged():
    assert plan_lint.lint(_fenced("```", "```")) == []


@pytest.mark.parametrize("info", ["text", "markdown", "md title=example"])
def test_fence_info_string_does_not_defeat_the_fence(info):
    assert plan_lint.lint(_fenced(f"```{info}", "```")) == [], info


def test_odd_marker_inside_a_tilde_fence_is_not_flagged():
    assert plan_lint.lint(_fenced("~~~text", "~~~")) == []


def test_a_backtick_line_does_not_close_a_tilde_fence():
    # Different fence chars are independent; a plan showing a ``` fence inside a ~~~ one
    # must not have its example spill back into plan content halfway through.
    text = _fenced("~~~text", "~~~", body="```\n- [~] documented example\n```")
    assert plan_lint.lint(text) == []


def test_a_shorter_run_does_not_close_a_longer_fence():
    # The reason the length rule exists: documenting a ``` fence needs a ```` wrapper.
    text = _fenced("````text", "````", body="```text\n- [~] documented example\n```")
    assert plan_lint.lint(text) == []


def test_odd_marker_outside_the_fence_is_still_flagged_in_the_same_phase():
    # The check must survive the fix: fencing one example must not blind the phase.
    text = _fenced("```text", "```").replace(
        "- [ ] Implement report", "- [ ] Implement report\n- [?] real dropped task"
    )
    assert plan_lint.lint(text) == [
        f"{_MARKER}Phase 2 — Reporting (A8):- [?] real dropped task"
    ]


def test_a_real_task_outside_a_fence_still_parses():
    text = _fenced("```text", "```")
    assert plan_lint.lint(text) == []
    # ...and it is the UNFENCED task doing that work, not the fenced line.
    stripped = text.replace("- [ ] Implement report\n", "")
    assert "phase-no-tasks:Phase 2 — Reporting (A8)" in plan_lint.lint(stripped)


def test_an_unterminated_fence_runs_to_the_end_of_its_phase():
    # CommonMark closes an unclosed fence at the end of its container, and this check's
    # whole premise is that a false hard-fail costs more than a missed finding. Bounded to
    # the phase because every marker scan is per-section.
    text = GOOD_PLAN + "\n```text\n- [~] documented example\n"
    assert plan_lint.lint(text) == []


def test_an_unterminated_fence_does_not_leak_into_the_next_phase():
    text = GOOD_PLAN.replace(
        "- [ ] Implement scoring",
        "- [ ] Implement scoring\n\n```text\n- [~] documented example",
    ).replace(
        "- [ ] Implement report", "- [ ] Implement report\n- [~] real dropped task"
    )
    assert plan_lint.lint(text) == [
        f"{_MARKER}Phase 2 — Reporting (A8):- [~] real dropped task"
    ]


def test_fenced_marker_does_not_fail_the_cli(tmp_path):
    plan = tmp_path / "plan.md"
    plan.write_text(_fenced("```text", "```"))
    assert plan_lint.main([str(plan)]) == 0


# --- a fenced H2 is not a phase (codex round 2, finding 3) -------------------------------
# `_phase_sections` split on every H2 BEFORE the fence filter ran, so a fenced example that
# showed a phase heading was parsed as a REAL phase: the marker finding came back, joined by
# every per-phase pointer failure the example could not possibly satisfy. Fence awareness has
# to happen during the split, not after it.

_FENCED_PHASE_EXAMPLE = "## Phase example (A99)\n\n- [~] documented example"


def test_a_phase_shaped_heading_inside_a_fence_is_not_a_phase():
    assert plan_lint.lint(_fenced("```md", "```", body=_FENCED_PHASE_EXAMPLE)) == []


def test_a_fenced_phase_heading_is_not_claimable_by_title():
    # `lint_phase_adrs` is autodev's pre-claim check and splits with the same helper: an
    # example must not be claimable as a phase.
    reasons, refs = plan_lint.lint_phase_adrs(
        _fenced("```md", "```", body=_FENCED_PHASE_EXAMPLE), "Phase example (A99)"
    )
    assert reasons == ["phase-not-found:Phase example (A99)"]
    assert refs == []


def test_a_real_phase_after_a_fenced_phase_heading_is_still_linted():
    # The fix must not blind the splitter — a REAL phase following the example still parses,
    # and still reports its own failures.
    text = (
        _fenced("```md", "```", body=_FENCED_PHASE_EXAMPLE)
        + "\n## Phase 3 — Glue (A9)\n\n- [ ] wire it up\n"
    )
    assert sorted(plan_lint.lint(text)) == [
        "phase-no-adr-pointer:Phase 3 — Glue (A9)",
        "phase-no-spec-pointer:Phase 3 — Glue (A9)",
    ]


def test_an_unterminated_fence_does_not_hide_the_headings_after_it():
    # The asymmetry the splitter needs: a CLOSED fence hides a heading, an unclosed one does
    # not. Reading the whole plan with one fence state would let a forgotten ``` swallow every
    # following heading, merging the rest of the file into the phase that opened it — the
    # already-committed leak test above is the same rule seen from the marker side.
    text = GOOD_PLAN.replace(
        "- [ ] Implement scoring",
        "- [ ] Implement scoring\n\n```md\n- [~] documented example",
    )
    titles = [t for (t, _s, _i), _sec in plan_lint._phase_sections(text)]
    assert titles == ["Phase 1 — Scoring (A3, A4)", "Phase 2 — Reporting (A8)"]


# --- an info string is not free text (codex round 2, finding 4) --------------------------
# CommonMark §4.5: a BACKTICK fence's info string may not contain a backtick (the line is
# inline code, not a fence); a TILDE fence's info string may. Accepting arbitrary info text
# let a non-fence line open a fence in this parser and suppress every following marker finding
# to the end of the phase — a false negative introduced by the fence fix itself.


def test_a_backtick_in_a_backtick_fence_info_string_opens_no_fence():
    text = _fenced("```md`not-an-opener", "```")
    assert plan_lint.lint(text) == [
        f"{_MARKER}Phase 2 — Reporting (A8):- [~] documented example"
    ]


def test_a_backtick_in_a_tilde_fence_info_string_still_opens_a_fence():
    # The other half of §4.5 — the rule is backtick-specific, so a tilde fence keeps working.
    assert plan_lint.lint(_fenced("~~~md`still-an-opener", "~~~")) == []


def test_a_run_of_backticks_after_the_opener_is_not_an_info_string():
    # ```` ``` ```` is the shape a plan uses to show a fence; the info string carries
    # backticks, so it is not an opener either.
    text = _fenced("```` ```", "````")
    assert plan_lint.lint(text) == [
        f"{_MARKER}Phase 2 — Reporting (A8):- [~] documented example"
    ]


def test_committed_plan_phase_titles_are_unchanged_by_fence_awareness():
    # The plan in this repo that HAS phases, so "don't change the split for plans without
    # fenced H2s" is checked against a real file and not only against fixtures.
    path = os.path.join(ROOT, "docs", "plans", "2026-07-06-plan-self-enforcement.md")
    text = open(path, encoding="utf-8").read()
    titles = [t for (t, _s, _i), _sec in plan_lint._phase_sections(text)]
    assert [t.split(" (")[0] for t in titles] == [
        "Phase 1 — Session-mode-aware unattended authority",
        'Phase 2 — README "Unattended authority" + canonical bypass spelling',
        "Phase 3 — Posture visibility in the generated driver",
        "Phase 4 — conductor gate lint + freeze covers the assertions source",
        "Phase 5 — Single-sourced identifiers: run-branch name + default-branch",
        "Phase 6 — conductor driver install|status",
    ]


# --- **ADRs:** pointer line (0.9.0) ----------------------------------------------------
# Live finding 2026-08-01: two architectural decisions existed ONLY in ADRs, so nothing
# carried them to a worker resuming a later phase, which could undo either while the
# done-gate stayed green. The line is required so silence and "none apply" look different.


def test_phase_without_adr_pointer_flagged():
    text = GOOD_PLAN.replace("**ADRs:** none\n", "")
    reasons = plan_lint.lint(text)
    assert "phase-no-adr-pointer:Phase 2 — Reporting (A8)" in reasons


def test_adrs_none_is_valid_and_explicit():
    # GOOD_PLAN's phase 2 already says `none`; the whole plan lints clean.
    assert plan_lint.lint(GOOD_PLAN) == []
    annotated = GOOD_PLAN.replace(
        "**ADRs:** none", "**ADRs:** none (no closed decision constrains reporting)"
    )
    assert plan_lint.lint(annotated) == []


def test_well_formed_id_lists_pass():
    for value in (
        "ADR-012",
        "ADR-012; ADR-016",
        "ADR-012 Shared retrieval spine, revisited; adr 7",
        "docs/adr/0001-drift-to-mop-query-path.md",
        "ADR-012; docs/adr/ADR-016-context-budget.md",
    ):
        text = GOOD_PLAN.replace("**ADRs:** none", f"**ADRs:** {value}")
        assert plan_lint.lint(text) == [], value


def test_malformed_adr_references_flagged():
    for value in ("TBD", "see the design doc", "ADR-", "ADR-XYZ"):
        text = GOOD_PLAN.replace("**ADRs:** none", f"**ADRs:** {value}")
        reasons = plan_lint.lint(text)
        assert f"phase-adr-malformed:Phase 2 — Reporting (A8):{value}" in reasons, value


def test_one_bad_fragment_in_a_list_is_flagged_alone():
    text = GOOD_PLAN.replace("**ADRs:** none", "**ADRs:** ADR-012; TBD the other one")
    reasons = plan_lint.lint(text)
    assert reasons == ["phase-adr-malformed:Phase 2 — Reporting (A8):TBD the other one"]


def test_empty_adr_line_is_not_compliance():
    # The line present with no value is silence wearing the line's clothes.
    text = GOOD_PLAN.replace("**ADRs:** none", "**ADRs:**")
    reasons = plan_lint.lint(text)
    assert "phase-adr-empty:Phase 2 — Reporting (A8)" in reasons


def test_adr_line_does_not_satisfy_the_spec_pointer():
    # Two independent bindings: dropping Spec must still fail even with ADRs present.
    text = GOOD_PLAN.replace("**Spec:** §10 Sample Report\n", "")
    reasons = plan_lint.lint(text)
    assert "phase-no-spec-pointer:Phase 2 — Reporting (A8)" in reasons
    assert not any(r.startswith("phase-no-adr-pointer") for r in reasons)


def test_annotated_adr_pointer_accepted():
    # Same annotated dialect the Spec pointer grew.
    text = GOOD_PLAN.replace(
        "**ADRs:** none",
        "**ADRs — REQUIRED READING (these are closed; do not relitigate):** ADR-012",
    )
    assert plan_lint.lint(text) == []


def _plan_citing(value: str) -> str:
    """GOOD_PLAN with `value` as the ONLY ADR reference in the whole plan."""
    return GOOD_PLAN.replace(
        "**ADRs:** ADR-004 Scoring rule is frozen; ADR-011 No per-tenant weights",
        "**ADRs:** none",
    ).replace(
        "**ADRs:** none\n\n- [ ] Implement report",
        f"**ADRs:** {value}\n\n- [ ] Implement report",
    )


def test_dangling_id_warns_when_the_repo_has_an_adr_dir(tmp_path):
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "ADR-012-shared-retrieval-spine.md").write_text("x")
    warns = plan_lint.adr_warnings(_plan_citing("ADR-012; ADR-999"), str(tmp_path))
    assert warns == ["warn:phase-adr-dangling:Phase 2 — Reporting (A8):ADR-999"]


def test_dangling_path_warns_only_when_its_dir_exists(tmp_path):
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    ref = "docs/adr/ADR-012-shared-retrieval-spine.md"
    assert plan_lint.adr_warnings(_plan_citing(ref), str(tmp_path)) == [
        f"warn:phase-adr-dangling:Phase 2 — Reporting (A8):{ref}"
    ]
    elsewhere = "docs/decisions/ADR-012.md"
    assert plan_lint.adr_warnings(_plan_citing(elsewhere), str(tmp_path)) == []


def test_unpadded_and_padded_ids_resolve_to_the_same_adr(tmp_path):
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "0001-drift-to-mop-query-path.md").write_text("x")
    assert plan_lint.adr_warnings(_plan_citing("ADR-1"), str(tmp_path)) == []


def test_no_adr_dir_means_no_id_warnings(tmp_path):
    # Plans are routinely written before the ADRs land — unverifiable is not dangling.
    assert plan_lint.adr_warnings(_plan_citing("ADR-999"), str(tmp_path)) == []


def test_dangling_warning_never_fails_the_lint(tmp_path):
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    text = _plan_citing("ADR-999")
    assert plan_lint.adr_warnings(text, str(tmp_path)) != []
    assert plan_lint.lint(text) == []


def test_main_prints_warnings_but_exits_zero(tmp_path, capsys):
    (tmp_path / "docs" / "adr").mkdir(parents=True)
    plan = tmp_path / "plan.md"
    plan.write_text(_plan_citing("ADR-999"))
    assert plan_lint.main([str(plan)]) == 0
    assert "warn:phase-adr-dangling:" in capsys.readouterr().err


def test_main_exits_one_on_a_missing_adr_line(tmp_path, capsys):
    plan = tmp_path / "plan.md"
    plan.write_text(GOOD_PLAN.replace("**ADRs:** none\n", ""))
    assert plan_lint.main([str(plan)]) == 1
    assert "phase-no-adr-pointer:" in capsys.readouterr().err


# --- codex PR-80 review: the value grammar's holes ------------------------------------
# Every case below lint()ed CLEAN before the fix, so the line could be present, required,
# and still say nothing — the exact defect the required line exists to remove.


def test_delimiters_only_are_not_an_answer():
    for value in (";", ";;", " ; ", "**"):
        text = GOOD_PLAN.replace("**ADRs:** none", f"**ADRs:** {value}")
        reasons = plan_lint.lint(text)
        assert "phase-adr-empty:Phase 2 — Reporting (A8)" in reasons, value


def test_trailing_semicolon_after_a_real_reference_still_passes():
    # A stray delimiter is a typo, not silence, once something real is on the line.
    text = GOOD_PLAN.replace("**ADRs:** none", "**ADRs:** ADR-012;")
    assert plan_lint.lint(text) == []


def test_none_beside_a_reference_is_a_contradiction():
    # "no decision applies" AND "this decision applies" cannot both hold; before the fix
    # `^none` matched and everything after it was discarded silently.
    for value in ("none; ADR-999", "none; TBD", "None; docs/adr/0001-x.md"):
        text = GOOD_PLAN.replace("**ADRs:** none", f"**ADRs:** {value}")
        reasons = plan_lint.lint(text)
        assert any(r.startswith("phase-adr-malformed:") for r in reasons), value


def test_duplicate_adr_pointers_in_one_phase_are_flagged():
    text = GOOD_PLAN.replace("**ADRs:** none", "**ADRs:** none\n**ADRs:** ADR-7")
    assert "phase-adr-duplicate:Phase 2 — Reporting (A8)" in plan_lint.lint(text)


def test_every_adr_pointer_is_validated_not_just_the_first():
    # The backfill inserts `none` ABOVE an author's line, so first-match-wins would let
    # the inserted line validate a malformed one below it.
    text = GOOD_PLAN.replace("**ADRs:** none", "**ADRs:** none\n**ADRs:** TBD")
    reasons = plan_lint.lint(text)
    assert "phase-adr-malformed:Phase 2 — Reporting (A8):TBD" in reasons


def test_crlf_empty_line_reports_empty_not_malformed():
    text = GOOD_PLAN.replace("**ADRs:** none\n", "**ADRs:**\r\n")
    reasons = plan_lint.lint(text)
    assert "phase-adr-empty:Phase 2 — Reporting (A8)" in reasons
    assert not any(r.startswith("phase-adr-malformed") for r in reasons)


def test_id_resolves_only_against_the_leading_number_slot(tmp_path):
    # `2026-08-12-…` must NOT resolve ADR-12 — a date would silently swallow the warning.
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    (adr_dir / "2026-08-12-unrelated.md").write_text("x")
    assert plan_lint.adr_warnings(_plan_citing("ADR-12"), str(tmp_path)) == [
        "warn:phase-adr-dangling:Phase 2 — Reporting (A8):ADR-12"
    ]
    (adr_dir / "adr-12-real.md").write_text("x")
    assert plan_lint.adr_warnings(_plan_citing("ADR-12"), str(tmp_path)) == []


@pytest.mark.skipif(
    hasattr(os, "geteuid") and os.geteuid() == 0,
    reason="root ignores mode 0o000, so the dir cannot be made unreadable",
)
def test_unreadable_adr_dir_never_fails_the_lint(tmp_path, capsys):
    adr_dir = tmp_path / "docs" / "adr"
    adr_dir.mkdir(parents=True)
    plan = tmp_path / "plan.md"
    plan.write_text(_plan_citing("ADR-999"))
    adr_dir.chmod(0o000)
    try:
        # Unindexable is not dangling: no warning, and above all no traceback.
        assert plan_lint.adr_warnings(plan.read_text(), str(tmp_path)) == []
        assert plan_lint.main([str(plan)]) == 0
    finally:
        adr_dir.chmod(0o755)
    assert "Traceback" not in capsys.readouterr().err


# --- `--phase`: autodev's fail-closed pre-claim check ---------------------------------
# The gap this closes: only `start`/`prepare` run the full lint, and an in-flight run never
# returns to either, so an upgraded pre-0.9.0 run would keep working phases with nothing
# carrying their decisions. Narrow ON PURPOSE — an unrelated plan defect must never be able
# to stop a headless fire.

_P1 = "Phase 1 — Scoring (A3, A4)"
_P2 = "Phase 2 — Reporting (A8)"


def test_phase_check_returns_that_phases_references():
    reasons, refs = plan_lint.lint_phase_adrs(GOOD_PLAN, _P1)
    assert reasons == []
    assert refs == ["ADR-004", "ADR-011"]


def test_phase_check_passes_on_none_with_no_references():
    assert plan_lint.lint_phase_adrs(GOOD_PLAN, _P2) == ([], [])


def test_phase_check_fails_closed_on_a_missing_line():
    text = GOOD_PLAN.replace("**ADRs:** none\n", "")
    reasons, refs = plan_lint.lint_phase_adrs(text, _P2)
    assert reasons == [f"phase-no-adr-pointer:{_P2}"]
    assert refs == []


def test_phase_check_fails_closed_when_the_phase_is_absent():
    # Nothing carries an absent phase's decisions either; silence is not a pass.
    assert plan_lint.lint_phase_adrs(GOOD_PLAN, "Phase 9 — Nope") == (
        ["phase-not-found:Phase 9 — Nope"],
        [],
    )


def test_phase_check_ignores_defects_in_other_phases():
    # The whole point: a broken phase 1 must not stop a fire working phase 2.
    text = GOOD_PLAN.replace("**Spec:** §6 Metrics; §7 Scoring & Decision Rule\n", "")
    assert f"phase-no-spec-pointer:{_P1}" in plan_lint.lint(text)
    assert plan_lint.lint_phase_adrs(text, _P2) == ([], [])


def test_phase_check_cli_prints_refs_and_exits_zero(tmp_path, capsys):
    plan = tmp_path / "plan.md"
    plan.write_text(GOOD_PLAN)
    assert plan_lint.main([str(plan), "--phase", _P1]) == 0
    assert capsys.readouterr().out.split() == ["ADR-004", "ADR-011"]


def test_phase_check_cli_reads_a_shell_hostile_title_from_stdin(
    tmp_path, capsys, monkeypatch
):
    # Real titles carry `"` and `|` (this repo's own plan has both), so the pipe form is
    # the one autodev uses — argv interpolation is a quoting bug waiting for that phase.
    hostile = 'Phase 2 — README "Unattended authority" + driver install|status (A8)'
    plan = tmp_path / "plan.md"
    plan.write_text(GOOD_PLAN.replace(_P2, hostile))
    monkeypatch.setattr("sys.stdin", io.StringIO(hostile + "\n"))
    assert plan_lint.main([str(plan), "--phase", "-"]) == 0
    assert capsys.readouterr().out == ""


def test_phase_check_cli_names_prepare_as_the_fix(tmp_path, capsys):
    plan = tmp_path / "plan.md"
    plan.write_text(GOOD_PLAN.replace("**ADRs:** none\n", ""))
    assert plan_lint.main([str(plan), "--phase", _P2]) == 1
    err = capsys.readouterr().err
    assert f"phase-no-adr-pointer:{_P2}" in err
    assert "/conductor:prepare" in err


ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_this_repos_own_current_dialect_plans_lint_clean():
    """Conductor conducts itself: shipping a hard-error dialect whose only valid example
    is a test fixture is an unfinished migration. Scoped by `**Normative spec:**` so the
    pre-dialect 2026-06-28 plans (kept as history) stay out of it."""
    plans = sorted(
        p
        for p in glob.glob(os.path.join(ROOT, "docs/plans/*.md"))
        if "**Normative spec:**" in open(p, encoding="utf-8").read()
    )
    assert plans, "no current-dialect plan found — did docs/plans/ move?"
    for path in plans:
        text = open(path, encoding="utf-8").read()
        assert plan_lint.lint(text) == [], os.path.basename(path)
