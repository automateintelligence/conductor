"""The deterministic run key.

    <spec-slug>-<short-hash-of-normalized-repository-relative-spec-path>[-g<N>]

Two properties earn the hash. First, ``spec_slug`` carries only the filename stem, so
``docs/specs/alpha.md`` and ``vendor/specs/alpha.md`` would otherwise map to the same run — the
relative-path hash separates them. Second, the hash is taken over the *repository-relative* path,
so moving the repository or conducting from a linked worktree does not change the key, and every
branch, worktree, gate directory, and run directory keeps its name.

Generation 1 omits the suffix so existing single-generation names stay short; generation 2 and
later append ``-g2``, ``-g3`` and so on, and that suffix is part of every derived name.
"""

from __future__ import annotations

import hashlib
import os
import pathlib
import re

from conductor.paths import spec_slug

HASH_LEN = 8

_KEY_RE = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")
_GEN_RE = re.compile(r"-g([1-9]\d*)\Z")


def _escapes_repo(rel: str) -> bool:
    """Check if a relative path escapes the repository via .. segments."""
    return rel == ".." or rel.startswith(".." + os.sep)


def normalize_spec_path(repo_root: str, spec_path: str) -> str:
    """The repository-relative POSIX path the key hashes.

    Accepts an absolute path or one already relative to ``repo_root``. Redundant segments are
    collapsed. A path that escapes the repository is refused: its key would not be reproducible
    from a different checkout."""
    root = os.path.realpath(repo_root)
    absolute = (
        os.path.normpath(spec_path)
        if os.path.isabs(spec_path)
        else os.path.normpath(os.path.join(root, spec_path))
    )
    relative = os.path.relpath(absolute, root)
    if _escapes_repo(relative):
        # The caller may have reached the repository through a symlinked alias (a
        # symlinked home, /tmp on macOS, a WSL mount): `root` is realpath'd but an
        # absolute spec_path is not, so relpath would compare a resolved path against
        # an unresolved one and report a file inside the repo as outside. Resolve once
        # and retry before refusing. A spec that is itself a symlink still keeps its
        # in-repo path — this runs only on the refusal path, so it rescues an alias and
        # never relocates a spec that already resolved inside the repository.
        relative = os.path.relpath(os.path.realpath(absolute), root)
    if _escapes_repo(relative):
        raise ValueError(
            f"spec path is outside the repository: {spec_path!r} is not under {root!r}"
        )
    return pathlib.PurePath(relative).as_posix()


def path_hash(normalized_spec_path: str) -> str:
    """The short hash component: the first ``HASH_LEN`` hex characters of the path's sha256."""
    return hashlib.sha256(normalized_spec_path.encode("utf-8")).hexdigest()[:HASH_LEN]


def run_key(normalized_spec_path: str, generation: int = 1) -> str:
    """The run key for a normalized spec path at ``generation`` (1-based)."""
    if generation < 1:
        raise ValueError(f"generation must be >= 1, got {generation}")
    base = f"{spec_slug(normalized_spec_path)}-{path_hash(normalized_spec_path)}"
    return base if generation == 1 else f"{base}-g{generation}"


def parse_generation(key: str) -> int:
    """The generation encoded in ``key``; an absent suffix means generation 1."""
    match = _GEN_RE.search(key)
    return int(match.group(1)) if match else 1


def is_safe_run_key(key: str) -> bool:
    """Whether ``key`` is safe as a single filesystem component and git ref segment: starts
    alphanumeric, contains only ``[a-z0-9._-]``, no separators, no ``..``, not ``*.lock``."""
    return bool(_KEY_RE.match(key)) and ".." not in key and not key.endswith(".lock")
