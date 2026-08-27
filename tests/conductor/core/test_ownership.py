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
from conductor.hosts import proc

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
    rejected unknown keys would turn that forward move into a fleet-wide refusal.

    Unknown FIELDS are still ignored; an unknown SCHEMA VERSION is not. The two are opposite
    directions: a wider document written by a newer build still says what this one needs, while
    an older document says something this build would misread."""
    root = str(tmp_path / "state")
    os.makedirs(runstate.run_dir(root, RUN))
    atomic.write_json_atomic(
        ownership.record_path(root, RUN),
        {
            "run_key": RUN,
            "host": "codex",
            "tier": "in-session",
            "wrapper_identity": "codex:019feab3-05f0-7081-90fb-18b96bc27db3:boot",
            "acquired_at": "2026-08-10T12:00:00+00:00",
            "schema_version": ownership.RECORD_SCHEMA_VERSION,
            "workstation_id": "f" * 32,
            "lease_expires_at": "2026-08-10T12:02:00+00:00",
        },
    )
    record = ownership.read(root, RUN)
    assert record is not None and record.host == "codex"


def test_a_version_1_bare_pid_record_is_refused_rather_than_read(tmp_path):
    """The reuse defence cannot be added retroactively to a record that has none.

    A v1 ``wrapper_identity`` is ``str(os.getpid())``. Read by this build it would answer
    liveness about a pid with nothing to say whether that pid is still the process that was
    recorded — and the wrong answer in one direction fires a driver into an occupied worktree.
    The refusal names the command that clears it, so an operator is not left with ``rm``."""
    root = str(tmp_path / "state")
    os.makedirs(runstate.run_dir(root, RUN))
    atomic.write_json_atomic(
        ownership.record_path(root, RUN),
        {
            "run_key": RUN,
            "host": "claude",
            "tier": "wrapper",
            "wrapper_identity": str(os.getpid()),
            "acquired_at": "2026-08-10T12:00:00+00:00",
            "schema_version": 1,
        },
    )
    with pytest.raises(ownership.OwnerAmbiguous, match="schema version 1") as excinfo:
        ownership.read(root, RUN)
    assert "conductor run disown" in str(excinfo.value)


def test_a_record_with_no_schema_version_at_all_is_refused(tmp_path):
    """Pre-versioning documents and hand-written ones land here. Same reasoning: an unknown
    provenance is not a licence to guess at the identity scheme."""
    root = str(tmp_path / "state")
    os.makedirs(runstate.run_dir(root, RUN))
    atomic.write_json_atomic(
        ownership.record_path(root, RUN),
        {
            "run_key": RUN,
            "host": "claude",
            "tier": "wrapper",
            "wrapper_identity": "4242",
            "acquired_at": "2026-08-10T12:00:00+00:00",
        },
    )
    with pytest.raises(ownership.OwnerAmbiguous, match="schema version"):
        ownership.read(root, RUN)


def test_acquire_refuses_while_a_live_identity_holds_the_run(tmp_path):
    root = str(tmp_path / "state")
    mine = _identity(os.getpid())
    with ownership.acquire(root, RUN, host="claude", wrapper_identity=mine):
        with pytest.raises(ownership.OwnerBusy, match=str(os.getpid())):
            with ownership.acquire(root, RUN, host="codex", wrapper_identity="999999"):
                pass


def test_acquire_takes_over_a_record_whose_identity_has_exited(tmp_path):
    """Provably exited is the only state that yields the run — not expiry, which is why this
    module carries no timer."""
    root = str(tmp_path / "state")
    dead = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    identity = _identity(dead.pid)
    dead.kill()
    dead.wait(timeout=30)
    os.makedirs(runstate.run_dir(root, RUN))
    atomic.write_json_atomic(
        ownership.record_path(root, RUN),
        {
            "run_key": RUN,
            "host": "claude",
            "tier": "wrapper",
            "wrapper_identity": identity,
            "acquired_at": "2026-08-10T12:00:00+00:00",
            "schema_version": ownership.RECORD_SCHEMA_VERSION,
        },
    )
    with ownership.acquire(root, RUN, host="codex", wrapper_identity="55") as record:
        assert record.host == "codex"


def test_acquire_refuses_a_record_it_cannot_interpret_rather_than_taking_over(tmp_path):
    """``None`` is not ``False``. An identity in a scheme no adapter recognises proves nothing
    about whether a process is running, and taking the run on that basis is the failure that
    loses work."""
    root = str(tmp_path / "state")
    with ownership.acquire(root, RUN, host="claude", wrapper_identity="wrapper-token"):
        with pytest.raises(ownership.OwnerBusy, match="liveness unknown"):
            with ownership.acquire(root, RUN, host="claude", wrapper_identity="other"):
                pass


def test_release_leaves_another_identitys_record_alone(tmp_path):
    root = str(tmp_path / "state")
    with ownership.acquire(root, RUN, host="claude", wrapper_identity="4242"):
        ownership.release(root, RUN, wrapper_identity="9999")
        survivor = ownership.read(root, RUN)
        assert survivor is not None and survivor.wrapper_identity == "4242"


def _identity(pid: int) -> str:
    """A real, checkable identity for ``pid``. Read while the process is alive, because
    ``starttime`` comes out of ``/proc`` and is gone once the process is."""
    identity = proc.local_identity(pid)
    assert identity is not None, f"could not mint an identity for pid {pid}"
    return identity


def _record_for(identity: str, *, host: str = "claude") -> ownership.OwnerRecord:
    return ownership.OwnerRecord(
        run_key=RUN,
        host=host,
        tier="wrapper",
        wrapper_identity=identity,
        acquired_at="2026-08-10T12:00:00+00:00",
    )


def test_liveness_separates_gone_from_uninterpretable():
    assert ownership.identity_is_live(_record_for(_identity(os.getpid()))) is True
    assert ownership.identity_is_live(_record_for("")) is None
    assert ownership.identity_is_live(_record_for("wrapper-token")) is None
    assert ownership.identity_is_live(_record_for("0")) is None
    # A BARE PID is uninterpretable, not live. This is the whole point of the schema bump: the
    # old reader answered "live" here, on a number with no reuse defence.
    assert ownership.identity_is_live(_record_for(str(os.getpid()))) is None
    # A host this build does not know cannot be asked, so no exit proof exists.
    assert ownership.identity_is_live(_record_for("proc:1:1:x", host="borg")) is None


def test_a_structured_identity_does_not_make_every_acquire_refuse_forever(tmp_path):
    """The regression that made this module unusable, pinned.

    ``identity_is_live`` parsed the identity with ``int()``. The first structured identity ever
    written made that raise, the function answer ``None``, and ``acquire`` refuse on ``live is
    not False`` — forever, including for the process that wrote the record. A second acquire by
    the SAME identity must proceed, and a re-acquire after a clean release must too."""
    root = str(tmp_path / "state")
    mine = _identity(os.getpid())
    with ownership.acquire(root, RUN, host="claude", wrapper_identity=mine) as first:
        assert first.wrapper_identity == mine
        with ownership.acquire(
            root, RUN, host="claude", wrapper_identity=mine
        ) as again:
            assert again.wrapper_identity == mine
    with ownership.acquire(root, RUN, host="claude", wrapper_identity=mine) as third:
        assert third.wrapper_identity == mine


def test_a_reaped_child_reads_as_exited():
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    identity = _identity(child.pid)
    child.kill()
    child.wait(timeout=30)
    assert ownership.identity_is_live(_record_for(identity)) is False


def test_a_recycled_pid_reads_as_exited_not_live():
    """The defence a bare pid could not have. Same pid, different start time: the process that
    was recorded is gone, whoever holds the number now."""
    live = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    try:
        scheme, pid, ticks, boot = _identity(live.pid).split(":")
        forged = f"{scheme}:{pid}:{int(ticks) + 1}:{boot}"
        assert ownership.identity_is_live(_record_for(forged)) is False
        assert live.poll() is None, (
            "the process died, so nothing was observed against it"
        )
    finally:
        live.kill()
        live.wait(timeout=30)


def test_a_record_from_a_previous_boot_reads_as_exited():
    scheme, pid, ticks, _ = _identity(os.getpid()).split(":")
    stale = f"{scheme}:{pid}:{ticks}:00000000-0000-0000-0000-000000000000"
    assert ownership.identity_is_live(_record_for(stale)) is False


def test_the_default_identity_is_verifiable_and_is_not_a_bare_pid(tmp_path):
    root = str(tmp_path / "state")
    with ownership.acquire(root, RUN, host="claude") as record:
        assert record.wrapper_identity != str(os.getpid())
        assert record.wrapper_identity.split(":")[:2] == ["proc", str(os.getpid())]
        assert ownership.identity_is_live(record) is True


# --- stale recovery -------------------------------------------------------------------------


def test_disown_clears_a_provably_exited_owner(tmp_path):
    root = str(tmp_path / "state")
    dead = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(120)"])
    identity = _identity(dead.pid)
    dead.kill()
    dead.wait(timeout=30)
    ownership.claim(root, RUN, host="claude", wrapper_identity=identity)
    outcome, detail = ownership.disown(root, RUN)
    assert outcome == "cleared" and "provably exited" in detail
    assert ownership.read(root, RUN) is None


def test_disown_refuses_a_live_owner_however_old_the_record(tmp_path):
    """Age is never the proof. A sixteen-day-old record naming a live process is a CORRECT
    refusal, and a timer that cleared it would fire a driver into an occupied checkout."""
    root = str(tmp_path / "state")
    ownership.claim(root, RUN, host="claude", wrapper_identity=_identity(os.getpid()))
    outcome, detail = ownership.disown(root, RUN)
    assert outcome == "refused"
    assert "positive exit proof, never on age" in detail
    assert "--force" in detail
    assert ownership.read(root, RUN) is not None


def test_force_clears_an_uninterpretable_record_that_nothing_can_prove_exited(tmp_path):
    """The one case with no other way out: without ``--force`` an operator's only recourse is
    deleting state files by hand, which is what a supported verb exists to prevent."""
    root = str(tmp_path / "state")
    ownership.claim(root, RUN, host="claude", wrapper_identity="wrapper-token")
    assert ownership.disown(root, RUN)[0] == "refused"
    outcome, _ = ownership.disown(root, RUN, force=True)
    assert outcome == "cleared"
    assert ownership.read(root, RUN) is None


def test_force_clears_a_refused_version_1_record(tmp_path):
    """A v1 record cannot be read, so it can never be proven exited — ``--force`` is the ONLY
    exit from it, and the refusal message says so."""
    root = str(tmp_path / "state")
    os.makedirs(runstate.run_dir(root, RUN))
    atomic.write_json_atomic(
        ownership.record_path(root, RUN),
        {
            "run_key": RUN,
            "host": "claude",
            "tier": "wrapper",
            "wrapper_identity": "4242",
            "acquired_at": "2026-08-10T12:00:00+00:00",
            "schema_version": 1,
        },
    )
    assert ownership.disown(root, RUN)[0] == "refused"
    assert ownership.disown(root, RUN, force=True)[0] == "cleared"
    assert ownership.read(root, RUN) is None


def test_disown_on_a_run_with_no_record_is_not_an_error(tmp_path):
    outcome, _ = ownership.disown(str(tmp_path / "state"), RUN)
    assert outcome == "none"


# --- inherited ownership --------------------------------------------------------------------


def test_a_descendant_of_the_owner_is_not_a_second_claimant():
    """A wrapper launches the driver, which launches the worker. Without this the wrapper's own
    driver would skip every fire on the record the wrapper just wrote."""
    record = _record_for(_identity(os.getpid()))
    env = {ownership.INHERITED_IDENTITY_ENV: record.wrapper_identity}
    assert ownership.is_inherited(record, env) is True
    assert ownership.is_inherited(record, {}) is False
    assert (
        ownership.is_inherited(record, {ownership.INHERITED_IDENTITY_ENV: ""}) is False
    )
    assert (
        ownership.is_inherited(
            record, {ownership.INHERITED_IDENTITY_ENV: "someone-else"}
        )
        is False
    )


def test_an_unsafe_run_key_never_becomes_a_path(tmp_path):
    with pytest.raises(ValueError, match="unsafe run key"):
        ownership.record_path(str(tmp_path), "../escape")
