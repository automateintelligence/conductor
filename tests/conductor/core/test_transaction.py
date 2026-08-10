"""Journalled cross-file writes (design §"Failure handling").

An operation that updates project.json and one or more run.json files writes and fsyncs a project
transaction first; every project entry point completes or reverses an unfinished transaction
before reading mappings, so a crash cannot leave a silently split identity.

Crash points are simulated by stopping after prepare / after commit / mid-apply and then calling
recover, which is what the next entry point would do."""

from __future__ import annotations

import json
import os

import pytest

from conductor.core import atomic, transaction


def _entry(path, before, after):
    return {"path": str(path), "before": before, "after": after}


def test_prepare_writes_a_journal_without_touching_the_targets(tmp_path):
    state_root = tmp_path / ".conductor"
    target = tmp_path / "project.json"
    atomic.write_json_atomic(str(target), {"revision": 1})
    transaction.prepare(
        str(state_root), "txn-1", [_entry(target, {"revision": 1}, {"revision": 2})]
    )
    assert atomic.read_json(str(target)) == {"revision": 1}
    journal = json.loads(
        open(transaction.journal_path(str(state_root), "txn-1")).read()
    )
    assert journal["state"] == "prepared"
    assert transaction.pending(str(state_root)) == ["txn-1"]


def test_commit_then_apply_writes_the_after_images_and_clears_the_journal(tmp_path):
    state_root = tmp_path / ".conductor"
    target = tmp_path / "project.json"
    atomic.write_json_atomic(str(target), {"revision": 1})
    transaction.prepare(
        str(state_root), "txn-1", [_entry(target, {"revision": 1}, {"revision": 2})]
    )
    transaction.commit(str(state_root), "txn-1")
    transaction.apply(str(state_root), "txn-1")
    assert atomic.read_json(str(target)) == {"revision": 2}
    assert transaction.pending(str(state_root)) == []


def test_crash_after_prepare_reverses_to_the_before_images(tmp_path):
    state_root = tmp_path / ".conductor"
    project = tmp_path / "project.json"
    run = tmp_path / "run.json"
    atomic.write_json_atomic(str(project), {"revision": 1})
    atomic.write_json_atomic(str(run), {"spec_path": "docs/specs/old.md"})
    transaction.prepare(
        str(state_root),
        "txn-1",
        [
            _entry(project, {"revision": 1}, {"revision": 2}),
            _entry(
                run,
                {"spec_path": "docs/specs/old.md"},
                {"spec_path": "docs/specs/new.md"},
            ),
        ],
    )
    # crash here — no commit
    assert transaction.recover(str(state_root)) == ["txn-1"]
    assert atomic.read_json(str(project)) == {"revision": 1}
    assert atomic.read_json(str(run)) == {"spec_path": "docs/specs/old.md"}


def test_crash_after_commit_rolls_forward(tmp_path):
    state_root = tmp_path / ".conductor"
    project = tmp_path / "project.json"
    run = tmp_path / "run.json"
    atomic.write_json_atomic(str(project), {"revision": 1})
    atomic.write_json_atomic(str(run), {"spec_path": "docs/specs/old.md"})
    transaction.prepare(
        str(state_root),
        "txn-1",
        [
            _entry(project, {"revision": 1}, {"revision": 2}),
            _entry(
                run,
                {"spec_path": "docs/specs/old.md"},
                {"spec_path": "docs/specs/new.md"},
            ),
        ],
    )
    transaction.commit(str(state_root), "txn-1")
    # crash here — apply never ran
    assert transaction.recover(str(state_root)) == ["txn-1"]
    assert atomic.read_json(str(project)) == {"revision": 2}
    assert atomic.read_json(str(run)) == {"spec_path": "docs/specs/new.md"}


def test_crash_midway_through_apply_completes_the_roll_forward(tmp_path):
    state_root = tmp_path / ".conductor"
    project = tmp_path / "project.json"
    run = tmp_path / "run.json"
    atomic.write_json_atomic(str(project), {"revision": 1})
    atomic.write_json_atomic(str(run), {"spec_path": "docs/specs/old.md"})
    transaction.prepare(
        str(state_root),
        "txn-1",
        [
            _entry(project, {"revision": 1}, {"revision": 2}),
            _entry(
                run,
                {"spec_path": "docs/specs/old.md"},
                {"spec_path": "docs/specs/new.md"},
            ),
        ],
    )
    transaction.commit(str(state_root), "txn-1")
    atomic.write_json_atomic(
        str(project), {"revision": 2}
    )  # first target landed, then crash
    assert transaction.recover(str(state_root)) == ["txn-1"]
    assert atomic.read_json(str(project)) == {"revision": 2}
    assert atomic.read_json(str(run)) == {"spec_path": "docs/specs/new.md"}


def test_recover_handles_creation_and_deletion_entries(tmp_path):
    state_root = tmp_path / ".conductor"
    created = tmp_path / "created.json"
    transaction.prepare(str(state_root), "txn-1", [_entry(created, None, {"a": 1})])
    transaction.recover(str(state_root))  # prepared -> reverse -> file must not exist
    assert not created.exists()
    transaction.prepare(str(state_root), "txn-2", [_entry(created, None, {"a": 1})])
    transaction.commit(str(state_root), "txn-2")
    transaction.recover(str(state_root))
    assert atomic.read_json(str(created)) == {"a": 1}
    transaction.prepare(str(state_root), "txn-3", [_entry(created, {"a": 1}, None)])
    transaction.commit(str(state_root), "txn-3")
    transaction.recover(str(state_root))
    assert not created.exists()


def test_recover_is_idempotent_and_processes_journals_in_sorted_order(tmp_path):
    state_root = tmp_path / ".conductor"
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    transaction.prepare(str(state_root), "txn-2", [_entry(second, None, {"n": 2})])
    transaction.commit(str(state_root), "txn-2")
    transaction.prepare(str(state_root), "txn-1", [_entry(first, None, {"n": 1})])
    transaction.commit(str(state_root), "txn-1")
    assert transaction.recover(str(state_root)) == ["txn-1", "txn-2"]
    assert transaction.recover(str(state_root)) == []
    assert atomic.read_json(str(first)) == {"n": 1}
    assert atomic.read_json(str(second)) == {"n": 2}


def test_recover_on_a_clean_state_root_does_nothing(tmp_path):
    assert transaction.recover(str(tmp_path / ".conductor")) == []


@pytest.mark.parametrize(
    "entries",
    [
        [],
        [{"path": "relative/project.json", "before": None, "after": {"a": 1}}],
        [{"path": "/abs/project.json", "before": None, "after": None}],
        [{"before": None, "after": {"a": 1}}],
    ],
)
def test_prepare_refuses_malformed_entries(tmp_path, entries):
    with pytest.raises(ValueError):
        transaction.prepare(str(tmp_path / ".conductor"), "txn-1", entries)


def test_prepare_refuses_an_unsafe_transaction_id(tmp_path):
    with pytest.raises(ValueError):
        transaction.prepare(
            str(tmp_path / ".conductor"),
            "../escape",
            [{"path": str(tmp_path / "x.json"), "before": None, "after": {"a": 1}}],
        )


def test_a_journal_that_cannot_be_parsed_fails_closed(tmp_path):
    state_root = tmp_path / ".conductor"
    os.makedirs(transaction.txn_dir(str(state_root)), exist_ok=True)
    with open(
        transaction.journal_path(str(state_root), "txn-1"), "w", encoding="utf-8"
    ) as fh:
        fh.write("{not json")
    with pytest.raises(ValueError):
        transaction.recover(str(state_root))
