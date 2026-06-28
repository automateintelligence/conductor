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
        "handoff",
        "one phase",
        "crondelete",
        "ask no questions",
        "environment-provided",
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
        "/loop /conductor:autodev",
        "start_probe.assertions_ready",
        "already done",
        "resume",
    ]:
        assert needle in body, needle
