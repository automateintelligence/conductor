"""Journalled cross-file state writes.

``atomic.write_json_atomic`` makes each file durable on its own, but an operation that touches
``project.json`` and one or more ``run.json`` files can still crash *between* them and leave a
registry mapping a spec to a run key the run record disagrees with. Design §"Failure handling"
therefore requires the journal be written and fsynced first, and every project entry point
complete or reverse an unfinished transaction before reading mappings.

Lifecycle:

    prepare(...)   journal written with state="prepared" and every before/after image
    commit(...)    journal flipped to state="committed" — the point of no return
    apply(...)     after images written, journal removed

``recover`` reverses a ``prepared`` journal (restore the before images) and rolls a ``committed``
one forward (write the after images). Both directions are idempotent, so recovering twice, or
recovering a transaction that had already half-applied, converges on the same state.

LOCKING — READ THIS BEFORE ADDING A ``run.json`` WRITER. Callers hold ``project.lock`` and
nothing else: ``registry.init``, ``registry.commit``, ``resolve.recover_pending``,
``run_cmd.cmd_new`` and ``repoint.repoint`` all call ``recover`` under the project lock alone,
before they take any per-run lock. ``recover`` therefore takes the per-run ``state.lock`` itself,
for every run its journal touches, in sorted run-key order — project -> state, the global order
``locks._check_order`` enforces. It cannot derive those lock paths from an opaque absolute target
path, so each entry that needs one carries it: ``{"lock": {"path": ..., "run_key": ...}}``, set by
whoever prepared the transaction. AN ENTRY THAT WRITES A ``run.json`` WITHOUT A LOCK HINT IS A BUG
— it will be replayed with no serialisation against that run's own writers.

Locking alone is NOT sufficient, which is the subtler half. Replay restores an image verbatim, so
a writer that legitimately advanced a file after the journal was written would be rolled back to a
revision it has already passed, and that revision number would then be REUSED — a stale holder
expecting it would pass its own compare-and-swap and clobber newer state. So ``_write_image``
converges on the FILE rather than the journal: it applies an image only when doing so moves the
revision forward (see ``_regresses``). Idempotence is preserved — a second replay sees its own
result — and a genuinely-ahead file is left alone, its transaction dropped as superseded.

Plan 02's heartbeat and lease writers are the first concurrent ``run.json`` writers. They inherit
both rules: prepare with a lock hint, and never assume an unapplied journal will win.
"""

from __future__ import annotations

import contextlib
import os
import re

from conductor.core import atomic, locks

SCHEMA_VERSION = 1

_TXN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")


def txn_dir(state_root: str) -> str:
    """Where journals live: ``<state_root>/transactions``."""
    return os.path.join(state_root, "transactions")


def journal_path(state_root: str, txn_id: str) -> str:
    """The journal file for one transaction."""
    return os.path.join(txn_dir(state_root), f"{txn_id}.json")


def _check_id(txn_id: str) -> None:
    if not isinstance(txn_id, str) or not _TXN_ID_RE.match(txn_id) or ".." in txn_id:
        raise ValueError(f"unsafe transaction id {txn_id!r}")


def _check_entries(entries: list[dict]) -> None:
    if not isinstance(entries, list) or not entries:
        raise ValueError("a transaction needs at least one entry")
    for entry in entries:
        if not isinstance(entry, dict) or "path" not in entry:
            raise ValueError(
                f"transaction entry must be a mapping with 'path': {entry!r}"
            )
        path = entry["path"]
        if not isinstance(path, str) or not os.path.isabs(path):
            raise ValueError(f"transaction entry path must be absolute, got {path!r}")
        before, after = entry.get("before"), entry.get("after")
        for label, value in (("before", before), ("after", after)):
            if value is not None and not isinstance(value, dict):
                raise ValueError(
                    f"transaction entry {label!r} must be a mapping or None"
                )
        if before is None and after is None:
            raise ValueError(
                f"transaction entry for {path!r} is a no-op (before and after None)"
            )
        lock = entry.get("lock")
        if lock is not None:
            if not isinstance(lock, dict):
                raise ValueError(
                    f"transaction entry 'lock' must be a mapping: {lock!r}"
                )
            lock_path, lock_run = lock.get("path"), lock.get("run_key")
            if not isinstance(lock_path, str) or not os.path.isabs(lock_path):
                raise ValueError(
                    f"transaction entry lock path must be absolute, got {lock_path!r}"
                )
            if not isinstance(lock_run, str) or not lock_run:
                raise ValueError(
                    f"transaction entry lock needs a run_key, got {lock_run!r}"
                )


def prepare(state_root: str, txn_id: str, entries: list[dict]) -> str:
    """Record the intended write and fsync it. Targets are untouched. Returns the journal path."""
    _check_id(txn_id)
    _check_entries(entries)
    # At most one journal may be pending. Every entry point recovers before it prepares, so this
    # never fires in normal operation; when it does, two transactions are in flight against the
    # same state root and their entry sets may overlap. Replay order would then decide the final
    # image, and journal ids are caller-supplied strings replayed in sorted order — that is
    # lexicographic, not causal, so the loser could be whichever id happens to sort last. Refusing
    # keeps both journals intact for an operator instead of silently picking one.
    outstanding = [other for other in pending(state_root) if other != txn_id]
    if outstanding:
        raise ValueError(
            f"cannot prepare transaction {txn_id!r}: {', '.join(outstanding)} still pending under "
            f"{txn_dir(state_root)}. Recover first — every entry point calls recover() before it "
            "writes, so a journal here means another writer is mid-transaction."
        )
    path = journal_path(state_root, txn_id)
    atomic.write_json_atomic(
        path,
        {
            "schema_version": SCHEMA_VERSION,
            "txn_id": txn_id,
            "state": "prepared",
            "entries": [
                {
                    "path": e["path"],
                    "before": e.get("before"),
                    "after": e.get("after"),
                    "lock": e.get("lock"),
                }
                for e in entries
            ],
        },
    )
    return path


def _load_journal(state_root: str, txn_id: str) -> dict:
    doc = atomic.read_json(journal_path(state_root, txn_id))
    if doc is None:
        raise ValueError(
            f"no transaction journal for {txn_id!r} under {txn_dir(state_root)}"
        )
    if doc.get("state") not in ("prepared", "committed"):
        raise ValueError(
            f"transaction {txn_id!r} has unknown state {doc.get('state')!r}"
        )
    return doc


def commit(state_root: str, txn_id: str) -> None:
    """Flip the journal to ``committed``: from here recovery rolls forward, never back."""
    doc = _load_journal(state_root, txn_id)
    doc["state"] = "committed"
    atomic.write_json_atomic(journal_path(state_root, txn_id), doc)


def _regresses(path: str, image: dict) -> bool:
    """Whether writing ``image`` to ``path`` would move a revision BACKWARDS.

    Replay restores an image verbatim — that is what makes recovery idempotent, and it is also
    what makes it dangerous: a writer that legitimately advanced the file since the journal was
    written would be rolled back to a revision it has already passed, and the revision number
    would then be REUSED. A stale holder expecting that number would pass its compare-and-swap
    and overwrite newer state, which is a lost update that no lock can prevent, because the
    clobbering writer's own CAS succeeded.

    So recovery converges on the file rather than the journal: apply the image only when it moves
    the revision forward. Replaying twice is still a no-op (the second pass sees its own result),
    and a reversal that finds the target already at or past its before-image is likewise a no-op.
    When the on-disk revision is genuinely ahead, that state won a compare-and-swap against a
    reader that had already seen the pre-transaction file, so it is the newer decision and it
    stands; this transaction's intent is superseded and is dropped."""
    current = atomic.read_json(path)
    if current is None:
        return False
    have, want = current.get("revision"), image.get("revision")
    return isinstance(have, int) and isinstance(want, int) and have >= want


def _write_image(path: str, image: dict | None) -> None:
    if image is None:
        atomic.remove_durably(path)
        return
    if _regresses(path, image):
        return
    atomic.write_json_atomic(path, image)


def apply(state_root: str, txn_id: str) -> None:
    """Write every after image and remove the journal. Only valid once committed."""
    doc = _load_journal(state_root, txn_id)
    if doc["state"] != "committed":
        raise ValueError(
            f"transaction {txn_id!r} is {doc['state']!r}; commit before apply"
        )
    for entry in doc["entries"]:
        _write_image(entry["path"], entry.get("after"))
    atomic.remove_durably(journal_path(state_root, txn_id))


def pending(state_root: str) -> list[str]:
    """Transaction ids with an unfinished journal, sorted."""
    directory = txn_dir(state_root)
    try:
        names = os.listdir(directory)
    except FileNotFoundError:
        return []
    return sorted(n[: -len(".json")] for n in names if n.endswith(".json"))


def write_status(handled: list[str], *, phrase: str = "no write occurred") -> str:
    """The failure-report write-status field for a refusal that ran ``recover`` first.

    Every refusal in this package states whether a write occurred. ``recover`` writes: it rolls
    committed after-images forward and removes the journal, irreversibly. A caller that recovered
    and then refused therefore cannot say "no write occurred" — the operator would go looking for
    an unchanged ``project.json`` that has in fact changed. Given ``recover``'s return value, this
    is the phrase to use: the plain ``phrase`` when nothing was recovered, and a qualified one
    naming the completed transactions when something was."""
    if not handled:
        return phrase
    return f"no further write occurred (transaction(s) {', '.join(handled)} were completed first)"


def pending_states(state_root: str) -> dict[str, str]:
    """Each unfinished transaction id mapped to its journal state — exactly what ``recover`` will
    do with it: a ``committed`` one is rolled forward, a ``prepared`` one is reversed.

    A failure report written after a write died mid-operation has to tell the operator which of
    those two is waiting, because only the first one means the intended change survives."""
    return {
        txn_id: _load_journal(state_root, txn_id)["state"]
        for txn_id in pending(state_root)
    }


def recover(state_root: str) -> list[str]:
    """Complete or reverse every unfinished transaction; return the ids handled, sorted.

    Called by every project entry point *before* reading mappings. A journal that cannot be
    parsed raises rather than being skipped — an unreadable journal means the split-identity
    question is unanswerable, which is a fail-closed condition, not a clean state.

    The return value is not decorative: a caller that recovers and then REFUSES must report the
    write status honestly, which ``write_status`` derives from exactly this list."""
    handled: list[str] = []
    for txn_id in pending(state_root):
        doc = _load_journal(state_root, txn_id)
        forward = doc["state"] == "committed"
        with contextlib.ExitStack() as stack:
            # Hold every per-run lock the journal names, in sorted run-key order, for the whole
            # replay. Without this, recovery rewrites run.json while holding only the caller's
            # project.lock — the run's own writers serialize on state.lock and would never see
            # it coming. Callers hold project.lock and nothing finer, so project -> state is the
            # documented order; the journal carries the lock paths because this module is
            # deliberately generic over opaque absolute paths and cannot derive them.
            for lock in _entry_locks(doc):
                stack.enter_context(
                    locks.hold(lock["path"], kind="state", run_key=lock["run_key"])
                )
            for entry in doc["entries"]:
                _write_image(
                    entry["path"],
                    entry.get("after") if forward else entry.get("before"),
                )
        atomic.remove_durably(journal_path(state_root, txn_id))
        handled.append(txn_id)
    return handled


def _entry_locks(doc: dict) -> list[dict]:
    """The distinct per-run locks a journal's entries name, in sorted run-key order."""
    by_run: dict[str, dict] = {}
    for entry in doc["entries"]:
        lock = entry.get("lock")
        if isinstance(lock, dict) and isinstance(lock.get("run_key"), str):
            by_run.setdefault(lock["run_key"], lock)
    return [by_run[run] for run in sorted(by_run)]
