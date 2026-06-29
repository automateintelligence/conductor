import pytest
from conductor import handoff


def _ctx(**over):
    ctx = {
        "goal": "ship X",
        "paths": {
            "spec": "s",
            "expectations": "e",
            "assertions": "a",
            "plan_index": "p",
            "adr_dir": "d",
        },
        "active_plan": "plan-1",
        "milestone": 3,
        "phase_issue": 7,
        "phase_status": "status:in-progress",
        "baseline": "aaa",
        "final": "bbb",
        "last_unit_summary": "did thing",
        "next_unit": "phase 2",
        "open_issues": {"debt": [1], "feature": [], "blocked": []},
        "branch": "feat/x",
        "resume_cmd": "claude -p '/conductor:start <spec>'",
    }
    ctx.update(over)
    return ctx


def test_build_includes_required_payload():
    md = handoff.build(_ctx())
    for needle in [
        "ship X",
        "assert run --level spec",
        "aaa..bbb",
        "#7",
        "claude -p",
        "status:in-progress",
        "feat/x",
    ]:
        assert needle in md, needle


def test_write_rejects_missing_field(tmp_path):
    with pytest.raises(ValueError):
        handoff.write({"goal": "x"}, str(tmp_path / "h.md"))
