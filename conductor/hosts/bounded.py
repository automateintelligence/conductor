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


def run_bounded(
    argv: list[str],
    *,
    cwd: str,
    timeout: float,
    grace: float = 5.0,
    env: Mapping[str, str] | None = None,
) -> Bounded:
    """Run ``argv`` for at most ``timeout`` seconds, then TERM its group, ``grace`` seconds, KILL."""
    started = time.monotonic()
    child = subprocess.Popen(
        argv,
        cwd=cwd,
        env=None if env is None else dict(env),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        errors="replace",
        start_new_session=True,
    )
    pgid = child.pid  # start_new_session=True: the leader's pid is the group id
    timed_out = False
    try:
        out, err = child.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        timed_out = True
        _signal_group(pgid, signal.SIGTERM)
        try:
            out, err = child.communicate(timeout=grace)
        except subprocess.TimeoutExpired:
            _signal_group(pgid, signal.SIGKILL)
            out, err = child.communicate()
    return Bounded(
        returncode=None if timed_out else child.returncode,
        stdout=out or "",
        stderr=err or "",
        duration_s=time.monotonic() - started,
        timed_out=timed_out,
    )
