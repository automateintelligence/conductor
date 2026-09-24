"""The host a given run launches — durable, per project, one resolver.

The scheduler fires a generated shell driver out of cron. Whatever decides "spawn ``codex``,
not ``claude``" has to be readable from a bare cron environment with no session, no plugin
context, and no live agent to ask, which is why the answer is a file next to
``.conductor/run_branch`` rather than anything inferred at fire time.

Resolution order, most explicit first:

1. ``$CONDUCTOR_HOST`` — an operator override, validated.
2. ``<main-checkout>/.conductor/host`` — what ``/conductor:start`` recorded for this run.
3. ``claude`` — the legacy default.

Step 2 resolves the MAIN checkout, never the directory it was handed. A run lives in a linked
worktree and the driver exports that worktree as ``CONDUCTOR_HOME``, so the recording and every
later read of it start from different paths; joining ``.conductor/host`` onto each would make one
run answer ``codex`` from the owner checkout and ``claude`` from its own worktree.

Step 3 is the compatibility guarantee, not a guess: every run installed before this module
existed has no host file, and each of those must keep rendering and firing byte-for-byte as it
does today. A file that exists but does not name a supported host is the opposite case —
something wrote it — so it raises. Falling back there would launch the wrong agent, and a cron
log that says nothing about it is the failure class this repository is built around.
"""

from __future__ import annotations

import os
import subprocess

from conductor.core.atomic import write_atomic
from conductor.hosts.base import HOST_IDS, HostAdapter, UnknownHost, load

#: Operator override. Set it to move one fire onto the other host without editing state.
HOST_ENV = "CONDUCTOR_HOST"

#: The host every run recorded before A1 shipped. Absent file == this.
DEFAULT_HOST = "claude"


def _common_root(project_root: str) -> str:
    """The MAIN checkout for any path inside the repository; the path itself outside one.

    ``conductor.core.resolve.repo_root`` is the single implementation of this (dirname of
    ``--git-common-dir``, identical from a linked worktree and from the owner checkout) and
    ``resume_script.main_root`` already delegates to it. Resolving here rather than at each call
    site is what makes the run's host ONE answer: the driver records from the main checkout, but
    it exports the run WORKTREE as ``CONDUCTOR_HOME``, so preflight, plan-lint and the merge gate
    all ask from in there. Joining ``.conductor/host`` onto whatever root each was handed gave a
    Codex run claude's preflight roots and the wrong review marker, from inside its own worktree.

    A path that is not in a repository — a project nobody has ``git init``-ed, a plain temp dir —
    keeps the literal-path behaviour rather than failing: this is a leaf read on the launcher's
    path, and refusing to answer would be a worse failure than answering about the directory the
    caller actually named.
    """
    from conductor.core.resolve import repo_root

    try:
        return repo_root(project_root)
    except (subprocess.CalledProcessError, subprocess.TimeoutExpired, OSError):
        return project_root


def host_file(project_root: str) -> str:
    """``<main-checkout>/.conductor/host`` — the recorded host for this project's run."""
    return os.path.join(_common_root(project_root), ".conductor", "host")


def _validated(host_id: str, *, source: str) -> str:
    if host_id not in HOST_IDS:
        raise UnknownHost(
            f"unknown host {host_id!r} from {source}; supported hosts are {HOST_IDS}"
        )
    return host_id


def recorded(project_root: str) -> str | None:
    """What ``<project>/.conductor/host`` says, or ``None`` when nothing is recorded.

    Separate from ``resolve`` because two callers need to tell "this run has no host yet" apart
    from "this run is claude", and ``resolve`` deliberately cannot: it answers ``claude`` for
    both. ``driver.install`` uses the distinction to leave a live run's host alone while still
    writing one down for a run that has none — collapsing the two is how an ambient
    ``$CONDUCTOR_HOST`` in an operator's shell would repoint a running project.
    """
    path = host_file(project_root)
    try:
        with open(path, encoding="utf-8") as f:
            raw = f.read().strip()
    except FileNotFoundError:
        # ABSENCE, and only absence, means "this run predates the file". Every other read
        # failure — a directory at that path, an unreadable mode, a `.conductor` that is itself
        # a file — means a host IS recorded and this process cannot see it. Catching the whole
        # of OSError answered `claude` for all of them: the same silent wrong-agent launch the
        # garbage-contents branch below already refuses, arrived at from the other direction.
        return None
    return _validated(raw, source=path)


def resolve(project_root: str) -> str:
    """The host id this project's run launches. Never returns an unsupported host."""
    override = os.environ.get(HOST_ENV)
    if override is not None and override.strip():
        return _validated(override.strip(), source=f"${HOST_ENV}")
    return recorded(project_root) or DEFAULT_HOST


def record(project_root: str, host_id: str) -> str:
    """Record ``host_id`` as this project's host and return the file path.

    Validated BEFORE the write, so a typo never leaves a file behind that the next resolve
    would refuse — the run would then be unlaunchable until someone deleted it by hand.
    """
    _validated(host_id, source="record()")
    path = host_file(project_root)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    write_atomic(path, f"{host_id}\n")
    return path


def adapter(project_root: str) -> HostAdapter:
    """The adapter for this project's recorded host."""
    return load(resolve(project_root))


class HostConflict(Exception):
    """The declared host disagrees with what this run is already bound to. Nothing written."""


def _bound(project_root: str) -> str | None:
    """The host this run is already bound to, or ``None`` for a genuinely fresh project.

    A project with no host file but an installed Tier-B driver predates the file and fires
    ``claude`` — the same absence rule ``resolve`` applies — so it is bound to the default.
    """
    found = recorded(project_root)
    if found is not None:
        return found
    # Lazy: resume_script imports this module. `host_file` is `<main>/.conductor/host`, and
    # the driver lives in that same checkout.
    from conductor import resume_script

    main_checkout = os.path.dirname(os.path.dirname(host_file(project_root)))
    driver = resume_script.driver_script_path(main_checkout)
    return DEFAULT_HOST if os.path.exists(driver) else None


def _conflict(bound: str, declared: str, path: str) -> HostConflict:
    return HostConflict(
        f"host: this run is bound to `{bound}` ({path}), but you declared `{declared}`. "
        "Nothing was changed.\n"
        "  A run's host is what every fire, preflight, plan-lint and merge-gate reads, so a "
        "start never rewrites it. Either:\n"
        f"  - continue the run on `{bound}`: invoke "
        f"{load(bound).native_invocation('conductor:start')} from {bound}; or\n"
        f"  - move the run to `{declared}` on purpose: confirm no worker owns it "
        "(`conductor run owner-busy` exits 11), then run "
        f"`conductor driver install --worktree <run-worktree> --host {declared}`, which "
        "rewrites the driver and the recording together, and start again. If no driver is "
        f"installed yet (`conductor driver status` finds none), delete {path} instead."
    )


def install_lock(project_root: str) -> str:
    """The lock every writer of this project's host AND driver takes: the driver's install lock.

    One lock for both files because they are one durable fact — the recording names the host
    and the driver fires it — so a host write serialized on anything else could still land
    between a `driver install`'s script write and its record, and leave them disagreeing.
    ``kind="project"`` in the global order (`conductor.core.locks`); nothing below it is taken
    while it is held here.
    """
    from conductor import resume_script  # lazy: resume_script imports this module

    main_checkout = os.path.dirname(os.path.dirname(host_file(project_root)))
    return resume_script.install_lock_path(main_checkout)


def _needs_write(project_root: str, chosen: str, *, repoint: bool) -> bool:
    """The compare: does binding ``chosen`` require a write? Raises ``HostConflict`` when the
    run is bound to another host and ``repoint`` is not sanctioned. Reads only."""
    bound = _bound(project_root)
    if bound is not None and bound != chosen and not repoint:
        raise _conflict(bound, chosen, host_file(project_root))
    # A legacy driver-installed run declaring its own (default) host still writes, so the
    # answer no longer rests on the absence rule.
    return recorded(project_root) != chosen


def _bind(project_root: str, chosen: str, *, repoint: bool) -> bool:
    """Compare-and-write, UNDER the install lock the caller already holds. Returns ``wrote``.

    The only path that writes a host recording for a run: `declare` (preflight) and
    `driver install` both come through here, so a writer that has not taken the lock cannot
    exist rather than merely being discouraged. ``repoint`` is `driver install --host`'s
    sanctioned move of a run onto another host; a declaration never repoints.
    """
    from conductor.core import locks

    lock = install_lock(project_root)
    if not locks.is_held(lock):
        raise RuntimeError(
            f"host write attempted without holding the install lock {lock}"
        )
    if not _needs_write(project_root, chosen, repoint=repoint):
        return False
    record(project_root, chosen)
    return True


def declare(project_root: str, host_id: str) -> tuple[str, str, bool]:
    """The running agent names itself: record ``host_id`` unless the run is already bound.

    How ``conductor preflight --host`` establishes the host BEFORE preflight, plan-lint or
    anything else resolves it — ``resolve`` answers the legacy default for an unrecorded
    project, so a Codex start that recorded late had already been checked as Claude.

    Returns ``(host_id, host_file, wrote)`` — the host the caller must go on to use. Same id
    already recorded: no write. Raises ``HostConflict`` (nothing written) when the run is bound
    to another host, or when ``$CONDUCTOR_HOST`` names another host — it outranks the
    recording, so every later check in that shell would answer for it. Raises ``UnknownHost``
    for an unsupported id or an unreadable/garbage recording, and ``LockTimeout`` when a fire
    or an install held the lock for the whole wait.

    The check is repeated under the install lock before the write. The unlocked pass only
    decides whether the lock is needed at all: an outcome that writes nothing (same host, or a
    refusal) cannot corrupt anything, and a live fire holds this lock for its whole duration,
    so re-invoking start on a running project must not queue behind it.
    """
    from conductor import resume_script
    from conductor.core import locks

    chosen = _validated(host_id, source="declare()")
    path = host_file(project_root)
    override = (os.environ.get(HOST_ENV) or "").strip()
    if override and override != chosen:
        raise HostConflict(
            f"host: ${HOST_ENV}={override} is set in this environment and outranks any "
            f"recording, so every check after this would run as `{override}`, not "
            f"`{chosen}`. Nothing was changed. Unset ${HOST_ENV} (or set it to `{chosen}`) "
            "and run this again."
        )
    if not _needs_write(project_root, chosen, repoint=False):
        return chosen, path, False
    lock = install_lock(project_root)
    os.makedirs(os.path.dirname(lock), exist_ok=True)
    with locks.hold(lock, kind="project", timeout=resume_script.INSTALL_LOCK_TIMEOUT_S):
        return chosen, path, _bind(project_root, chosen, repoint=False)
