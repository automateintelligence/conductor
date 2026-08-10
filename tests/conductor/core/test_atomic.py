"""Durable state writes (design §"Failure handling"): sibling temp file, flush, fsync,
atomic replace. A crash must leave either the old bytes or the new ones — never a torn file."""

from __future__ import annotations

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


def test_remove_durably_deletes_an_existing_file_and_is_silent_on_absence(tmp_path):
    target = tmp_path / "to_remove.json"
    target.write_text("content\n")
    atomic.remove_durably(str(target))
    assert not target.exists()
    # Second call on absent file must not raise
    atomic.remove_durably(str(target))
