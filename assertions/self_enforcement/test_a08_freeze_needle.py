"""A8 — gate-freeze-needle-present (contract).

Contract pinned: the start-skill contract test (`tests/conductor/test_skill_outputs.py`,
`test_start_skill_contract`) carries a `gate freeze` needle, so removing the freeze step
from `skills/start/SKILL.md` fails that contract test and the freeze step cannot silently
rot out of the skill. This test also checks the needle actually HOLDS today (the skill
names `conductor gate freeze`), so the pair cannot drift green.
"""

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONTRACT_TEST = ROOT / "tests" / "conductor" / "test_skill_outputs.py"
START_SKILL = ROOT / "skills" / "start" / "SKILL.md"


def _start_contract_block() -> str:
    src = CONTRACT_TEST.read_text()
    m = re.search(r"def test_start_skill_contract\(.*?\):(.*?)(?=\ndef |\Z)", src, re.S)
    assert m, "test_start_skill_contract missing from the contract test"
    return m.group(1)


def test_gate_freeze_needle_is_in_the_start_contract():
    block = _start_contract_block()
    # an ACTIVE needle line: a quoted string in the needle list, not a comment
    assert re.search(r"""(?m)^\s*["'](conductor )?gate freeze["'],?\s*$""", block), (
        "no active 'gate freeze' needle in test_start_skill_contract"
    )
    # must-not: the needle is not merely present as a commented-out line
    assert not re.search(r"""(?m)^\s*#\s*["'](conductor )?gate freeze["']""", block)


def test_the_needle_holds_against_the_skill_today():
    body = START_SKILL.read_text().lower()
    assert "gate freeze" in body, "skills/start/SKILL.md lost its freeze step"
