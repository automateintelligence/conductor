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
"""

from __future__ import annotations

import os
import subprocess
from typing import NamedTuple

from conductor.core import locks, registry, runstate, schema, transaction

_GIT_TIMEOUT = 30.0


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
    ``--show-toplevel`` is not, which is why it is not used here."""
    base = start or os.environ.get("CONDUCTOR_HOME") or os.getcwd()
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


def _recover_pending(root: str) -> None:
    """Complete or reverse any unfinished transaction before this state root is READ.

    A crash between ``transaction.commit`` and ``transaction.apply`` leaves ``project.json`` and
    ``run.json`` holding their before images, so an un-recovered read sees the *previous* set of
    runs: a bare command would report no active run, or resolve to a run that a committed
    transaction has already replaced, and land work on the wrong branch. ``pending`` is checked
    before the lock is taken so resolving inside a repository that has no state root never
    creates one; ``transaction`` itself takes no locks, so recovering under ``project.lock`` —
    the same lock ``registry`` recovers under — cannot deadlock."""
    if not transaction.pending(root):
        return
    with locks.hold(registry.lock_path(root), kind="project"):
        transaction.recover(root)


def active_run_keys(root: str) -> list[str]:
    """Run keys whose RUN RECORD says active, checkpointed, or blocked, sorted.

    Reads ``run.json`` rather than the registry's status mirror: the mirror exists for the
    new-generation policy and cheap listing, and a stale mirror must never decide which run a
    bare command operates on."""
    _recover_pending(root)
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
    """The run this invocation means."""
    root = repo_root(start)
    sroot = os.path.join(root, ".conductor")
    _recover_pending(sroot)
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
        assert run is not None  # active_run_keys only returns keys whose record loaded
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
