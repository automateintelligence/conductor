# tests/test_skill_outputs.py
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def test_issue_sync_skill_contract_present():
    body = open(os.path.join(ROOT, "skills/issue-sync/SKILL.md")).read().lower()
    for needle in [
        "generate",
        "convert",
        "reconcile",
        "precedence",
        "sub-issue",
        "never prompt",
        "stale",
    ]:
        assert needle in body, needle


def test_assertions_to_tests_skill_contract_present():
    body = (
        open(os.path.join(ROOT, "skills/assertions-to-tests/SKILL.md")).read().lower()
    )
    for needle in [
        "superpowers:test-driven-development",
        "spec-craft:executable-assertions",
        "manifest.yaml",
        "one test per",
        "must not contain",
        "level",
        "kind",
        "stays red",
        "stable",
    ]:
        assert needle in body, needle
