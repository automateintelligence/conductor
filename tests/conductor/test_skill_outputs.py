# tests/conductor/test_skill_outputs.py
import os

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def test_autodev_skill_contract():
    body = open(os.path.join(ROOT, "skills/autodev/SKILL.md")).read().lower()
    for needle in [
        "re-load goal",
        "reconcile",
        "assert run --level spec",
        "fresh subagent",
        "conductor merge-gate",
        "conductor merge <pr>",
        "no-progress-cap-exceeded",
        "handoff",
        "one phase",
        "crondelete",
        "ask no questions",
        "environment-provided",
        "--from-gate",
        "phase-done",
        "codex review",
        "usage-limit fallback",
        "escalate.file_followup",
        "conductor remote",
        "reconcile-within-phase",
        "wip: reclaimed partial work",
        "never sufficient",
        "normative spec",
        "# conductor-autodev",
        "grep -f -v",
        "owner-owned",
        "keep the run branch current",
        "merge, never rebase",
        "run-packet",
        "not with --admin, not at all",
        "base = the run branch",
        "run_branch",
        # the decisions leg: ADRs reach the worker, bind it, and get cited when written
        "**adrs:**",
        "an adr binds the phase exactly as its",
        "never quietly build against a closed decision",
        "cite that adr",
    ]:
        assert needle in body, needle


def test_start_skill_contract():
    body = open(os.path.join(ROOT, "skills/start/SKILL.md")).read().lower()
    for needle in [
        "preflight",
        "reconcile-first",
        "idempotent",
        "spec-craft:executable-assertions",
        "conductor:assertions-to-tests",
        "issue-sync",
        "croncreate",
        "/conductor:autodev",
        "start_probe.assertions_ready",
        "gate freeze",
        "conductor gate lint",
        "already done",
        "resume",
        "plan-lint",
        "normative spec:",
        "the plan builds to the spec",
        "done-floor",
        "codex-review the plan",
        "verify durability",
        "flock",
        "conductor resume-script",
        "resume-env.sh",
        "resume-script verify",
        "# conductor-autodev",
        "run topology",
        "conductor/run-",
        "run_branch",
        "worktree",
        "conductor_allow_direct_main_merge=1",
        "base-mismatch",
        # the plan MUST carry the decisions pointer, and `none` is the explicit answer
        "**adrs:**",
        "**adrs:** none",
        "adr dir",
    ]:
        assert needle in body, needle


def test_assertions_to_tests_skill_contract():
    body = (
        open(os.path.join(ROOT, "skills/assertions-to-tests/SKILL.md")).read().lower()
    )
    for needle in [
        "pytest_disable_plugin_autoload=1",
        "--noconftest",
        "no:cacheprovider",
        "self-contained",
        "<spec>.assertions.md",
        "red-team",
        "worse than none",
        "exists-but-unused",
    ]:
        assert needle in body, needle


def test_prepare_skill_contract():
    body = open(os.path.join(ROOT, "skills/prepare/SKILL.md")).read().lower()
    for needle in [
        "brownfield",
        "owner-supervised",
        "dry-run first",
        "assertion-id set",
        "conductor ledger align <plan.md> --apply",
        "conductor ledger convert <plan.md>",
        "plan-lint",
        "--from-gate",
        "phase-done",
        "status:draft",
        "run topology",
        "ready for `/conductor:start`",
        "never guess",
        "gate verify",
        # the mechanical migration path for the required **ADRs:** line
        "backfill",
        "**adrs:** none",
        "phase-no-adr-pointer",
    ]:
        assert needle in body, needle


def _step(body: str, start: str, end: str) -> str:
    """The slice of a SKILL.md between two step markers.

    Narrow placement check, not a redesign: these contract tests assert vocabulary exists
    ANYWHERE in the file, so a refactor that moves an instruction out of the step a worker
    executes and into trailing prose keeps them green. Making all ~40 needles step-aware is
    tracked separately; the two below cover the instructions that carry decisions to a
    worker, where wrong placement means the decisions silently stop arriving."""
    i, j = body.index(start), body.index(end)
    assert i < j, (start, end)
    return body[i:j]


def test_adr_precondition_lives_in_autodevs_pre_claim_step():
    body = open(os.path.join(ROOT, "skills/autodev/SKILL.md")).read().lower()
    precondition = _step(body, "4b. **decisions precondition", "5. **claim.**")
    assert "--phase" in precondition
    assert "before the claim" in precondition
    assert "/conductor:prepare" in precondition
    # And the references it prints must be handed over inside the execute step.
    execute = _step(body, "6. **execute the phase", "7. **escalation")
    assert "build within the decisions" in execute


def test_adr_backfill_lives_in_prepares_plan_evaluation_step():
    body = open(os.path.join(ROOT, "skills/prepare/SKILL.md")).read().lower()
    evaluation = _step(body, "2. **plan evaluation.**", "3. **ledger alignment")
    assert "backfill" in evaluation
    assert "**adrs:** none" in evaluation
    assert "dry-run" in evaluation
