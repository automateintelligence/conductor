"""Per-run execution ownership: the record, and whether the process it names is still alive.

``owner.lock`` is the mutex; ``owner.json`` beside it is the record the mutex guards. They are
two files on purpose. The design reads as though the ownership fields live inside the lock file,
but Plan 01's durable-write contract finishes every write with ``os.replace``, which installs a
NEW INODE at the path. A process holding ``flock`` on the old inode keeps holding it — on a file
nothing will ever open again — while every later acquirer locks the new inode uncontended. The
mutex would stop excluding anything and nothing would fail. So the lock file is never written;
only the record is. (Plan 02, "Where this plan corrects the roadmap and the design", correction 1.)

SCOPE. This module carries exactly what A-DH-5 needs: a record that names a process identity, and
a liveness answer about that identity. Plan 02 owns the rest — leases, renewal, the two-tier
posture, takeover, prune, rebind, and the ``workstation_id``/``host_identity``/``heartbeat_id``
fields. Two consequences of that boundary are deliberate:

* **No lease, and therefore no expiry.** The design's rule is that expiry is necessary but never
  sufficient: a record outliving its holder is settled by proving the holder exited, not by a
  timer. A consumer that asks only "is the named process alive?" is the conservative half of that
  rule, and it is the half a refusal needs. Adding a timer here could only ever make a check stop
  refusing while a process is still running, which is the failure direction that loses data.
* **``read`` ignores unknown fields.** Plan 02 will write a wider record. Selecting the fields it
  needs rather than rejecting the document keeps that forward move from breaking this reader.

The identity written by ``acquire`` is the local process id of the acquiring process. PID reuse is
real and Plan 02 answers it with an exit proof; until then a recycled PID makes ``identity_is_live``
answer "live" for a holder that has exited. That is a false REFUSAL, never a false clear-to-proceed,
so it fails in the safe direction for every consumer here.
"""

from __future__ import annotations

import contextlib
import datetime
import os
from collections.abc import Iterator
from typing import NamedTuple

from conductor.core import atomic, locks, runstate

RECORD_SCHEMA_VERSION = 1

#: "wrapper": a shell/heartbeat process that outlives a single tool call and can hold the mutex
#: for its launched host's whole lifetime. "in-session": a fire inside a live host REPL, where
#: nothing outlives a tool call, so the RECORD carries admission and the mutex is taken only
#: around record mutations. Exclusion is decided by the record on both tiers.
TIERS = ("wrapper", "in-session")

_FIELDS = ("run_key", "host", "tier", "wrapper_identity", "acquired_at")


class OwnerBusy(RuntimeError):
    """A live owner holds this run; the caller must not proceed."""


class OwnerAmbiguous(RuntimeError):
    """Ownership state cannot be interpreted safely. Always fail-closed on this."""


class OwnerRecord(NamedTuple):
    run_key: str
    host: str
    tier: str
    wrapper_identity: str
    acquired_at: str

    def validated(self) -> OwnerRecord:
        if self.tier not in TIERS:
            raise OwnerAmbiguous(
                f"ownership record tier {self.tier!r}; expected one of {TIERS}"
            )
        if not self.wrapper_identity:
            raise OwnerAmbiguous(
                f"ownership record for {self.run_key!r} has no wrapper identity"
            )
        return self

    def as_doc(self) -> dict:
        doc: dict = {field: getattr(self, field) for field in _FIELDS}
        doc["schema_version"] = RECORD_SCHEMA_VERSION
        return doc


def record_path(state_root: str, run_key: str) -> str:
    """``owner.json``, beside ``owner.lock`` in the run directory."""
    return os.path.join(runstate.run_dir(state_root, run_key), "owner.json")


def read(state_root: str, run_key: str) -> OwnerRecord | None:
    """The recorded owner, or ``None``.

    Raises ``OwnerAmbiguous`` on a record that does not describe this run, or that is missing a
    field — a hand-edited or partially restored file must not be believed.

    Deliberately LOCK-FREE. Every writer publishes through ``atomic.write_json_atomic``, so a
    reader sees either the previous document or the complete new one and never a torn file. A
    diagnostic reader that took ``owner.lock`` would instead block behind a wrapper-tier holder
    that legitimately keeps the mutex for its whole fire, and would then have to interpret its own
    timeout — turning "who owns this run" into "who is holding a file descriptor right now",
    which is the question the record exists to replace.
    """
    doc = atomic.read_json(record_path(state_root, run_key))
    if doc is None:
        return None
    missing = [field for field in _FIELDS if field not in doc]
    if missing:
        raise OwnerAmbiguous(
            f"ownership record at {record_path(state_root, run_key)} is missing "
            f"{', '.join(missing)}; it names no usable owner. Inspect it, then remove it only "
            f"once you have confirmed no process is still working on run {run_key}."
        )
    if doc["run_key"] != run_key:
        raise OwnerAmbiguous(
            f"ownership record at {record_path(state_root, run_key)} names run "
            f"{doc['run_key']!r}, not {run_key!r}. Inspect both with: "
            f"conductor run show --run {run_key}"
        )
    return OwnerRecord(**{field: doc[field] for field in _FIELDS}).validated()


def identity_is_live(wrapper_identity: str) -> bool | None:
    """Is the process this identity names still running?

    ``True`` live, ``False`` provably exited, ``None`` uninterpretable — a caller deciding whether
    it is safe to disturb a run must be able to tell "nobody is there" from "I cannot tell", and
    collapsing the third answer into either of the other two is how a scan either clears a live
    checkout or blocks forever on a garbage string.
    """
    try:
        pid = int(wrapper_identity)
    except (TypeError, ValueError):
        return None
    if pid <= 0:
        return None
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # The process exists; it belongs to another user. Alive is the honest answer.
        return True
    except OSError:
        return None
    return True


def _write(state_root: str, run_key: str, record: OwnerRecord | None) -> None:
    """Replace or remove the record. Every caller holds ``owner.lock`` for this run — the lock
    file itself is never touched, so the atomic replace here cannot orphan anyone's lock."""
    path = record_path(state_root, run_key)
    if record is None:
        atomic.remove_durably(path)
        return
    atomic.write_json_atomic(path, record.validated().as_doc())


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


@contextlib.contextmanager
def acquire(
    state_root: str,
    run_key: str,
    *,
    host: str,
    wrapper_identity: str | None = None,
    tier: str = "wrapper",
) -> Iterator[OwnerRecord]:
    """Take execution ownership of ``run_key`` for the block's duration.

    Refuses with ``OwnerBusy`` while another record's identity is live, or while a record cannot
    be interpreted. The mutex is held only around the two record mutations: on the wrapper tier a
    caller may additionally hold ``owner.lock`` itself for its whole fire, which is strictly
    stronger, but exclusion does not depend on it — every acquirer refuses on a live RECORD, and
    the in-session tier has no process that could hold a descriptor that long.

    On exit the record is removed only if it is still ours. A record another identity has taken
    over is left alone; deleting it would hand the run to a third acquirer while its real owner
    is still working.
    """
    identity = str(os.getpid()) if wrapper_identity is None else str(wrapper_identity)
    lock = runstate.owner_lock_path(state_root, run_key)
    os.makedirs(runstate.run_dir(state_root, run_key), exist_ok=True)
    with locks.hold(lock, kind="owner", run_key=run_key):
        existing = read(state_root, run_key)
        if existing is not None and existing.wrapper_identity != identity:
            live = identity_is_live(existing.wrapper_identity)
            if live is not False:
                raise OwnerBusy(
                    f"run {run_key!r} is owned by {existing.host} identity "
                    f"{existing.wrapper_identity} "
                    + ("(live)" if live else "(liveness unknown)")
                    + f", recorded at {record_path(state_root, run_key)}; no write occurred."
                )
        record = OwnerRecord(
            run_key=run_key,
            host=host,
            tier=tier,
            wrapper_identity=identity,
            acquired_at=_now(),
        ).validated()
        _write(state_root, run_key, record)
    try:
        yield record
    finally:
        release(state_root, run_key, wrapper_identity=identity)


def release(state_root: str, run_key: str, *, wrapper_identity: str) -> None:
    """Drop ownership if ``wrapper_identity`` still holds it. A no-op otherwise."""
    lock = runstate.owner_lock_path(state_root, run_key)
    with locks.hold(lock, kind="owner", run_key=run_key):
        try:
            current = read(state_root, run_key)
        except OwnerAmbiguous:
            return
        if current is not None and current.wrapper_identity == str(wrapper_identity):
            _write(state_root, run_key, None)
