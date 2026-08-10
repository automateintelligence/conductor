"""The global lock order (design §"Failure handling"): migration.lock, then project.lock, then
owner.lock, then state.lock; multi-run project operations take run locks in sorted run-key order.

A lock-order violation is a deadlock waiting to happen, so it must fail loudly at acquisition
rather than block until a timeout."""

from __future__ import annotations

import errno

import pytest

from conductor.core import locks


def test_lock_order_is_the_documented_sequence():
    assert locks.LOCK_ORDER == ("migration", "project", "owner", "state")


def test_locks_may_be_taken_in_increasing_order(tmp_path):
    with locks.hold(str(tmp_path / "project.lock"), kind="project"):
        with locks.hold(str(tmp_path / "owner.lock"), kind="owner"):
            with locks.hold(str(tmp_path / "state.lock"), kind="state"):
                pass


def test_taking_a_lower_lock_while_holding_a_higher_one_is_refused(tmp_path):
    with locks.hold(str(tmp_path / "state.lock"), kind="state"):
        with pytest.raises(locks.LockOrderError) as excinfo:
            with locks.hold(str(tmp_path / "project.lock"), kind="project"):
                pass
    assert "project" in str(excinfo.value) and "state" in str(excinfo.value)


def test_reentrant_acquisition_of_the_same_lock_is_refused(tmp_path):
    path = str(tmp_path / "project.lock")
    with locks.hold(path, kind="project"):
        with pytest.raises(locks.LockOrderError) as excinfo:
            with locks.hold(path, kind="project"):
                pass
    assert "re-entrant" in str(excinfo.value)


def test_run_locks_of_the_same_kind_must_be_taken_in_sorted_run_key_order(tmp_path):
    with locks.hold(str(tmp_path / "b.lock"), kind="owner", run_key="beta-2222"):
        with pytest.raises(locks.LockOrderError) as excinfo:
            with locks.hold(
                str(tmp_path / "a.lock"), kind="owner", run_key="alpha-1111"
            ):
                pass
    assert "sorted run-key order" in str(excinfo.value)
    with locks.hold(str(tmp_path / "a2.lock"), kind="owner", run_key="alpha-1111"):
        with locks.hold(str(tmp_path / "b2.lock"), kind="owner", run_key="beta-2222"):
            pass


def test_an_unknown_lock_kind_is_refused(tmp_path):
    with pytest.raises(locks.LockOrderError):
        with locks.hold(str(tmp_path / "x.lock"), kind="mystery"):
            pass


def test_a_busy_lock_times_out_with_the_path_named(tmp_path, monkeypatch):
    def always_busy(*_args, **_kwargs):
        raise OSError(errno.EAGAIN, "would block")

    monkeypatch.setattr(locks.fcntl, "flock", always_busy)
    path = str(tmp_path / "project.lock")
    with pytest.raises(locks.LockTimeout) as excinfo:
        with locks.hold(path, kind="project", timeout=0.05, poll=0.01):
            pass
    assert path in str(excinfo.value)


def test_the_held_set_is_cleared_after_a_lock_is_released(tmp_path):
    with locks.hold(str(tmp_path / "state.lock"), kind="state"):
        pass
    with locks.hold(str(tmp_path / "project.lock"), kind="project"):
        pass
