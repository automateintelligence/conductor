"""A15 — readme-authority-present-and-no-grant-leftover (example).

Contract pinned: the README documents the session-inherit unattended-authority model in
an "Unattended authority" section (naming the inherit model, the warning+acknowledgment
gate, and the less-privileged dry-run), and NO user-facing doc — README, the E5 recovery
runbook, or any skill — references the removed `grant --scoped` / `grant --full`
commands. This spec and its `.assertions.md` document the removal and are exempt by
construction (they are simply not in the checked set).
"""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
README = ROOT / "README.md"
RECOVERY = ROOT / "experiments" / "E5-end-to-end" / "recovery.md"


def _user_facing_docs() -> list:
    docs = [README, RECOVERY]
    docs += sorted((ROOT / "skills").glob("*/SKILL.md"))
    for d in docs:
        assert d.is_file(), f"user-facing doc missing: {d}"
    return docs


def test_readme_has_an_unattended_authority_section():
    text = README.read_text()
    low = text.lower()
    idx = low.find("unattended authority")
    assert idx != -1, "README has no 'Unattended authority' section"
    section = low[idx : idx + 3000]
    # the section states the session-inherit model, the bypass warning+acknowledgment,
    # and the less-privileged dry-run
    assert "inherit" in section, "section does not describe the session-inherit model"
    assert "acknowledg" in section, "section does not describe the acknowledgment gate"
    assert "dry-run" in section, "section does not describe the less-privileged dry-run"


def test_no_user_facing_doc_references_the_removed_grant_command():
    for doc in _user_facing_docs():
        text = doc.read_text()
        # must-not: the removed command resurfacing anywhere user-facing
        assert "grant --scoped" not in text, doc
        assert "grant --full" not in text, doc
