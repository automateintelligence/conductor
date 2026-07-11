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
from typing import NamedTuple


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


def _ambient_slug(root: str) -> tuple[str | None, str]:
    """The slug from AMBIENT run metadata + how it was found: ``.conductor/run_branch`` first
    (present at run time), then the spec named in ``.conductor/goal.md``. ``(None, "none")``
    when neither resolves. Distinct from the explicit ``$CONDUCTOR_GATE_SLUG`` override."""
    s = _run_branch_slug(root)
    if s:
        return s, "run_branch"
    s = _goal_slug(root)
    if s:
        return s, "goal"
    return None, "none"


def has_namespaced_frozen_gate(repo_root: str | None = None) -> bool:
    """True if any ``assertions/<slug>/.frozen`` exists — the repo has FROZEN per-spec gates.
    The flat baseline ``assertions/.frozen`` is NOT namespaced (no subdir) and never counts
    here (``resolve_gate`` checks the flat baseline separately)."""
    root = repo_root or project_root()
    return bool(glob.glob(os.path.join(root, "assertions", "*", ".frozen")))


class GateResolution(NamedTuple):
    """The fully-resolved done-gate location + integrity verdict for one run (see
    ``resolve_gate``). ``fail_closed`` is None when the run may proceed, else the reason
    ``assert run`` / ``gate verify`` must refuse."""

    directory: str  # the gate dir (manifest + baseline + results live here)
    manifest: str  # manifest.yaml path
    baseline: str  # .frozen baseline path
    run_dir: str  # results.json dir
    slug: str | None  # the resolved slug (None = flat gate)
    source: str  # how selected: gate_dir_env|explicit_slug|run_branch|goal|flat
    fail_closed: str | None  # None = ok; else the §5 refuse reason


def resolve_gate(repo_root: str | None = None) -> GateResolution:
    """THE gate-resolution policy — the single decision function for WHERE this run's done-gate
    lives and WHETHER it is dodging a frozen gate. ``gate_dir`` / ``manifest_path`` /
    ``baseline_path`` / ``run_dir`` / ``unresolved_frozen_gate`` all delegate here, and the
    runner + ``gate freeze|verify`` call it directly, so the policy cannot drift across callers.

    DIRECTORY precedence (``source``):
      1. ``$CONDUCTOR_GATE_DIR``  -> that dir                              (``gate_dir_env``)
      2. ``$CONDUCTOR_GATE_SLUG`` -> ``assertions/<slug>``, FORCED         (``explicit_slug``)
           A deliberate "this run is namespaced" signal (start exports it for the step-3
           build/lint/freeze, before run_branch/goal exist). No flat fallback: a not-yet-written
           or mis-written manifest fails closed rather than silently using the legacy flat gate.
      3. AMBIENT slug (``.conductor/run_branch`` then ``goal.md``)         (``run_branch`` / ``goal``)
           a. ``assertions/<slug>`` when it holds ``manifest.yaml`` OR ``.frozen`` (built/frozen)
           b. ``assertions/<slug>`` (unbuilt) when ANY namespaced ``.frozen`` exists — so a
              stale/corrupt slug fails closed instead of dodging to the flat gate
           c. else flat ``assertions/`` (legacy fallback; repo hasn't adopted per-spec gates)
      4. no slug -> flat ``assertions/``                                   (``flat``)

    ``manifest`` = ``$CONDUCTOR_MANIFEST`` or ``<dir>/manifest.yaml``;
    ``baseline`` = ``$CONDUCTOR_FREEZE_BASELINE`` or ``<dir>/.frozen``;
    ``run_dir``  = ``<dir-of-manifest>/run``.

    FAIL_CLOSED (§5 ambient-dodge guard) is set — and the runner + ``gate verify`` must refuse
    — on either signature of repointed ambient run metadata dodging a real frozen baseline
    (ANY explicit override — slug / gate-dir / manifest / freeze-baseline — is a deliberate
    selection and is exempt):
      (i)  the resolved gate is UNFROZEN (baseline absent) while a frozen gate exists
           ELSEWHERE — a namespaced ``assertions/<slug>/.frozen`` OR the flat ``assertions/
           .frozen`` (an edited ``run_branch`` or a planted unfrozen manifest); or
      (ii) the ``run_branch`` slug and the ``goal.md`` spec DISAGREE — ``run_branch`` was
           repointed onto a DIFFERENT (possibly already-green, frozen) gate than the one this
           run declared. ``/conductor:start`` writes the two together, so at run time they
           agree; a mismatch is repointed metadata.
    A repo with no frozen gate at all, and a run whose run_branch/goal.md agree, is never
    affected."""
    root = repo_root or project_root()
    flat = os.path.join(root, "assertions")
    env_dir = os.environ.get("CONDUCTOR_GATE_DIR")
    env_slug = os.environ.get("CONDUCTOR_GATE_SLUG")
    env_manifest = os.environ.get("CONDUCTOR_MANIFEST")
    env_baseline = os.environ.get("CONDUCTOR_FREEZE_BASELINE")
    explicit = bool(env_dir or env_slug or env_manifest or env_baseline)

    if env_dir:  # (1)
        directory, slug, source = env_dir, None, "gate_dir_env"
    elif env_slug:  # (2) forced, no fallback
        directory, slug, source = (
            os.path.join(flat, env_slug),
            env_slug,
            "explicit_slug",
        )
    else:
        slug, source = _ambient_slug(root)  # (3) / (4)
        if slug:
            nsdir = os.path.join(flat, slug)
            built = os.path.isfile(
                os.path.join(nsdir, "manifest.yaml")
            ) or os.path.isfile(os.path.join(nsdir, ".frozen"))
            if built or has_namespaced_frozen_gate(root):  # (3a) / (3b)
                directory = nsdir
            else:  # (3c) legacy fallback
                directory, slug, source = flat, None, "flat"
        else:  # (4)
            directory, source = flat, "flat"

    manifest = env_manifest or os.path.join(directory, "manifest.yaml")
    baseline = env_baseline or os.path.join(directory, ".frozen")
    rundir = os.path.join(os.path.dirname(manifest), "run")

    fail_closed = None
    if not explicit:
        if not os.path.exists(baseline):
            # (i) dodge onto an UNFROZEN gate while a frozen gate exists elsewhere.
            flat_frozen = os.path.isfile(os.path.join(flat, ".frozen"))
            if flat_frozen or has_namespaced_frozen_gate(root):
                fail_closed = (
                    "run resolves to an unfrozen gate but a frozen gate exists — check "
                    ".conductor/run_branch or CONDUCTOR_GATE_SLUG"
                )
        elif source == "run_branch":
            # (ii) dodge onto a DIFFERENT, already-FROZEN gate by repointing run_branch:
            # run_branch and goal.md are two independent declarations of the run's spec, and
            # /conductor:start writes them together. If goal.md names a DIFFERENT spec, the
            # run_branch was repointed to validate an alternate baseline (e.g. another spec's
            # green gate) instead of this run's — fail closed (§5).
            goal = _goal_slug(root)
            if goal is not None and goal != slug:
                fail_closed = (
                    f".conductor/run_branch names {slug!r} but goal.md names {goal!r} — "
                    "repointed run metadata; check .conductor/run_branch"
                )
    return GateResolution(
        directory, manifest, baseline, rundir, slug, source, fail_closed
    )


def gate_slug(repo_root: str | None = None) -> str | None:
    """The slug that names this run's gate, or None for the flat gate: ``$CONDUCTOR_GATE_SLUG``,
    else the ambient ``.conductor/run_branch`` slug, else the ``goal.md`` spec's slug."""
    env = os.environ.get("CONDUCTOR_GATE_SLUG")
    if env:
        return env
    return _ambient_slug(repo_root or project_root())[0]


def gate_dir(repo_root: str | None = None) -> str:
    """The directory holding this run's done-gate. Thin wrapper over ``resolve_gate``."""
    return resolve_gate(repo_root).directory


def manifest_path(repo_root: str | None = None) -> str:
    """The done-gate manifest path. Thin wrapper over ``resolve_gate``."""
    return resolve_gate(repo_root).manifest


def baseline_path(repo_root: str | None = None) -> str:
    """The freeze baseline path. Thin wrapper over ``resolve_gate``."""
    return resolve_gate(repo_root).baseline


def run_dir(repo_root: str | None = None) -> str:
    """Where the runner writes ``results.json``. Thin wrapper over ``resolve_gate``."""
    return resolve_gate(repo_root).run_dir


def unresolved_frozen_gate(repo_root: str | None = None) -> bool:
    """Whether this run is ambiently dodging a frozen gate (§5). Thin wrapper over
    ``resolve_gate`` — True iff ``fail_closed`` is set."""
    return resolve_gate(repo_root).fail_closed is not None
