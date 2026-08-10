"""Repository hygiene for project-local run state.

Design §"Project and run identity": before creating project-local worktrees or support state,
verify that ``.worktrees/`` and ``.conductor/`` are not tracked by Git. If either is already
tracked, fail closed and report the exact ``git rm -r --cached`` recovery command; otherwise
establish a local Git exclude and recheck it.

Tracked run state is not a cosmetic problem. ``owner.lock``, ``state.lock``, leases, heartbeat
records and compaction markers are per-machine facts; committing them puts one workstation's
ownership claims into every collaborator's checkout and into the run's own pull requests.
Assertions stay tracked — they are the run's audited evidence, not its scratch state.
"""

from __future__ import annotations

import os
import subprocess

from conductor.core import atomic

STATE_PATHS = (".conductor", ".worktrees")
_EXCLUDE_LINES = ("/.conductor/", "/.worktrees/")
_EXCLUDE_HEADER = (
    "# conductor: project-local run state is per-machine and never tracked"
)
# Probe files rather than the bare directories: a trailing-slash ignore pattern only matches a
# directory, and `git check-ignore` cannot classify a path that does not exist yet.
_IGNORE_PROBES = (".conductor/project.json", ".worktrees/conductor/probe/integration")
_GIT_TIMEOUT = 30.0


class TrackedStateError(RuntimeError):
    """Run state is tracked by Git, or could not be proven untracked/ignored."""


def _git(repo_root: str, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", "-C", repo_root, *args],
        capture_output=True,
        text=True,
        timeout=_GIT_TIMEOUT,
    )


def tracked_state_paths(repo_root: str) -> list[str]:
    """Tracked files under ``.conductor/`` or ``.worktrees/``."""
    out = _git(repo_root, "ls-files", "--", *STATE_PATHS)
    if out.returncode != 0:
        raise TrackedStateError(
            f"git ls-files failed in {repo_root} (rc={out.returncode}): "
            f"{(out.stderr or '').strip()}; no write occurred"
        )
    return [line for line in out.stdout.splitlines() if line.strip()]


def assert_state_paths_untracked(repo_root: str) -> None:
    """Fail closed if any run-state path is tracked, naming the exact recovery commands."""
    tracked = tracked_state_paths(repo_root)
    if not tracked:
        return
    raise TrackedStateError(
        f"refusing to create project-local run state in {repo_root}: "
        f"{len(tracked)} file(s) under {' or '.join(STATE_PATHS)} are tracked by git "
        f"(first: {tracked[0]}). No write occurred. Recover with:\n"
        f"  git -C {repo_root} rm -r --cached {' '.join(STATE_PATHS)}\n"
        f"  git -C {repo_root} commit -m 'stop tracking conductor run state'"
    )


def is_ignored(repo_root: str, relative: str) -> bool:
    """Whether git would ignore ``relative`` in this repository."""
    out = _git(repo_root, "check-ignore", "-q", "--", relative)
    if out.returncode == 0:
        return True
    if out.returncode == 1:
        return False
    raise TrackedStateError(
        f"git check-ignore failed in {repo_root} (rc={out.returncode}): "
        f"{(out.stderr or '').strip()}; no write occurred"
    )


def _exclude_file(repo_root: str) -> str:
    common = _git(repo_root, "rev-parse", "--path-format=absolute", "--git-common-dir")
    if common.returncode != 0:
        raise TrackedStateError(
            f"cannot locate the git common directory for {repo_root}: "
            f"{(common.stderr or '').strip()}; no write occurred"
        )
    return os.path.join(common.stdout.strip(), "info", "exclude")


def ensure_local_exclude(repo_root: str) -> None:
    """Make both state paths ignored, then prove it.

    A repository that already ignores them (its own ``.gitignore``, a global excludes file) is
    left untouched — writing redundant rules into someone else's exclude file is noise. The
    recheck is the point: if the paths are still not ignored after the write, that is a
    fail-closed condition, not a warning."""
    if all(is_ignored(repo_root, probe) for probe in _IGNORE_PROBES):
        return
    exclude = _exclude_file(repo_root)
    try:
        with open(exclude, encoding="utf-8") as handle:
            existing = handle.read()
    except FileNotFoundError:
        existing = ""
    present = {line.strip() for line in existing.splitlines()}
    missing = [line for line in _EXCLUDE_LINES if line not in present]
    if missing:
        body = (
            existing if (not existing or existing.endswith("\n")) else existing + "\n"
        )
        body += _EXCLUDE_HEADER + "\n" + "\n".join(missing) + "\n"
        atomic.write_atomic(exclude, body)
    unresolved = [p for p in _IGNORE_PROBES if not is_ignored(repo_root, p)]
    if unresolved:
        raise TrackedStateError(
            f"{', '.join(unresolved)} is still not ignored in {repo_root} after writing "
            f"{exclude}. Run state would be committed. Add these lines to .gitignore and retry:\n"
            + "\n".join(f"  {line}" for line in _EXCLUDE_LINES)
        )
