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

from conductor.core.names import is_safe_segment
from conductor.paths import spec_slug

HASH_LEN = 8

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
    # Containment is decided on the RESOLVED path, always. Deciding it lexically and resolving
    # only after a refusal leaves the reverse case open: `docs/specs/alpha.md` symlinked to
    # somewhere outside the repository is lexically inside, so it would never be resolved at all
    # and would key a run on content no other checkout has.
    lexical = os.path.relpath(absolute, root)
    resolved = os.path.relpath(os.path.realpath(absolute), root)
    if _escapes_repo(resolved):
        raise ValueError(
            f"spec path is outside the repository: {spec_path!r} is not under {root!r}"
        )
    # Containment proven, the KEY still prefers the lexical path. A spec that is a symlink to
    # another file inside the repository keeps its own path, so its run key does not change. The
    # resolved form is used only to rescue the alias case — the caller reached the repository
    # through a symlinked home, /tmp on macOS, or a WSL mount, so `root` is realpath'd while an
    # absolute spec_path is not, and the lexical comparison reports an in-repo file as outside.
    relative = lexical if not _escapes_repo(lexical) else resolved
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
    alphanumeric, contains only ``[a-z0-9._-]``, no separators, no ``..``, not ``*.lock``.

    A thin delegation to ``names.is_safe_segment``, which is THE definition of that rule. This
    used to be an independent copy of it, identical by coincidence rather than by construction;
    ``names`` is a leaf, so both this module and ``paths`` can share it without a cycle."""
    return is_safe_segment(key)
