"""Advisory file locks and the one global lock order.

Design §"Failure handling" fixes the order: ``migration.lock`` when applicable, then
``project.lock`` when applicable, then ``owner.lock``, then ``state.lock``; multi-run project
operations acquire run locks in sorted run-key order. Nothing enforces that but code, and a
violation shows up as an intermittent deadlock between a heartbeat and a takeover — the worst
possible failure mode for an unattended run. So the order is tracked per context and violations
raise immediately, naming both locks.

``flock`` is per open-file-description: two ``os.open`` calls in one process genuinely conflict.
Re-entrant acquisition is therefore refused rather than left to block until the timeout.
"""

from __future__ import annotations

import contextlib
import contextvars
import errno
import fcntl
import os
import time
from collections.abc import Iterator

LOCK_ORDER = ("migration", "project", "owner", "state")

_held: contextvars.ContextVar[tuple[tuple[str, int, str | None], ...]] = (
    contextvars.ContextVar("conductor_locks_held", default=())
)


class LockOrderError(RuntimeError):
    """A lock was requested out of the global order, re-entrantly, or under an unknown kind."""


class LockTimeout(RuntimeError):
    """Another holder kept the lock for the whole timeout window."""


def _rank(kind: str) -> int:
    try:
        return LOCK_ORDER.index(kind)
    except ValueError:
        raise LockOrderError(
            f"unknown lock kind {kind!r}; expected one of {LOCK_ORDER}"
        ) from None


def _check_order(kind: str, rank: int, run_key: str | None) -> None:
    for held_kind, held_rank, held_run in _held.get():
        if held_rank > rank:
            raise LockOrderError(
                f"lock-order violation: cannot take the {kind} lock while holding the "
                f"{held_kind} lock; the order is {' -> '.join(LOCK_ORDER)}"
            )
        if held_rank == rank and held_run == run_key:
            raise LockOrderError(
                f"re-entrant acquisition of the {kind} lock"
                + (f" for run {run_key!r}" if run_key else "")
                + " — flock is per open-file-description and this would deadlock"
            )
        if held_rank == rank and run_key is not None and held_run is not None:
            if run_key < held_run:
                raise LockOrderError(
                    f"lock-order violation: {kind} lock for {run_key!r} requested after "
                    f"{held_run!r}; multi-run operations take locks in sorted run-key order"
                )


@contextlib.contextmanager
def hold(
    path: str,
    *,
    kind: str,
    run_key: str | None = None,
    timeout: float = 30.0,
    poll: float = 0.05,
) -> Iterator[int]:
    """Hold an exclusive advisory lock at ``path`` for the block's duration.

    ``kind`` must be one of ``LOCK_ORDER``; ``run_key`` distinguishes per-run locks of the same
    kind so their sorted-order requirement can be checked."""
    rank = _rank(kind)
    _check_order(kind, rank, run_key)
    os.makedirs(os.path.dirname(os.path.abspath(path)) or ".", exist_ok=True)
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    deadline = time.monotonic() + timeout
    try:
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError as exc:
                if exc.errno not in (errno.EACCES, errno.EAGAIN):
                    raise
                if time.monotonic() >= deadline:
                    raise LockTimeout(
                        f"{kind} lock still held by another process after {timeout}s: {path}"
                    ) from None
                time.sleep(poll)
        token = _held.set(_held.get() + ((kind, rank, run_key),))
        try:
            yield fd
        finally:
            _held.reset(token)
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)
