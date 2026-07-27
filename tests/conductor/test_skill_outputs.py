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
        # effort router: same model, three efforts; set inside the fire, carried by prompt
        "effort router",
        "the model never changes",
        "/effort auto",
        "/effort xhigh",
        "reason at high effort",
        "no effort parameter",
        "model_reasoning_effort",
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
        # effort router: setup/research/plan is xhigh; the recipe carries auto/high into plans
        "effort router",
        "the model never changes",
        "/effort xhigh",
        "no effort parameter",
        "implement, pr, merge, release at `auto`",
        "not inherited across the cron boundary",
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
        # effort router: building the done-gate is setup work
        "effort router",
        "/effort xhigh",
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
        # effort router: brownfield alignment is setup/plan work
        "effort router",
        "/effort xhigh",
    ]:
        assert needle in body, needle
