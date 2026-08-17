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


# --------------------------------------------------------------------- host-derived recipe
#
# A1. The recipe needles used to be the literal tuple ("/code-review", "codex", "merge-gate",
# "closes #"). Two of those four name ONE host: the opposite-host reviewer and the invocation
# form of the review command. On a Codex-hosted run the opposite host is Claude, so a plan
# whose recipe correctly says "claude review" failed a lint that demanded the substring
# "codex" — the lint rejected the only correct plan it could have been given.

CODEX_HOSTED_PLAN = GOOD_PLAN.replace(
    "/code-review per task", "$code-review per task"
).replace("codex review ×2", "claude review ×2")


def test_claude_hosted_recipe_still_passes_unchanged():
    # The regression floor: today's plan text, today's verdict.
    assert plan_lint.lint(GOOD_PLAN, host_id="claude") == []


def test_codex_hosted_recipe_naming_claude_as_reviewer_passes():
    assert plan_lint.lint(CODEX_HOSTED_PLAN, host_id="codex") == []


def test_a_codex_hosted_plan_that_names_codex_as_its_own_reviewer_fails():
    # Same-host review is the defect the opposite-host policy exists to prevent, and it is
    # exactly what a plan copied from a Claude run would say.
    reasons = plan_lint.lint(GOOD_PLAN, host_id="codex")
    assert "recipe-missing:claude" in reasons


def test_a_claude_hosted_plan_that_names_claude_as_its_own_reviewer_fails():
    reasons = plan_lint.lint(CODEX_HOSTED_PLAN, host_id="claude")
    assert "recipe-missing:codex" in reasons


def test_the_review_command_needle_is_rendered_in_the_hosts_own_form():
    assert plan_lint.recipe_needles("claude")[0] == "/code-review"
    assert plan_lint.recipe_needles("codex")[0] == "$code-review"


def test_the_reviewer_needle_is_always_the_opposite_host():
    assert "codex" in plan_lint.recipe_needles("claude")
    assert "claude" in plan_lint.recipe_needles("codex")
    assert "claude" not in plan_lint.recipe_needles("claude")
    assert "codex" not in plan_lint.recipe_needles("codex")


def test_the_host_neutral_needles_are_identical_on_both_hosts():
    shared = set(plan_lint.recipe_needles("claude")) & set(
        plan_lint.recipe_needles("codex")
    )
    assert shared == {"merge-gate", "closes #"}


def test_lint_defaults_to_the_recorded_host_when_none_is_passed(tmp_path, monkeypatch):
    monkeypatch.setenv("CONDUCTOR_HOST", "codex")
    assert plan_lint.lint(CODEX_HOSTED_PLAN) == []
    assert "recipe-missing:claude" in plan_lint.lint(GOOD_PLAN)


def test_an_unknown_host_is_refused_rather_than_silently_linted_as_claude():
    with pytest.raises(Exception) as excinfo:
        plan_lint.recipe_needles("gemini")
    assert "gemini" in str(excinfo.value)


# Every host case above hands `lint` its answer — as `host_id=` or through `$CONDUCTOR_HOST`.
# The CLI derives it instead, from the PLAN'S OWN repo, and that derivation had no coverage.


def _repo_with_plan(tmp_path, text):
    import subprocess

    repo = tmp_path / "repo"
    repo.mkdir(parents=True)
    subprocess.run(["git", "init", "-q", str(repo)], check=True, timeout=30)
    plan = repo / "plan.md"
    plan.write_text(text, encoding="utf-8")
    return repo, str(plan)


def test_the_cli_derives_claude_for_a_repo_with_nothing_recorded(
    tmp_path, monkeypatch, capsys
):
    """No `host_id=`, no `$CONDUCTOR_HOST`, no `.conductor/host`: the pre-A1 state, which must
    keep linting for a Claude-hosted run and keep demanding a Codex reviewer."""
    monkeypatch.delenv("CONDUCTOR_HOST", raising=False)
    _repo, good = _repo_with_plan(tmp_path, GOOD_PLAN)
    assert plan_lint.main([good]) == 0

    _repo2, codex_plan = _repo_with_plan(tmp_path / "b", CODEX_HOSTED_PLAN)
    assert plan_lint.main([codex_plan]) == 1
    assert "recipe-missing:codex" in capsys.readouterr().err


def test_the_cli_derives_codex_from_the_repos_durable_recording(
    tmp_path, monkeypatch, capsys
):
    """The same derivation with the recording as its only input — no environment at all. A
    Codex-hosted plan passes and a copied Claude-hosted one is refused for naming its own
    host as reviewer."""
    from conductor.hosts import runhost

    monkeypatch.delenv("CONDUCTOR_HOST", raising=False)
    repo, codex_plan = _repo_with_plan(tmp_path, CODEX_HOSTED_PLAN)
    runhost.record(str(repo), "codex")
    assert plan_lint.main([codex_plan]) == 0

    (repo / "claude-plan.md").write_text(GOOD_PLAN, encoding="utf-8")
    assert plan_lint.main([str(repo / "claude-plan.md")]) == 1
    assert "recipe-missing:claude" in capsys.readouterr().err
