"""Durable state writes (design §"Failure handling"): sibling temp file, flush, fsync,
atomic replace. A crash must leave either the old bytes or the new ones — never a torn file."""

from __future__ import annotations

import os
import stat

import pytest

from conductor.core import atomic


def test_write_atomic_replaces_and_leaves_no_temp_file(tmp_path):
    target = tmp_path / "state.json"
    target.write_text("old\n")
    atomic.write_atomic(str(target), "new\n")
    assert target.read_text() == "new\n"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_failed_replace_leaves_the_old_bytes_and_removes_the_temp_file(
    tmp_path, monkeypatch
):
    target = tmp_path / "state.json"
    target.write_text("old\n")

    class Boom(RuntimeError):
        pass

    def explode(*_args, **_kwargs):
        raise Boom("replace failed")

    monkeypatch.setattr(atomic.os, "replace", explode)
    with pytest.raises(Boom):
        atomic.write_atomic(str(target), "new\n")
    assert target.read_text() == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["state.json"]


def test_every_directory_level_created_is_fsynced(tmp_path, monkeypatch):
    """``os.makedirs`` can create several levels at once, and fsyncing only the leaf leaves the
    entries that link the new levels to their parents unflushed — a crash can then lose the whole
    subtree along with a state file whose own bytes were faithfully fsynced. The first run of a
    project creates ``.conductor/runs/<run-key>/`` in exactly one call."""
    synced = []
    monkeypatch.setattr(atomic, "_fsync_dir", synced.append)
    target = tmp_path / ".conductor" / "runs" / "alpha-00000000" / "run.json"
    atomic.write_json_atomic(str(target), {"n": 1})
    # The directory holding each new level's entry, outermost first, then the leaf again for the
    # rename. Asserted exactly: with the durable makedirs reverted only the last entry appears.
    assert synced == [
        str(tmp_path),
        str(tmp_path / ".conductor"),
        str(tmp_path / ".conductor" / "runs"),
        str(target.parent),
    ]
    assert atomic.read_json(str(target)) == {"n": 1}


def test_the_mode_is_set_before_the_file_is_fsynced(tmp_path, monkeypatch):
    """A ``chmod`` after the only fsync mutates the inode with nothing left to flush it — the
    directory fsync that follows covers the rename's entry, not the mode — so a crash could
    publish the file with ``mkstemp``'s private 0600 instead of the mode asked for."""
    order = []
    real_fchmod, real_fsync = os.fchmod, os.fsync
    monkeypatch.setattr(
        os, "fchmod", lambda fd, mode: (order.append("chmod"), real_fchmod(fd, mode))[1]
    )
    monkeypatch.setattr(
        os, "fsync", lambda fd: (order.append("fsync"), real_fsync(fd))[1]
    )
    atomic.write_atomic(str(tmp_path / "f.json"), "{}", mode=0o640)
    assert order[:2] == ["chmod", "fsync"]
    assert stat.S_IMODE(os.stat(tmp_path / "f.json").st_mode) == 0o640


def test_write_atomic_creates_missing_parent_directories(tmp_path):
    target = tmp_path / "runs" / "alpha-1a2b3c4d" / "run.json"
    atomic.write_atomic(str(target), "{}\n")
    assert target.read_text() == "{}\n"


def test_write_atomic_honours_mode(tmp_path):
    target = tmp_path / "resume-env"
    atomic.write_atomic(str(target), "x\n", mode=0o600)
    assert stat.S_IMODE(target.stat().st_mode) == 0o600


def test_write_json_atomic_round_trips_with_stable_key_order(tmp_path):
    target = tmp_path / "project.json"
    atomic.write_json_atomic(str(target), {"b": 1, "a": 2})
    assert atomic.read_json(str(target)) == {"a": 2, "b": 1}
    assert target.read_text().index('"a"') < target.read_text().index('"b"')


def test_read_json_returns_none_only_for_an_absent_file(tmp_path):
    assert atomic.read_json(str(tmp_path / "missing.json")) is None


def test_read_json_raises_on_malformed_content(tmp_path):
    target = tmp_path / "project.json"
    target.write_text("{not json")
    with pytest.raises(ValueError):
        atomic.read_json(str(target))


@pytest.mark.parametrize("body", ["[]", '["a"]', '"a string"', "3", "null"])
def test_read_json_raises_on_well_formed_json_that_is_not_an_object(tmp_path, body):
    """The signature says ``dict | None`` and ``registry.load`` / ``runstate.load`` return this
    value untouched to callers that subscript it. Without the guard a top-level array reaches
    them as a list and fails as an AttributeError traceback naming nothing, instead of a refusal
    naming the file."""
    target = tmp_path / "project.json"
    target.write_text(body)
    with pytest.raises(ValueError) as excinfo:
        atomic.read_json(str(target))
    assert str(target) in str(excinfo.value)


def test_remove_durably_deletes_an_existing_file_and_is_silent_on_absence(tmp_path):
    target = tmp_path / "to_remove.json"
    target.write_text("content\n")
    atomic.remove_durably(str(target))
    assert not target.exists()
    # Second call on absent file must not raise
    atomic.remove_durably(str(target))
