"""The execution-ownership record: where it lives, what it names, and whether that is alive.

The record exists so a question about a run ("is anything executing this?") has an answer that
outlives the process asking it. Two properties carry that weight and are tested here: the record
is a SIBLING of the lock rather than the lock's contents, and liveness distinguishes "gone" from
"cannot tell" instead of collapsing them.
"""

from __future__ import annotations

import os
import subprocess
import sys

import pytest

from conductor.core import atomic, ownership, runstate

RUN = "alpha-0123456789ab"


def test_the_record_is_a_sibling_of_the_lock_and_the_lock_stays_empty(tmp_path):
    """The lock file is a pure mutex. Writing the fields into it would replace its inode on
    every write — ``atomic.write_atomic`` finishes with ``os.replace`` — leaving every existing
    holder flocked to a file nothing will reopen, while later acquirers lock the new inode
    uncontended and the mutex silently stops excluding anything."""
    root = str(tmp_path / "state")
    with ownership.acquire(root, RUN, host="claude", wrapper_identity="4242") as record:
        lock = runstate.owner_lock_path(root, RUN)
        assert record.wrapper_identity == "4242"
        assert os.path.exists(ownership.record_path(root, RUN))
        assert os.path.dirname(lock) == os.path.dirname(
            ownership.record_path(root, RUN)
        )
        assert os.path.getsize(lock) == 0


def test_the_lock_inode_survives_repeated_record_writes(tmp_path):
    root = str(tmp_path / "state")
    lock = runstate.owner_lock_path(root, RUN)
    with ownership.acquire(root, RUN, host="claude", wrapper_identity="1"):
        first = os.stat(lock).st_ino
    with ownership.acquire(root, RUN, host="codex", wrapper_identity="2"):
        assert os.stat(lock).st_ino == first


def test_the_record_is_removed_on_a_clean_exit(tmp_path):
    root = str(tmp_path / "state")
    with ownership.acquire(root, RUN, host="claude", wrapper_identity="4242"):
        pass
    assert ownership.read(root, RUN) is None


def test_a_record_naming_another_run_is_refused_rather_than_returned(tmp_path):
    root = str(tmp_path / "state")
    os.makedirs(runstate.run_dir(root, RUN))
    atomic.write_json_atomic(
        ownership.record_path(root, RUN),
        {
            "run_key": "beta-0123456789ab",
            "host": "claude",
            "tier": "wrapper",
            "wrapper_identity": "1",
            "acquired_at": "2026-08-10T12:00:00+00:00",
        },
    )
    with pytest.raises(ownership.OwnerAmbiguous, match="names run"):
        ownership.read(root, RUN)


def test_a_record_missing_a_field_is_refused_rather_than_returned(tmp_path):
    root = str(tmp_path / "state")
    os.makedirs(runstate.run_dir(root, RUN))
    atomic.write_json_atomic(
        ownership.record_path(root, RUN), {"run_key": RUN, "host": "claude"}
    )
    with pytest.raises(ownership.OwnerAmbiguous, match="missing"):
        ownership.read(root, RUN)


def test_a_wider_future_record_still_reads(tmp_path):
    """Plan 02 writes workstation, lease and heartbeat fields onto this document. A reader that
    rejected unknown keys would turn that forward move into a fleet-wide refusal."""
    root = str(tmp_path / "state")
    os.makedirs(runstate.run_dir(root, RUN))
    atomic.write_json_atomic(
        ownership.record_path(root, RUN),
        {
            "run_key": RUN,
            "host": "codex",
            "tier": "in-session",
            "wrapper_identity": "77",
            "acquired_at": "2026-08-10T12:00:00+00:00",
            "workstation_id": "f" * 32,
            "lease_expires_at": "2026-08-10T12:02:00+00:00",
        },
    )
    record = ownership.read(root, RUN)
    assert record is not None and record.host == "codex"


def test_acquire_refuses_while_a_live_identity_holds_the_run(tmp_path):
    root = str(tmp_path / "state")
    with ownership.acquire(root, RUN, host="claude", wrapper_identity=str(os.getpid())):
        with pytest.raises(ownership.OwnerBusy, match=str(os.getpid())):
            with ownership.acquire(root, RUN, host="codex", wrapper_identity="999999"):
                pass


def test_acquire_takes_over_a_record_whose_identity_has_exited(tmp_path):
    """Provably exited is the only state that yields the run — not expiry, which is why this
    module carries no timer."""
    root = str(tmp_path / "state")
    dead = subprocess.Popen([sys.executable, "-c", "pass"])
    dead.wait(timeout=30)
    os.makedirs(runstate.run_dir(root, RUN))
    atomic.write_json_atomic(
        ownership.record_path(root, RUN),
        {
            "run_key": RUN,
            "host": "claude",
            "tier": "wrapper",
            "wrapper_identity": str(dead.pid),
            "acquired_at": "2026-08-10T12:00:00+00:00",
        },
    )
    with ownership.acquire(root, RUN, host="codex", wrapper_identity="55") as record:
        assert record.host == "codex"


def test_release_leaves_another_identitys_record_alone(tmp_path):
    root = str(tmp_path / "state")
    with ownership.acquire(root, RUN, host="claude", wrapper_identity="4242"):
        ownership.release(root, RUN, wrapper_identity="9999")
        survivor = ownership.read(root, RUN)
        assert survivor is not None and survivor.wrapper_identity == "4242"


def test_liveness_separates_gone_from_uninterpretable():
    assert ownership.identity_is_live(str(os.getpid())) is True
    assert ownership.identity_is_live("") is None
    assert ownership.identity_is_live("wrapper-token") is None
    assert ownership.identity_is_live("0") is None


def test_a_reaped_child_reads_as_exited():
    child = subprocess.Popen([sys.executable, "-c", "pass"])
    child.wait(timeout=30)
    assert ownership.identity_is_live(str(child.pid)) is False


def test_an_unsafe_run_key_never_becomes_a_path(tmp_path):
    with pytest.raises(ValueError, match="unsafe run key"):
        ownership.record_path(str(tmp_path), "../escape")
