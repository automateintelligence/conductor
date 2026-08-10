"""``<project>/.conductor/runs/<run-key>/run.json`` — one record per run.

Every mutation is a read-modify-write guarded by the short-lived ``state.lock`` and the state
revision (design §"Project and run identity"). Atomic replace prevents torn files; the revision
prevents lost updates when two short-lived host processes overlap. As with the registry,
``update`` reads *before* taking the lock so the retry path is exercised in normal operation.

``state.lock`` is only for run.json mutation. ``owner.lock`` — created here so the path is
single-sourced — is the execution-ownership lock and belongs to Plan 02; nothing in this module
interprets it.
"""

from __future__ import annotations

import datetime
import os
from collections.abc import Callable

from conductor.core import atomic, locks, schema
from conductor.core.runkey import is_safe_run_key


class RunMissing(RuntimeError):
    """No run record for this key under this state root."""


class RunExists(RuntimeError):
    """A run record already exists; creating one would discard history."""


class RevisionConflict(RuntimeError):
    """``run.json`` advanced underneath a writer that read an older revision."""


def _checked(run_key: str) -> str:
    if not isinstance(run_key, str) or not is_safe_run_key(run_key):
        raise ValueError(
            f"unsafe run key {run_key!r}; refusing to build a state path from it"
        )
    return run_key


def run_dir(state_root: str, run_key: str) -> str:
    return os.path.join(state_root, "runs", _checked(run_key))


def run_path(state_root: str, run_key: str) -> str:
    return os.path.join(run_dir(state_root, run_key), "run.json")


def state_lock_path(state_root: str, run_key: str) -> str:
    return os.path.join(run_dir(state_root, run_key), "state.lock")


def owner_lock_path(state_root: str, run_key: str) -> str:
    """The execution-ownership lock. Plan 02 owns its semantics; the path lives here so both
    plans cannot disagree about where it is."""
    return os.path.join(run_dir(state_root, run_key), "owner.lock")


def _now() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()


def load(state_root: str, run_key: str) -> dict | None:
    """The run record, or ``None`` when this run does not exist."""
    return atomic.read_json(run_path(state_root, run_key))


def create(state_root: str, run_key: str, doc: dict) -> dict:
    """Write a new run record. Refuses to overwrite an existing one — a new generation gets a
    new run key, and history is never replaced."""
    _checked(run_key)
    if doc.get("run_key") != run_key:
        raise ValueError(
            f"run document declares run_key {doc.get('run_key')!r} but is being written as "
            f"{run_key!r}"
        )
    schema.validate_run(doc)
    os.makedirs(run_dir(state_root, run_key), exist_ok=True)
    with locks.hold(
        state_lock_path(state_root, run_key), kind="state", run_key=run_key
    ):
        if load(state_root, run_key) is not None:
            raise RunExists(
                f"run {run_key!r} already exists at {run_path(state_root, run_key)}; no write "
                "occurred. Start a new generation with: conductor run new <spec.md> --new-run"
            )
        atomic.write_json_atomic(run_path(state_root, run_key), doc)
    return doc


def commit(state_root: str, run_key: str, doc: dict, *, expect_revision: int) -> dict:
    """Write ``doc`` if the on-disk revision still equals ``expect_revision``."""
    _checked(run_key)
    with locks.hold(
        state_lock_path(state_root, run_key), kind="state", run_key=run_key
    ):
        current = load(state_root, run_key)
        if current is None:
            raise RunMissing(
                f"no run record at {run_path(state_root, run_key)}; no write occurred. "
                "List known runs with: conductor run list --all"
            )
        if current["revision"] != expect_revision:
            raise RevisionConflict(
                f"run {run_key!r} moved from revision {expect_revision} to "
                f"{current['revision']} at {run_path(state_root, run_key)}; no write occurred. "
                "Re-read and retry."
            )
        proposed = dict(doc)
        proposed["revision"] = expect_revision + 1
        proposed["updated_at"] = _now()
        schema.validate_run(proposed)
        atomic.write_json_atomic(run_path(state_root, run_key), proposed)
        return proposed


def update(
    state_root: str, run_key: str, mutate: Callable[[dict], dict], *, attempts: int = 5
) -> dict:
    """Apply ``mutate`` to a private copy of the run record and commit it, retrying on conflict.

    ``mutate`` must not change ``revision`` or ``updated_at`` — ``commit`` owns both."""
    last: RevisionConflict | None = None
    for _ in range(max(1, attempts)):
        current = load(state_root, run_key)
        if current is None:
            raise RunMissing(
                f"no run record at {run_path(state_root, run_key)}; no write occurred. "
                "List known runs with: conductor run list --all"
            )
        expect = current["revision"]
        try:
            return commit(
                state_root,
                run_key,
                mutate(schema.clone(current)),
                expect_revision=expect,
            )
        except RevisionConflict as exc:
            last = exc
    raise RevisionConflict(
        f"run {run_key!r} changed under {attempts} attempts; no write occurred. "
        f"Last conflict: {last}"
    )


def set_status(state_root: str, run_key: str, status: str) -> dict:
    """Move the run to ``status`` if the transition is legal, stamping the matching timestamp."""

    def mutate(doc: dict) -> dict:
        schema.assert_transition(doc["status"], status)
        doc["status"] = status
        if status == "terminal":
            doc["completed_at"] = _now()
        elif status == "failed":
            doc["failed_at"] = _now()
        return doc

    return update(state_root, run_key, mutate)
