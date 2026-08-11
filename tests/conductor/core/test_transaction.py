"""Journalled cross-file writes (design §"Failure handling").

An operation that updates project.json and one or more run.json files writes and fsyncs a project
transaction first; every project entry point completes or reverses an unfinished transaction
before reading mappings, so a crash cannot leave a silently split identity.

Crash points are simulated by stopping after prepare / after commit / mid-apply and then calling
recover, which is what the next entry point would do."""

from __future__ import annotations

import contextlib
import json
import os
from unittest import mock

import pytest

from conductor.core import atomic, locks, transaction


def _entry(path, before, after):
    return {"path": str(path), "before": before, "after": after}


def _plant_committed_journal(state_root, txn_id, *entries):
    """Write a committed journal straight to disk, bypassing ``prepare``'s one-at-a-time guard.

    Used to reconstruct a state the API no longer produces but the filesystem can still hold: a
    journal restored from a backup, or left by a build that predates the guard."""
    atomic.write_json_atomic(
        transaction.journal_path(str(state_root), txn_id),
        {
            "schema_version": transaction.SCHEMA_VERSION,
            "txn_id": txn_id,
            "state": "committed",
            "entries": [{**entry, "lock": None} for entry in entries],
        },
    )


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


def test_a_second_journal_cannot_be_prepared_while_one_is_pending(tmp_path):
    """Two pending journals make the final image depend on replay order, and journal ids are
    caller-supplied strings replayed in sorted order — lexicographic, not causal. Overlapping
    entry sets would then be decided by whichever id happens to sort last, so the second
    ``prepare`` is refused and both journals are left intact for an operator."""
    state_root = tmp_path / ".conductor"
    transaction.prepare(
        str(state_root), "txn-2", [_entry(tmp_path / "b.json", None, {"n": 2})]
    )
    transaction.commit(str(state_root), "txn-2")
    with pytest.raises(ValueError) as excinfo:
        transaction.prepare(
            str(state_root), "txn-1", [_entry(tmp_path / "a.json", None, {"n": 1})]
        )
    assert "txn-2" in str(excinfo.value) and "still pending" in str(excinfo.value)
    assert transaction.pending(str(state_root)) == ["txn-2"]


def test_recover_is_idempotent_and_processes_journals_in_sorted_order(tmp_path):
    state_root = tmp_path / ".conductor"
    first = tmp_path / "a.json"
    second = tmp_path / "b.json"
    transaction.prepare(str(state_root), "txn-2", [_entry(second, None, {"n": 2})])
    transaction.commit(str(state_root), "txn-2")
    # A second journal can no longer be created through the API while the first is pending, but
    # one can still be found on disk — a journal restored from a backup, or written by a build
    # of Conductor that predates that guard. Recovery must still drain them deterministically.
    _plant_committed_journal(state_root, "txn-1", _entry(first, None, {"n": 1}))
    assert transaction.recover(str(state_root)) == ["txn-1", "txn-2"]
    assert transaction.recover(str(state_root)) == []
    assert atomic.read_json(str(first)) == {"n": 1}
    assert atomic.read_json(str(second)) == {"n": 2}


def test_replay_never_moves_a_revision_backwards(tmp_path):
    """Verbatim replay is what makes recovery idempotent, and it is also what makes it dangerous:
    a writer that legitimately advanced the file since the journal was written would be rolled
    back to a revision it has already passed, and that number would then be REUSED. A stale holder
    expecting it would pass its own compare-and-swap and clobber newer state — a lost update no
    lock prevents, because the clobbering writer's CAS genuinely succeeded."""
    state_root = tmp_path / ".conductor"
    target = tmp_path / "run.json"
    transaction.prepare(
        str(state_root),
        "txn-1",
        [_entry(target, {"revision": 1, "n": 1}, {"revision": 2, "n": 2})],
    )
    transaction.commit(str(state_root), "txn-1")
    # A live writer got there first and is now ahead of the journal's after image.
    atomic.write_json_atomic(str(target), {"revision": 3, "n": 3})
    assert transaction.recover(str(state_root)) == ["txn-1"]
    assert atomic.read_json(str(target)) == {"revision": 3, "n": 3}


def test_replay_of_a_committed_journal_is_idempotent(tmp_path):
    """The non-regression rule must not break the property it guards: the first replay writes, and
    every replay after it sees its own result and does nothing."""
    state_root = tmp_path / ".conductor"
    target = tmp_path / "run.json"
    atomic.write_json_atomic(str(target), {"revision": 1, "n": 1})
    transaction.prepare(
        str(state_root),
        "txn-1",
        [_entry(target, {"revision": 1, "n": 1}, {"revision": 2, "n": 2})],
    )
    transaction.commit(str(state_root), "txn-1")
    assert transaction.recover(str(state_root)) == ["txn-1"]
    assert atomic.read_json(str(target)) == {"revision": 2, "n": 2}
    assert transaction.recover(str(state_root)) == []
    assert atomic.read_json(str(target)) == {"revision": 2, "n": 2}


def test_recovery_holds_the_per_run_lock_the_journal_names(tmp_path):
    """Recovery runs in a later process holding only ``project.lock`` and cannot derive which lock
    guards an opaque absolute path, so the journal carries it. Without this the run's own writers
    serialize on ``state.lock`` and never see the rewrite coming."""
    state_root = tmp_path / ".conductor"
    target = tmp_path / "run.json"
    lock_path = str(tmp_path / "state.lock")
    entry = {
        **_entry(target, None, {"revision": 1}),
        "lock": {"path": lock_path, "run_key": "alpha-00000000"},
    }
    transaction.prepare(str(state_root), "txn-1", [entry])
    transaction.commit(str(state_root), "txn-1")

    held = []
    real_hold = locks.hold

    @contextlib.contextmanager
    def observing_hold(path, **kwargs):
        held.append((os.path.realpath(path), kwargs.get("kind")))
        with real_hold(path, **kwargs) as fd:
            yield fd

    with mock.patch.object(locks, "hold", observing_hold):
        assert transaction.recover(str(state_root)) == ["txn-1"]
    assert (os.path.realpath(lock_path), "state") in held


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


def test_committed_delete_entry_leaves_target_absent_and_recover_is_idempotent(
    tmp_path,
):
    state_root = tmp_path / ".conductor"
    target = tmp_path / "to_delete.json"
    atomic.write_json_atomic(str(target), {"data": "present"})
    assert target.exists()
    transaction.prepare(
        str(state_root), "txn-1", [_entry(target, {"data": "present"}, None)]
    )
    transaction.commit(str(state_root), "txn-1")
    transaction.apply(str(state_root), "txn-1")
    assert not target.exists()
    assert transaction.pending(str(state_root)) == []
    # recover should be idempotent: calling on clean state returns empty
    assert transaction.recover(str(state_root)) == []
    assert not target.exists()
