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
        # effort router. The worker CANNOT set its own effort (/effort is a human-only local
        # command), so these pin the disclaimer plus the one lever it does hold: the dispatch
        # prompt. A needle asserting autodev runs /effort would lock in fake compliance.
        "effort router",
        "the model never changes",
        "you cannot set your own effort",
        "no human is in a fire",
        "no effort parameter",
        "reason at high effort",
        "reason at xhigh effort",
        "dispatch prompt is the one lever",
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
        # effort router: xhigh setup, and the three real levers — ask the owner (supervised
        # setup), --effort on the headless driver, in-prompt directive for subagents. The
        # idle-REPL needle pins WHY the owner must drop the REPL to auto before walking away.
        "effort router",
        "the model never changes",
        "you cannot set your own effort",
        "ask the owner to type `/effort xhigh`",
        "type `/effort auto` once setup finishes",
        "idle repl",
        'conductor_resume_claude_flags="--effort auto"',
        "no effort parameter",
        "reason at xhigh effort",
        "implement, pr, merge, release at `auto`",
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
        # effort router: building the done-gate is xhigh setup work, asked of the owner
        "you cannot set this",
        "ask the owner to type `/effort xhigh`",
        "reason at xhigh effort",
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
        # effort router: brownfield alignment is xhigh work, asked of the supervising owner
        "you cannot set your own effort",
        "ask the owner to type `/effort xhigh`",
        "reason at xhigh effort",
    ]:
        assert needle in body, needle
