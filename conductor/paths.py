"""Where a conductor run's state and done-gate live.

The plugin dir holds read-only TOOL CODE; a run's state + gate belong to the PROJECT — the git
repo you invoke conductor from. ``bin/conductor`` resolves the project once and exports
``CONDUCTOR_HOME`` so the runner, the freeze guard, and the handoff writer all agree on the same
location. Kept in one module so those callers cannot diverge.
"""

from __future__ import annotations

import glob
import hashlib
import os
import pathlib
import re
import subprocess


def project_root() -> str:
    """The PROJECT that owns run state + the done-gate: ``$CONDUCTOR_HOME``, else the git repo
    of the current directory, else the current directory. Distinct from the plugin dir (tool
    code), which must never hold a project's gate/state."""
    home = os.environ.get("CONDUCTOR_HOME")
    if home:
        return home
    try:
        top = subprocess.run(
            ["git", "rev-parse", "--show-toplevel"],
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
        ).stdout.strip()
        if top:
            return top
    except Exception:
        pass
    return os.getcwd()


# --- Per-spec done-gate location (multi-spec safety) -----------------------------------
#
# The done-gate (manifest.yaml, .frozen, tests, run/results.json) is a TRACKED path. Left
# flat at ``assertions/`` it is a single per-repo slot: two specs conducted in sibling
# worktrees each rebuild that one slot on their own branch and contend for it at the shared
# base — whichever merges last defines ``assertions/`` on the default branch and drops the
# other's frozen gate. Namespacing the gate at ``assertions/<slug>/`` lets sibling specs
# coexist. ``spec_slug`` is the single source of that slug (``branches.run_branch_name``
# reuses it) so the gate dir and the run branch never diverge.


def spec_slug(spec_path: str) -> str:
    """Deterministic ref-safe slug for a spec path — the SINGLE source shared by the run
    branch (``conductor/run-<slug>``) and the per-spec gate dir (``assertions/<slug>/``).

    Slug = the spec filename's stem, lowercased, non-``[a-z0-9._-]`` runs collapsed to one
    hyphen, dot runs to one dot, stripped of leading/trailing ``-``/``.``. A stem that
    strips to nothing, cannot start with an alphanumeric, or would end in ``.lock`` (all
    git-ref-invalid) falls back to a deterministic ``spec-<sha256[:8]>`` of the full path."""
    stem = pathlib.PurePath(spec_path).stem.lower()
    slug = re.sub(r"\.{2,}", ".", re.sub(r"[^a-z0-9._-]+", "-", stem)).strip("-.")
    if not slug or not re.match(r"[a-z0-9]", slug) or slug.endswith(".lock"):
        slug = "spec-" + hashlib.sha256(spec_path.encode()).hexdigest()[:8]
    return slug


def _run_branch_slug(root: str) -> str | None:
    """The slug from ``<root>/.conductor/run_branch`` (``conductor/run-<slug>``), else None.
    Present at RUN time (start writes it during topology setup) and equal, by construction,
    to ``spec_slug(<spec>)``."""
    prefix = "conductor/run-"
    try:
        with open(
            os.path.join(root, ".conductor", "run_branch"), encoding="utf-8"
        ) as f:
            name = f.read().strip()
    except OSError:
        return None
    return (
        name[len(prefix) :]
        if name.startswith(prefix) and len(name) > len(prefix)
        else None
    )


def _goal_slug(root: str) -> str | None:
    """The slug of the ``docs/specs/<name>.md`` named in ``<root>/.conductor/goal.md``, else
    None. Fallback source when ``run_branch`` is absent."""
    try:
        with open(os.path.join(root, ".conductor", "goal.md"), encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r"docs/specs/[^\s`'\"]+?\.md", text)
    return spec_slug(m.group(0)) if m else None


def gate_slug(repo_root: str | None = None) -> str | None:
    """The slug that namespaces this run's done-gate, or None for the flat legacy gate.

    Discovered, in precedence order, from:
      1. ``$CONDUCTOR_GATE_SLUG`` — the explicit SETUP-time override. ``run_branch`` and
         ``goal.md`` are written AFTER ``/conductor:start`` builds+freezes the gate, so the
         start skill exports this (derived from ``conductor run-branch name <spec>``) for
         its build-time gate ops;
      2. ``<root>/.conductor/run_branch`` — present at run time (autodev, the driver);
      3. ``<root>/.conductor/goal.md``'s spec — fallback.
    None present -> None -> the flat legacy ``assertions/`` gate."""
    env = os.environ.get("CONDUCTOR_GATE_SLUG")
    if env:
        return env
    root = repo_root or project_root()
    return _run_branch_slug(root) or _goal_slug(root)


def gate_dir(repo_root: str | None = None) -> str:
    """The directory holding THIS run's done-gate (manifest + baseline + results/).

    Resolution, in precedence order:
      1. ``$CONDUCTOR_GATE_DIR`` — wins outright.
      2. An EXPLICIT ``$CONDUCTOR_GATE_SLUG`` — ``assertions/<slug>/`` with NO flat fallback.
         The env slug is a deliberate "this run is namespaced" signal (``/conductor:start``
         exports it for the step-3 build/lint/freeze). Forcing the dir means a not-yet-written
         or mis-written manifest FAILS CLOSED (runner exit 2 / verify unloadable) instead of
         silently validating or freezing the legacy flat gate under a new spec.
      3. An AMBIENT slug (from ``.conductor/run_branch`` or ``goal.md``) — ``assertions/<slug>/``
         only once that dir holds a ``manifest.yaml`` OR a ``.frozen`` baseline; otherwise the
         flat legacy ``assertions/``. This existence gate is fail-safe toward the legacy layout:
         a repo with an in-place flat gate, or a stale ``.conductor/`` left by a finished run
         whose slug has no dir, keeps reading its flat gate untouched.
      4. No slug -> flat ``assertions/``.

    Integrity (§5): the ``.frozen`` leg of (3) is load-bearing. Once a namespaced gate is
    FROZEN, that dir owns the run even if its ``manifest.yaml`` is later deleted or renamed —
    the resolver must NOT fall back to the flat gate, or a worker could shed a frozen baseline
    by removing the manifest and have ``gate verify`` / ``assert run`` read a green flat slot.
    Keeping the frozen dir makes the missing manifest fail closed under that baseline."""
    env = os.environ.get("CONDUCTOR_GATE_DIR")
    if env:
        return env
    root = repo_root or project_root()
    flat = os.path.join(root, "assertions")
    explicit = os.environ.get("CONDUCTOR_GATE_SLUG")
    if explicit:
        return os.path.join(flat, explicit)
    slug = gate_slug(root)
    if slug:
        nsdir = os.path.join(flat, slug)
        if os.path.isfile(os.path.join(nsdir, "manifest.yaml")) or os.path.isfile(
            os.path.join(nsdir, ".frozen")
        ):
            return nsdir
        # The ambient slug (from .conductor/run_branch / goal.md) resolves to an unbuilt dir.
        # Falling back to the flat gate is safe ONLY when the repo has NOT adopted per-spec
        # gates. If a frozen namespaced gate exists, stale/corrupt run metadata (an edited
        # run_branch) must NOT silently abandon it for a — possibly green — flat slot: keep
        # nsdir so the missing manifest fails closed (assert run exit 2). (codex P1)
        if has_namespaced_frozen_gate(root):
            return nsdir
    return flat


def has_namespaced_frozen_gate(repo_root: str | None = None) -> bool:
    """True if any ``assertions/<slug>/.frozen`` exists — the repo has FROZEN per-spec gates.
    Once it has, an ambient slug that doesn't resolve to a built gate is stale/corrupt run
    metadata and must fail closed rather than fall back to the flat gate. The flat baseline
    ``assertions/.frozen`` is NOT namespaced (no subdir) and never counts here."""
    root = repo_root or project_root()
    return bool(glob.glob(os.path.join(root, "assertions", "*", ".frozen")))


def manifest_path(repo_root: str | None = None) -> str:
    """The done-gate manifest: ``$CONDUCTOR_MANIFEST`` if set, else ``gate_dir()/manifest.yaml``."""
    return os.environ.get("CONDUCTOR_MANIFEST") or os.path.join(
        gate_dir(repo_root), "manifest.yaml"
    )


def baseline_path(repo_root: str | None = None) -> str:
    """The freeze baseline: ``$CONDUCTOR_FREEZE_BASELINE`` if set, else ``gate_dir()/.frozen``."""
    return os.environ.get("CONDUCTOR_FREEZE_BASELINE") or os.path.join(
        gate_dir(repo_root), ".frozen"
    )


def run_dir(repo_root: str | None = None) -> str:
    """Where the runner writes ``results.json`` — beside the manifest, so a per-spec gate's
    results never overwrite another spec's at a shared flat ``assertions/run/``."""
    return os.path.join(os.path.dirname(manifest_path(repo_root)), "run")
