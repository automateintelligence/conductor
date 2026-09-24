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

IDENTITY IS THE ADAPTER'S BUSINESS, NOT THIS MODULE'S. An identity string is opaque here: this
module writes it, compares it for equality, and asks the adapter named by the record's ``host``
field whether the thing it names is still running. It never parses one. That boundary is what
lets the two hosts prove liveness by genuinely different mechanisms — Claude by ``(pid,
starttime, boot_id)`` from ``/proc``, Codex by a kernel-held ``flock`` keyed on a thread id —
without this module learning either. ``conductor.hosts.proc`` is imported for exactly one thing,
minting the identity of the acquiring process itself, and that module knows no host name.

NO PROCESS-NAME MATCHING REACHES THIS MODULE OR ANY MODULE IT CALLS. The cron guard this
replaced ran ``pgrep -f 'claude'``, which matched Conductor's own shells and missed every real
session; nothing in the chain from here to the kernel compares a process name to anything.
"""

from __future__ import annotations

import contextlib
import datetime
import fcntl
import os
from collections.abc import Iterator, Mapping
from typing import NamedTuple

from conductor.core import atomic, locks, runstate
from conductor.hosts import base as hostbase
from conductor.hosts import proc

#: Bumped 1 -> 2 when the identity stopped being a bare pid string.
#:
#: THE BUMP IS A SAFETY MECHANISM, NOT BOOKKEEPING. A v1 record's ``wrapper_identity`` is
#: ``str(os.getpid())`` — a number with no reuse defence and no boot id. Read by this build,
#: which asks an adapter about it, a bare pid parses as no known scheme and answers ``None``:
#: safe, but permanently ambiguous. Read the other way round — a v1 record interpreted by code
#: that BELIEVES reuse protection is present — a recycled pid answers "live" for a holder that
#: exited, or "exited" for one that has not, and the second direction fires a driver into an
#: occupied worktree. So an old record is REFUSED with instructions rather than guessed at.
RECORD_SCHEMA_VERSION = 2

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


class OwnerUnidentified(OwnerAmbiguous):
    """No identity could be minted for the caller, so it must not claim ownership.

    A SUBCLASS so every existing fail-closed handler keeps catching it, while a caller that
    wants to say something more useful than "ambiguous" can. It is raised when the host's
    session variable is absent — both are undocumented and can disappear in any release — or
    when ``/proc`` will not yield a start time or a boot id.

    Refusing to register is the SAFE direction and it is worth stating why, because the
    alternative looks harmless: a record naming an identity that nothing can later verify does
    not degrade to "no protection", it degrades to a permanent one. Every consultation would
    answer "cannot tell", every consumer treats that as occupied, and the run stops advancing
    with no process anywhere to point at.
    """


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


#: The identity a parent has ALREADY registered, handed down to the processes it launches.
#:
#: Without it the wrapper tier deadlocks against itself. ``conductor heartbeat`` takes ownership
#: and then runs the driver, whose whole job is to refuse to fire while this run has a live
#: owner — and the live owner it would find is the heartbeat that launched it. The driver would
#: skip every fire, forever, for the most correct-looking reason in the log.
#:
#: An explicit token passed down a process tree, NOT an inference. Nothing here compares process
#: names, walks for a "conductor-looking" ancestor, or assumes anything about who spawned whom:
#: a descendant either carries the exact string its ancestor recorded or it does not. A stale or
#: forged value is harmless in the direction that matters — the record it names must ALSO still
#: be the recorded owner and still be live, so at worst a descendant declines to block on a
#: record that is already about to be released.
INHERITED_IDENTITY_ENV = "CONDUCTOR_OWNER_IDENTITY"


#: The generated driver's fire lock, ``$PROJECT/.conductor/resume.lock``, held for the WHOLE of
#: one fire (``exec 9>"$LOCK"; flock -n 9``) and by every descendant that inherits that
#: descriptor. See ``running_fire`` for why ownership consults it.
FIRE_LOCK_NAME = "resume.lock"


def fire_lock_path(state_root: str) -> str:
    return os.path.join(state_root, FIRE_LOCK_NAME)


def running_fire(state_root: str) -> str | None:
    """A refusal sentence while a driver fire OTHER THAN THE CALLER'S OWN holds the project's
    fire lock, else ``None``.

    WHY OWNERSHIP ASKS. The record names the heartbeat WRAPPER, and the wrapper is not the last
    thing running: the driver it launched, and the host session under that, outlive a killed
    wrapper. Judged by the record alone the run then reads as free — the wrapper is provably
    gone — while a fire is still editing the checkout. ``resume.lock`` is the fact that outlives
    the wrapper: the driver takes it before firing and the kernel releases it only once every
    process sharing that descriptor has exited. So an owner's exit is an exit proof for the RUN
    only while this lock is free. (This is the ``same fact from the Python side`` the driver's
    own comment promises.) Recording the driver's identity instead was the alternative; it would
    widen the record's schema and still miss a host session that outlives a killed driver.

    A NON-BLOCKING PROBE, NOT ``/proc/locks``. The driver takes the lock through the ``flock``
    helper, which exits at once; the kernel files the lock under that helper's pid, and
    ``/proc/locks`` omits locks whose pid no longer exists — so a held driver lock is invisible
    there. Taking the lock with ``LOCK_NB`` and dropping it is the one test that sees it. The cost
    is that a driver starting in the same instant skips that one tick, logged as
    ``fire-skipped reason=lock-held``.

    THE CALLER'S OWN FIRE IS NOT A RIVAL. A cron-launched driver holds the lock while its worker
    registers with ``conductor run own``; refusing that worker would stop the run. The holder is
    identified by parentage plus a kernel fact — an open file description of this process or an
    ancestor HOLDS the lock, per ``proc.holds_flock`` — never by a process name, and never by merely
    having the file open. A caller outside that tree is refused."""
    path = fire_lock_path(state_root)
    try:
        fd = os.open(path, os.O_RDONLY | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError as exc:
        return (
            f"the driver fire lock {path} could not be opened ({exc}), so whether a fire is "
            "still running is unknown"
        )
    try:
        st = os.fstat(fd)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            pass
        except OSError as exc:
            return (
                f"the driver fire lock {path} could not be tested ({exc}), so whether a fire "
                "is still running is unknown"
            )
        else:
            return (
                None  # free: closing the descriptor below releases the probe's own lock
            )
    finally:
        os.close(fd)
    if any(proc.holds_flock(pid, st) for pid in proc.ancestor_pids(os.getpid())):
        return None
    return (
        f"a driver fire still holds {path} — its heartbeat wrapper may have exited, but the "
        "fire it launched has not. Let that fire finish (conductor driver status shows its "
        "log), or stop it, then retry"
    )


def record_path(state_root: str, run_key: str) -> str:
    """``owner.json``, beside ``owner.lock`` in the run directory."""
    return os.path.join(runstate.run_dir(state_root, run_key), "owner.json")


def is_inherited(record: OwnerRecord, env: Mapping[str, str]) -> bool:
    """Does ``record`` name an ownership this caller was launched underneath?

    True only on an exact match against ``INHERITED_IDENTITY_ENV``. A descendant of the process
    that owns the run is not a second claimant to be excluded; it is that owner, doing the work
    the ownership was taken for.
    """
    inherited = env.get(INHERITED_IDENTITY_ENV)
    return bool(inherited) and inherited == record.wrapper_identity


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
    version = doc.get("schema_version")
    if version != RECORD_SCHEMA_VERSION:
        raise OwnerAmbiguous(
            f"ownership record at {record_path(state_root, run_key)} is schema version "
            f"{version!r}; this build writes and reads version {RECORD_SCHEMA_VERSION}. A "
            f"version {version!r} record names its owner as a bare process id, which carries no "
            "defence against that id having been reused by an unrelated process — so believing "
            "it could either block this run forever or clear a live owner. It is refused rather "
            "than guessed at. If no process is still working on this run, clear it with:\n"
            f"  conductor run disown --run {run_key} --force"
        )
    return OwnerRecord(**{field: doc[field] for field in _FIELDS}).validated()


def identity_is_live(record: OwnerRecord) -> bool | None:
    """Is the session or process this record names still running?

    ``True`` live, ``False`` provably exited, ``None`` uninterpretable — a caller deciding whether
    it is safe to disturb a run must be able to tell "nobody is there" from "I cannot tell", and
    collapsing the third answer into either of the other two is how a scan either clears a live
    checkout or blocks forever on a garbage string.

    TAKES THE RECORD, NOT THE IDENTITY STRING, because the identity alone is not enough to
    interpret: the two hosts prove liveness by different kernel facts, and which one applies is
    the record's ``host`` field. The previous signature took the bare string and parsed it as an
    ``int``, which meant the FIRST structured identity ever written made ``int()`` raise, this
    function answer ``None``, and every subsequent ``acquire`` raise ``OwnerBusy`` forever —
    including for the process that wrote the record. That is why nothing here parses an identity
    any more; the adapter does.

    An adapter that cannot be loaded, or that raises, is ``None`` and never ``False``. A record
    written by a host this build does not know is exactly the case where an exit proof is
    unavailable, and inventing one there is what would clear a live owner.
    """
    try:
        adapter = hostbase.load(record.host)
    except hostbase.UnknownHost:
        return None
    try:
        return adapter.process_alive(record.wrapper_identity)
    except Exception:
        return None


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
    record = claim(
        state_root,
        run_key,
        host=host,
        wrapper_identity=wrapper_identity,
        tier=tier,
    )
    try:
        yield record
    finally:
        release(state_root, run_key, wrapper_identity=record.wrapper_identity)


def local_process_identity() -> str:
    """This process's own identity, for a caller that IS the thing to be excluded.

    The wrapper tier only: a ``conductor heartbeat`` supervising a fire lives for the fire's
    whole life, so its pid is a truthful answer to "what is working on this run". A caller
    inside a host REPL must NOT use this — the ``conductor`` CLI call it runs in exits within
    the second, and recording it would leave a record that reads as provably exited while the
    session is still working. Those callers use the adapter's ``session_identity`` instead.
    """
    identity = proc.local_identity(os.getpid())
    if identity is None:
        raise OwnerUnidentified(
            "no verifiable identity could be minted for this process: /proc did not yield "
            f"both a start time for pid {os.getpid()} and a boot id. Refusing to claim "
            "ownership rather than record an identity nothing can later check."
        )
    return identity


def claim(
    state_root: str,
    run_key: str,
    *,
    host: str,
    wrapper_identity: str | None = None,
    tier: str = "wrapper",
) -> OwnerRecord:
    """Write the ownership record for ``run_key``, refusing while a live owner holds it.

    Split out of ``acquire`` because the two tiers have different lifetimes and only one of them
    fits a context manager. A wrapper outlives its fire and can hold a ``with`` block around it.
    An in-session worker cannot: the process that registers is a ``conductor`` CLI call that
    exits immediately, while the session it speaks for keeps working for hours. That worker
    needs a bare write now and an explicit ``release`` later, which is exactly this function.
    """
    identity = (
        local_process_identity() if wrapper_identity is None else str(wrapper_identity)
    )
    if not identity:
        raise OwnerUnidentified(
            f"refusing to claim run {run_key!r} with an empty identity; no write occurred."
        )
    lock = runstate.owner_lock_path(state_root, run_key)
    os.makedirs(runstate.run_dir(state_root, run_key), exist_ok=True)
    with locks.hold(lock, kind="owner", run_key=run_key):
        existing = read(state_root, run_key)
        if existing is not None and existing.wrapper_identity != identity:
            live = identity_is_live(existing)
            if live is not False:
                raise OwnerBusy(
                    f"run {run_key!r} is owned by {existing.host} identity "
                    f"{existing.wrapper_identity} "
                    + ("(live)" if live else "(liveness unknown)")
                    + f", recorded at {record_path(state_root, run_key)}; no write occurred."
                    + (
                        ""
                        if live
                        else " This build cannot tell whether that identity is still "
                        "running. If you have confirmed no process is working on this run, "
                        f"clear it with: conductor run disown --run {run_key} --force"
                    )
                )
        if existing is None or existing.wrapper_identity != identity:
            fire = running_fire(state_root)
            if fire:
                raise OwnerBusy(
                    f"run {run_key!r} is not free: {fire}; no write occurred."
                )
        record = OwnerRecord(
            run_key=run_key,
            host=host,
            tier=tier,
            wrapper_identity=identity,
            acquired_at=_now(),
        ).validated()
        _write(state_root, run_key, record)
    return record


def disown(state_root: str, run_key: str, *, force: bool = False) -> tuple[str, str]:
    """Clear a record whose owner is provably gone. Returns ``(outcome, detail)``.

    Outcomes: ``"none"`` (there was no record), ``"cleared"``, ``"refused"``.

    THE SUPPORTED RECOVERY PATH, and it exists so operators do not learn to ``rm`` state files
    by hand. Every refusal this contract can produce lands on "occupied", which is the safe
    direction but leaves a human with nowhere to go unless clearing is a first-class verb.

    Without ``force`` only a POSITIVE EXIT PROOF clears the record — never age. A record naming
    a live process is a correct refusal however old it is, and a timer that cleared it would
    fire a driver into a checkout someone is working in. ``force`` is for the genuinely
    uninterpretable cases (a foreign host, an unloadable adapter, a schema this build refuses,
    ``hidepid`` hiding the target) where no proof is obtainable and only a human can supply the
    missing fact.

    NEITHER CLEARS A RECORD UNDER A RUNNING FIRE. A record is the run's admission, and while
    ``running_fire`` reports a driver fire holding ``resume.lock`` outside the caller's own
    process tree, clearing it hands the run to the next claimant underneath that fire — the
    same reason ``claim`` refuses. ``force`` supplies a missing exit proof for the RECORD; it is
    not evidence about the fire, so it does not override this. There is no override: the way
    out is to let the fire finish or stop it, which releases the lock.
    """
    lock = runstate.owner_lock_path(state_root, run_key)
    os.makedirs(runstate.run_dir(state_root, run_key), exist_ok=True)
    with locks.hold(lock, kind="owner", run_key=run_key):
        fire = running_fire(state_root)
        if fire:
            return (
                "refused",
                f"run {run_key!r}: {fire}; nothing was removed. --force does not override "
                "a running fire.",
            )
        try:
            record = read(state_root, run_key)
        except OwnerAmbiguous as exc:
            if not force:
                return "refused", str(exc)
            _write(state_root, run_key, None)
            return "cleared", f"forced removal of an unreadable record: {exc}"
        if record is None:
            return "none", f"run {run_key!r} has no ownership record."
        live = identity_is_live(record)
        if live is False or force:
            _write(state_root, run_key, None)
            proof = "provably exited" if live is False else "forced by the operator"
            return (
                "cleared",
                f"cleared {record.host} identity {record.wrapper_identity} ({proof}).",
            )
        return (
            "refused",
            f"run {run_key!r} is owned by {record.host} identity "
            f"{record.wrapper_identity} "
            + ("(live)" if live else "(liveness unknown)")
            + f", recorded {record.acquired_at}; nothing was removed. A record is cleared on a "
            "positive exit proof, never on age. If you have confirmed nothing is working on "
            f"this run, force it with:\n  conductor run disown --run {run_key} --force",
        )


def release(state_root: str, run_key: str, *, wrapper_identity: str) -> str | None:
    """Drop ownership if ``wrapper_identity`` still holds it. A no-op otherwise.

    Returns a refusal sentence, leaving the record in place, while a fire outside the caller's
    own process tree still holds ``resume.lock`` (see ``disown``): a wrapper whose driver left
    descendants running is not done with the run just because its own work returned. The record
    then names an identity that will exit, and ``claim`` frees it only once the fire is gone."""
    lock = runstate.owner_lock_path(state_root, run_key)
    with locks.hold(lock, kind="owner", run_key=run_key):
        try:
            current = read(state_root, run_key)
        except OwnerAmbiguous:
            return None
        if current is None or current.wrapper_identity != str(wrapper_identity):
            return None
        fire = running_fire(state_root)
        if fire:
            return (
                f"run {run_key!r}: {fire}; the ownership record of {wrapper_identity} was "
                "left in place."
            )
        _write(state_root, run_key, None)
        return None
