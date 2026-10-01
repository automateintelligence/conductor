from __future__ import annotations

import pytest

from conductor import dispatches
from conductor.core import runkey, runstate, schema
from conductor.hosts import base

WORKSTATION = "0123456789abcdef0123456789abcdef"
SPEC = "docs/specs/alpha.md"
NOW = "2026-09-30T00:00:00+00:00"


def _u(i=100, c=60, w=10, o=5):
    return base.Usage(i, c, w, o, "s", "text", False)


def _doc(key):
    return schema.new_run_doc(
        run_key=key,
        generation=1,
        spec_path=SPEC,
        workstation_id=WORKSTATION,
        integration_branch=f"conductor/run-{key}",
        gate_dir=f"assertions/{key}",
        spec_digest="a" * 64,
        now=NOW,
    )


@pytest.fixture
def run_key_value():
    return runkey.run_key(SPEC)


@pytest.fixture
def run_doc(run_key_value):
    return _doc(run_key_value)


@pytest.fixture
def state_root(tmp_path, run_key_value):
    root = str(tmp_path / ".conductor")
    runstate.create(root, run_key_value, _doc(run_key_value))
    return root


def dispatches_run(state_root, run_key) -> dict:
    run = runstate.load(state_root, run_key)
    assert run is not None
    return run


def test_make_builds_a_valid_entry_with_nulls_preserved():
    e = dispatches.make(
        host="codex",
        role="reviewer",
        phase_id="12",
        head_sha="abc",
        usage=base.Usage.unknown(),
        wall_s=3.5,
        outcome="error",
        note="no-usage",
        now="2026-09-30T00:00:00+00:00",
    )
    assert schema.validate_dispatch(e) is e
    assert e["input_tokens"] is None and e["output_tokens"] is None
    assert set(e) == {
        "host",
        "role",
        "phase_id",
        "head_sha",
        *schema.DISPATCH_TOKEN_FIELDS,
        "wall_s",
        "outcome",
        "note",
        "recorded_at",
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("role", "author"),
        ("host", "gpt"),
        ("outcome", "fine"),
        ("input_tokens", -1),
        ("output_tokens", 1.5),
        ("wall_s", -0.1),
        ("phase_id", 12),
        ("recorded_at", ""),
    ],
)
def test_validate_dispatch_rejects_bad_values(field, value):
    e = dispatches.make(
        host="claude",
        role="worker",
        phase_id=None,
        head_sha=None,
        usage=_u(),
        wall_s=1,
        outcome="ok",
        now="t",
    )
    e[field] = value
    with pytest.raises(schema.SchemaError):
        schema.validate_dispatch(e)


def test_validate_dispatch_rejects_non_mapping_missing_and_extra_fields():
    e = dispatches.make(
        host="claude",
        role="worker",
        phase_id=None,
        head_sha=None,
        usage=_u(),
        wall_s=1,
        outcome="ok",
        now="t",
    )
    with pytest.raises(schema.SchemaError):
        schema.validate_dispatch([e])
    with pytest.raises(schema.SchemaError):
        schema.validate_dispatch({k: v for k, v in e.items() if k != "note"})
    with pytest.raises(schema.SchemaError):
        schema.validate_dispatch({**e, "extra": 1})
    with pytest.raises(schema.SchemaError):
        schema.validate_dispatch({**e, "input_tokens": True})


def test_validate_run_rejects_a_bad_dispatch_entry(run_doc):
    run_doc["dispatches"] = [{"host": "claude"}]
    with pytest.raises(schema.SchemaError):
        schema.validate_run(run_doc)


def test_validate_run_accepts_a_good_dispatch_entry(run_doc):
    run_doc["dispatches"] = [
        dispatches.make(
            host="claude",
            role="worker",
            phase_id=None,
            head_sha=None,
            usage=_u(),
            wall_s=1,
            outcome="ok",
            now="t",
        )
    ]
    assert schema.validate_run(run_doc) is run_doc


def test_phase_totals_split_by_phase_and_role_and_mark_nulls_incomplete():
    ds = [
        dispatches.make(
            host="claude",
            role="worker",
            phase_id="3",
            head_sha=None,
            usage=_u(),
            wall_s=10,
            outcome="ok",
            now="t",
        ),
        dispatches.make(
            host="codex",
            role="reviewer",
            phase_id="3",
            head_sha="a",
            usage=_u(200, 150, 0, 7),
            wall_s=4,
            outcome="ok",
            now="t",
        ),
        dispatches.make(
            host="codex",
            role="reviewer",
            phase_id="3",
            head_sha="b",
            usage=base.Usage.unknown(),
            wall_s=2,
            outcome="timeout",
            now="t",
        ),
        dispatches.make(
            host="claude",
            role="worker",
            phase_id=None,
            head_sha=None,
            usage=_u(1, 0, 0, 1),
            wall_s=1,
            outcome="ok",
            now="t",
        ),
    ]
    t = dispatches.phase_totals(ds)
    assert t["3"]["worker"] == {
        "dispatches": 1,
        "input_tokens": 100,
        "cached_input_tokens": 60,
        "cache_write_tokens": 10,
        "output_tokens": 5,
        "wall_s": 10.0,
        "complete": True,
    }
    assert t["3"]["reviewer"]["dispatches"] == 2
    assert t["3"]["reviewer"]["input_tokens"] == 200
    assert t["3"]["reviewer"]["wall_s"] == 6.0
    assert t["3"]["reviewer"]["complete"] is False
    assert t[dispatches.UNATTRIBUTED]["worker"]["input_tokens"] == 1


def test_append_commits_under_the_run_revision(state_root, run_key_value):
    before = dispatches_run(state_root, run_key_value)["revision"]
    e = dispatches.make(
        host="claude",
        role="worker",
        phase_id="1",
        head_sha=None,
        usage=_u(),
        wall_s=1,
        outcome="ok",
        now="t",
    )
    after = dispatches.append(state_root, run_key_value, e)
    assert after["revision"] == before + 1 and after["dispatches"][-1] == e


def test_append_refuses_an_invalid_entry_without_writing(state_root, run_key_value):
    before = dispatches_run(state_root, run_key_value)
    with pytest.raises(schema.SchemaError):
        dispatches.append(state_root, run_key_value, {"host": "claude"})
    assert dispatches_run(state_root, run_key_value) == before
