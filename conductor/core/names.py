"""The names a run key determines.

A run key is the single source of two derived names — the run's done-gate directory and its
integration branch — and this module is the single definition of both formats. Three callers
need them: ``schema.validate_run`` (to check a record against them), ``paths.resolve_gate`` (to
resolve a gate from a key), and ``run_cmd`` (to build a new run). Without one definition each
would carry its own literal copy, which is exactly the drift ``conductor/branches.py``'s header
records from review B-5: start and autodev each derived ``conductor/run-<slug>`` in prose, and
the two diverged.

A LEAF module on purpose. It imports nothing from ``conductor.paths`` or
``conductor.core.runkey`` — ``runkey`` already imports ``paths.spec_slug``, so a shared
definition living in either of those would make ``paths.py`` unable to use it without a cycle.
"""

from __future__ import annotations

from typing import NamedTuple

GATE_DIR_PREFIX = "assertions/"
RUN_BRANCH_PREFIX = "conductor/run-"


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
