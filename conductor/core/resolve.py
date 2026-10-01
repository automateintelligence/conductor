"""Where a run's state lives, and which run an invocation means.

Two decisions live here, and nowhere else:

1. THE CANONICAL STATE ROOT. Resolved from the repository's Git common directory, so starting
   from a linked worktree finds the same ``<main-checkout>/.conductor`` as the main checkout.
   Phase and integration worktrees therefore never grow their own registries.

2. WHICH RUN. When an invocation carries a run key, that key alone determines the run — legacy
   ``.conductor/run_branch``, legacy ``.conductor/goal.md``, and ambient ``CONDUCTOR_GATE_*``
   variables are ignored rather than consulted as fallback. Without a key, resolution succeeds
   only when exactly one ACTIVE run exists; zero or several fail with the available keys and the
   exact commands, because guessing is how a fire lands work on the wrong run's branch.

One narrower question lives here too: which run a given WORKTREE belongs to
(``run_for_worktree``), answered by the binding rule ``worktree_unbound`` — the same rule the
heartbeat applies to its driver — for callers that know where they ran but not which run.
"""

from __future__ import annotations

import os
import subprocess
from typing import NamedTuple

from conductor import paths
from conductor.core import locks, registry, runstate, schema, transaction

_GIT_TIMEOUT = 30.0
_BINDING_GIT_TIMEOUT = float(os.environ.get("CONDUCTOR_GIT_TIMEOUT", "30"))

#: Statuses a run worktree can still be bound to: the active set, plus a run whose final fire
#: moved it to awaiting-team-merge.
BINDABLE_STATUSES = (*schema.ACTIVE_STATUSES, "awaiting-team-merge")


class RunNotFound(RuntimeError):
    """The named run does not exist, or no active run exists to default to."""


class RunAmbiguous(RuntimeError):
    """Several runs are active and the invocation carried no run key."""


class RunResolution(NamedTuple):
    """A fully-resolved run: where its state lives and what it currently says."""

    state_root: str
    repo_root: str
    run_key: str
    run_dir: str
    run: dict


def repo_root(start: str | None = None) -> str:
    """The MAIN checkout root for any path inside the repository.

    ``--git-common-dir`` is identical from the owner checkout and from a linked run worktree;
    ``--show-toplevel`` is not, which is why it is not used here.

    Only an ABSENT ``start`` falls back to the ambient project. A supplied one is passed to git
    as given, empty string included: ``resume_script.main`` hands ``--project`` straight to
    ``main_root`` with no or-chain of its own, so treating ``""`` as "not supplied" would let
    ``CONDUCTOR_HOME`` name a different project than the one the operator asked for, and
    ``uninstall-cron`` would report success while the target project's cron lines kept firing."""
    base = (
        start
        if start is not None
        else (os.environ.get("CONDUCTOR_HOME") or os.getcwd())
    )
    common = subprocess.run(
        ["git", "-C", base, "rev-parse", "--path-format=absolute", "--git-common-dir"],
        check=True,
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    ).stdout.strip()
    return os.path.dirname(common)


def state_root(start: str | None = None) -> str:
    """The canonical project-local state root: ``<main-checkout>/.conductor``."""
    return os.path.join(repo_root(start), ".conductor")


def repo_identity(root: str) -> dict:
    """Stable repository identity for ``project.json``: the oldest root commit plus the
    configured origin URL. The root commit survives renaming the checkout or changing remotes;
    the URL is recorded for diagnostics only. ``rev-list`` prints newest first, so a repository
    with several roots (a grafted import) still yields the same oldest one every time."""

    def _capture(*args: str) -> str | None:
        out = subprocess.run(
            ["git", "-C", root, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
        if out.returncode != 0:
            return None
        lines = [
            line.strip() for line in (out.stdout or "").splitlines() if line.strip()
        ]
        return lines[-1] if lines else None

    return {
        "root_commit": _capture("rev-list", "--max-parents=0", "HEAD"),
        "origin_url": _capture("remote", "get-url", "origin"),
    }


def recover_pending(state_root: str) -> list[str]:
    """Complete or reverse every unfinished transaction under ``state_root``; return the ids
    handled, sorted.

    CALL THIS FROM AN ENTRY POINT, BEFORE ACQUIRING ANY LOCK. It takes ``project.lock`` itself,
    so calling it while already holding the project, owner or state lock raises
    ``locks.LockOrderError``. It is deliberately NOT called by ``resolve`` or ``active_run_keys``:
    those are leaf reads that must stay callable from under a lock, and a recovery hidden inside
    them would fail only when a journal happened to be pending — an intermittent unattended-run
    failure of exactly the kind ``locks``' global order exists to prevent.

    Why an entry point must call it at all: a crash between ``transaction.commit`` and
    ``transaction.apply`` leaves ``project.json`` and ``run.json`` holding their BEFORE images,
    so an unrecovered read sees the previous set of runs — a bare command reports no active run
    for a run that is already committed, or resolves to a run a committed transaction has
    replaced, and lands work on the wrong branch.

    ``transaction.pending`` is checked before the lock is taken, so calling this against a
    repository that has no state root never creates one. A journal appearing in that window
    belongs to a live writer, which holds ``project.lock`` across prepare/commit/apply and
    finishes its own transaction; the next entry point recovers anything it truly abandoned."""
    if not transaction.pending(state_root):
        return []
    with locks.hold(registry.lock_path(state_root), kind="project"):
        return transaction.recover(state_root)


def active_run_keys(root: str) -> list[str]:
    """Run keys whose RUN RECORD says active, checkpointed, or blocked, sorted.

    Reads ``run.json`` rather than the registry's status mirror: the mirror exists for the
    new-generation policy and cheap listing, and a stale mirror must never decide which run a
    bare command operates on.

    A pure read: takes no lock, so it is safe under one. Recovering a pending transaction is
    ``recover_pending``'s job, and the entry point's to call."""
    doc = registry.load(root)
    if doc is None:
        return []
    keys = []
    for key in registry.run_keys(doc):
        run = runstate.load(root, key)
        if run is not None and schema.is_active(str(run.get("status", ""))):
            keys.append(key)
    return sorted(keys)


def resolve(*, run_key: str | None = None, start: str | None = None) -> RunResolution:
    """The run this invocation means.

    A pure read, like ``active_run_keys``: safe to call while holding a lock. An entry point that
    may be running after a crash calls ``recover_pending`` first."""
    root = repo_root(start)
    sroot = os.path.join(root, ".conductor")
    if run_key is not None:
        run = runstate.load(sroot, run_key)
        if run is None:
            raise RunNotFound(
                f"no run {run_key!r} under {sroot}; no write occurred. "
                "List known runs with: conductor run list --all"
            )
        return RunResolution(
            sroot, root, run_key, runstate.run_dir(sroot, run_key), run
        )
    active = active_run_keys(sroot)
    if len(active) == 1:
        key = active[0]
        run = runstate.load(sroot, key)
        if run is None:
            # active_run_keys loaded this record a moment ago, so reaching here means it was
            # removed mid-resolution. Rare, but it gets the same treatment as every other
            # failure here: name the run, the path, that nothing was written, and the way out.
            raise RunNotFound(
                f"run {key!r} was listed as active but its record at "
                f"{runstate.run_path(sroot, key)} disappeared mid-resolution; no write "
                "occurred. Re-run the command, or list known runs with: "
                "conductor run list --all"
            )
        return RunResolution(sroot, root, key, runstate.run_dir(sroot, key), run)
    if not active:
        raise RunNotFound(
            f"no active run under {sroot}; no write occurred. "
            "Start one with: conductor run new <spec.md>  |  inspect history with: "
            "conductor run list --all"
        )
    listing = "\n".join(f"  conductor run show --run {key}" for key in active)
    raise RunAmbiguous(
        f"{len(active)} active runs under {sroot} and no --run given; no write occurred.\n"
        f"Active run keys: {', '.join(active)}\n"
        f"Re-run the command with one of:\n{listing}"
    )


def gate_for_run(res: RunResolution) -> paths.GateResolution:
    """The done-gate this run owns. The one place that pairs a loaded run record with
    ``paths.resolve_gate``'s run-key mode, so no caller has to remember to pass both."""
    return paths.resolve_gate(res.repo_root, run_key=res.run_key, run=res.run)


class Unbound(NamedTuple):
    """Why a worktree is not provably a run's. ``foreign``: it is not a work tree of this
    repository (``detail`` says what it is instead). Otherwise it is one, but with a branch
    checked out that is not the run's (``detail`` names it)."""

    foreign: bool
    detail: str


def _binding_git(worktree: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", worktree, *args],
        capture_output=True,
        text=True,
        timeout=_BINDING_GIT_TIMEOUT,
        check=False,
    )


def worktree_unbound(repo_root: str, worktree: str, run: dict) -> Unbound | None:
    """``None`` when ``worktree`` provably belongs to ``run``, else why not.

    Bound means: its realpath is the ``integration_worktree`` or ``phase_worktree`` the run
    records, or it is a work tree OF THIS REPOSITORY with the run's integration or phase branch
    checked out. A branch name alone is no evidence: a clone elsewhere with the same branch
    checked out is another repository's work tree, so the worktree must share ``repo_root``'s
    git common dir before its branch counts."""
    real = os.path.realpath(worktree)
    recorded = {
        os.path.realpath(path)
        for path in (run.get("integration_worktree"), run.get("phase_worktree"))
        if isinstance(path, str) and path
    }
    if real in recorded:
        return None
    branches_of_run = [
        name
        for name in (run.get("integration_branch"), run.get("phase_branch"))
        if isinstance(name, str) and name
    ]
    common = _binding_git(
        worktree, "rev-parse", "--path-format=absolute", "--git-common-dir"
    )
    common_dir = (common.stdout or "").strip() if common.returncode == 0 else ""
    if not common_dir or os.path.realpath(
        os.path.dirname(common_dir)
    ) != os.path.realpath(repo_root):
        return Unbound(
            True,
            f"belongs to the repository at {os.path.dirname(common_dir)}"
            if common_dir
            else f"is not a git work tree git could resolve (exit {common.returncode}: "
            f"{(common.stderr or '').strip() or 'no output'})",
        )
    head = _binding_git(worktree, "symbolic-ref", "--quiet", "--short", "HEAD")
    checked_out = (head.stdout or "").strip() if head.returncode == 0 else None
    if checked_out and checked_out in branches_of_run:
        return None
    return Unbound(
        False,
        f"branch {checked_out!r}"
        if checked_out
        else f"no branch git could name (exit {head.returncode}: "
        f"{(head.stderr or '').strip() or 'no output'})",
    )


def run_for_worktree(worktree: str) -> RunResolution:
    """The run ``worktree`` belongs to, for a caller that knows where it ran but carries no
    run key (the driver's post-fire usage ingest, ``conductor review``).

    The BINDING decides first: the unique run in ``BINDABLE_STATUSES`` that ``worktree_unbound``
    binds ``worktree`` to — so a run whose final fire moved it to awaiting-team-merge keeps its
    own worktree even while another run is the only active one. Several bound runs raise
    ``RunAmbiguous``. Only when no run is bound does the bare resolution apply (the one active
    run, else its ``RunNotFound`` / ``RunAmbiguous``). A pure read, like ``resolve``."""
    root = repo_root(worktree)
    sroot = os.path.join(root, ".conductor")
    doc = registry.load(sroot)
    bound: list[tuple[str, dict]] = []
    for key in registry.run_keys(doc) if doc is not None else []:
        run = runstate.load(sroot, key)
        if run is None or run.get("status") not in BINDABLE_STATUSES:
            continue
        if worktree_unbound(root, worktree, run) is None:
            bound.append((key, run))
    if len(bound) == 1:
        key, run = bound[0]
        return RunResolution(sroot, root, key, runstate.run_dir(sroot, key), run)
    if bound:
        keys = ", ".join(key for key, _ in bound)
        raise RunAmbiguous(
            f"{len(bound)} runs under {sroot} are bound to the worktree "
            f"{os.path.realpath(worktree)} ({keys}); no write occurred."
        )
    return resolve(start=worktree)
