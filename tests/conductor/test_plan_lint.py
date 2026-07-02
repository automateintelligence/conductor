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

- [ ] Write failing tests
- [ ] Implement scoring

## Phase 2 — Reporting (A8)

**Spec:** §10 Sample Report

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
        "## Backend [ready]\n\n**Spec:** §2\n\ngate: none\n\n- [ ] build it\n"
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
