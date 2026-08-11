"""The names a run key determines.

A run key is the single source of two derived names — the run's done-gate directory and its
integration branch — and this module is the single definition of both formats. Three callers
need them: ``schema.validate_run`` (to check a record against them), ``paths.resolve_gate`` (to
resolve a gate from a key), and ``run_cmd`` (to build a new run). Without one definition each
would carry its own literal copy, which is exactly the drift ``conductor/branches.py``'s header
records from review B-5: start and autodev each derived ``conductor/run-<slug>`` in prose, and
the two diverged.

It also owns the rule for what may BE one of those name components — ``is_safe_segment``. Three
modules enforce it: ``paths`` (before joining a segment into a gate path), ``schema`` (before
accepting a ``gate_dir`` into a record) and ``runkey`` (before accepting a run key). They used to
carry three independent copies, and two of them had already drifted: ``schema`` accepted
``assertions/a..b`` while ``paths`` refused it, so a record could be legal to write and
impossible to resolve.

A LEAF module on purpose. It imports nothing from ``conductor.paths`` or
``conductor.core.runkey`` — ``runkey`` already imports ``paths.spec_slug``, so a shared
definition living in either of those would make ``paths.py`` unable to use it without a cycle.
"""

from __future__ import annotations

import re
from typing import NamedTuple

GATE_DIR_PREFIX = "assertions/"
RUN_BRANCH_PREFIX = "conductor/run-"

_SAFE_SEGMENT = re.compile(r"[a-z0-9][a-z0-9._-]*\Z")


def is_safe_segment(segment: str) -> bool:
    """Whether ``segment`` is safe as ONE filesystem component and ONE git ref segment: starts
    alphanumeric, holds only ``[a-z0-9._-]``, contains no path separator, no ``..``, and does not
    end in ``.lock``.

    THE definition — ``paths``, ``schema`` and ``runkey`` all delegate here. Each clause earns its
    place: the leading-alphanumeric and character-class rules keep the segment a valid ref
    component and exclude ``/``; ``..`` is excluded separately because the character class permits
    dots, so ``a..b`` matches the pattern yet traverses; ``*.lock`` is refused because git reserves
    it for ref locks."""
    return (
        isinstance(segment, str)
        and bool(_SAFE_SEGMENT.match(segment))
        and ".." not in segment
        and not segment.endswith(".lock")
    )


class DerivedNames(NamedTuple):
    """The repository-relative gate directory and the integration branch for one run key."""

    gate_dir: str
    integration_branch: str


def derived_names(run_key: str) -> DerivedNames:
    """The two names ``run_key`` determines.

    Only meaningful for ``identity_scheme="path-hash-v2"``. A ``legacy-slug-v1`` run retains the
    gate directory and branch names recorded when it was migrated, and must never be compared
    against these — callers are responsible for that distinction."""
    return DerivedNames(f"{GATE_DIR_PREFIX}{run_key}", f"{RUN_BRANCH_PREFIX}{run_key}")
