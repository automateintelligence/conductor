"""Run a host process to completion under a wall-clock bound, killing its whole process group.

Shared process mechanics, not argv: the caller builds the vector (each adapter builds its own),
this module only runs it. Used by the read-only reviewer launch, whose bound is elapsed time —
unlike a worker fire, a review is short and a wedged one must not hold the caller forever.

Stdin is ``/dev/null`` because Codex subcommands hang on an open stdin (ground truth
2026-08-12); the child runs in its own session so the TERM/KILL reaches helpers it started,
not just the leader.
"""

from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass


@dataclass(frozen=True)
class Bounded:
    returncode: int | None  # None when the process was killed for exceeding the timeout
    stdout: str
    stderr: str
    duration_s: float
    timed_out: bool


def _signal_group(pgid: int, sig: signal.Signals) -> None:
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        pass


def _decode(data: bytes | str | None) -> str:
    # TimeoutExpired carries the partial output as bytes even when the Popen is in text mode.
    if data is None:
        return ""
    return data.decode("utf-8", errors="replace") if isinstance(data, bytes) else data


def _partial(child: subprocess.Popen, stream) -> str:
    """What ``communicate`` had read from ``stream`` before it was interrupted.

    CPython keeps the chunks on the Popen object between calls (it is what lets a second
    ``communicate`` after ``TimeoutExpired`` return the whole output); there is no public way
    to get them back after any other exception. Absent that attribute, nothing is returned.
    """
    chunks = getattr(child, "_fileobj2output", {}).get(stream)
    return _decode(b"".join(chunks)) if chunks else ""


def _stop(child: subprocess.Popen, pgid: int, grace: float) -> None:
    """TERM the group, give the leader ``grace`` seconds, then KILL the group and reap.

    The KILL sits in a ``finally`` so a second interruption during the grace wait still reaches
    it: an unsignalled reviewer would run on with nothing bounding or recording it."""
    try:
        _signal_group(pgid, signal.SIGTERM)
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass
    finally:
        _signal_group(pgid, signal.SIGKILL)
        for stream in (child.stdout, child.stderr):
            if stream is not None:
                stream.close()
        try:
            child.wait(timeout=grace)
        except subprocess.TimeoutExpired:
            pass


def _wait(
    child: subprocess.Popen, pgid: int, timeout: float, grace: float
) -> tuple[str | None, str | None, bool]:
    timed_out = False
    try:
        out, err = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _signal_group(pgid, signal.SIGTERM)
        try:
            out, err = child.communicate(timeout=grace)
        except subprocess.TimeoutExpired:
            out, err = None, None
        # KILL unconditionally: a leader that exited on TERM says nothing about a group member
        # that ignored it, and an unsignalled member would be orphaned still running.
        _signal_group(pgid, signal.SIGKILL)
        if out is None:
            try:
                out, err = child.communicate(timeout=grace)
            except subprocess.TimeoutExpired as late:
                # A descendant that left the group (setsid) still holds the pipes open, so
                # killpg cannot reach it and waiting for EOF would block until it exits. Give
                # up on the pipes, reap the leader, and return what was captured so far.
                out, err = _decode(late.stdout), _decode(late.stderr)
                for stream in (child.stdout, child.stderr):
                    if stream is not None:
                        stream.close()
                child.wait()
    return out, err, timed_out


def run_bounded(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
    grace: float = 5.0,
    env: Mapping[str, str] | None = None,
) -> Bounded:
    """Run ``argv`` for at most ``timeout`` seconds, then TERM its group, wait ``grace`` seconds, KILL
    it, and wait ``grace`` more for the pipes to close. Worst case is ``timeout + 2 * grace``.

    An ``OSError`` from starting the process propagates: nothing ran. Once it has started, the
    group never outlives this call. An ``OSError`` while waiting on it (reading its pipes)
    kills the group and comes back as a result with ``returncode=None``, ``timed_out=False``
    and the error in ``stderr``, because the host did run. Any other exception, including one a
    signal handler raises (``KeyboardInterrupt``, the caller's own), kills the group and then
    propagates.
    """
    started = time.monotonic()
    child = subprocess.Popen(
        argv,
        cwd=cwd,
        env=None if env is None else dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        encoding="utf-8",
        errors="replace",
        start_new_session=True,
    )
    pgid = child.pid  # start_new_session=True: the leader's pid is the group id
    try:
        out, err, timed_out = _wait(child, pgid, timeout, grace)
    except OSError as exc:
        partial = _partial(child, child.stdout)
        _stop(child, pgid, grace)
        return Bounded(
            returncode=None,
            stdout=partial,
            stderr=f"run_bounded: {type(exc).__name__}: {exc}",
            duration_s=time.monotonic() - started,
            timed_out=False,
        )
    except BaseException:
        _stop(child, pgid, grace)
        raise
    return Bounded(
        returncode=None if timed_out else child.returncode,
        stdout=out or "",
        stderr=err or "",
        duration_s=time.monotonic() - started,
        timed_out=timed_out,
    )
