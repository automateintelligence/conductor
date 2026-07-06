"""A12 — skills-call-the-resolvers (contract).

Contract pinned: both `skills/start/SKILL.md` and `skills/autodev/SKILL.md` invoke
`conductor run-branch name` and `conductor default-branch` (the single-sourced resolvers),
and neither still instructs deriving the run-branch slug in prose ("compute
`conductor/run-<spec-slug>` from ..."). A resolver that exists but is not called leaves
the prose fragility in place — this is the "exists-but-unused" hole closed mechanically.
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
SKILLS = (
    ROOT / "skills" / "start" / "SKILL.md",
    ROOT / "skills" / "autodev" / "SKILL.md",
)


def test_both_skills_invoke_both_resolvers():
    for skill in SKILLS:
        body = skill.read_text().lower()
        assert "conductor run-branch name" in body, skill
        assert "conductor default-branch" in body, skill


def test_prose_slug_derivation_is_gone():
    for skill in SKILLS:
        body = skill.read_text().lower()
        # must-not: the old instruction to derive the slug by hand
        # ("compute `conductor/run-<spec-slug>` from THIS spec's filename" /
        #  "`conductor/run-<spec-slug>` from the goal's spec path")
        assert "conductor/run-<spec-slug>` from" not in body, skill
